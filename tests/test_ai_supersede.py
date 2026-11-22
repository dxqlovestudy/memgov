from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from memgov.adapters.hindsight import HindsightAdapter
from memgov.ai import AIConfig
from memgov.ai_supersede import (
    SupersedeGovernor,
    SupersedeLedger,
    SupersedeVerdict,
    detect_conflict,
    order_pair,
    parse_verdict,
)
from memgov.candidates import CandidatePair
from memgov.ledger import Ledger
from memgov.models import MemoryRecord
from memgov.prompts import SUPERSEDE_FRAMINGS

T0 = datetime(2026, 11, 1, tzinfo=timezone.utc)
T1 = T0 + timedelta(days=3)

OLD_TEXT = "Hindsight 实例地址为 http://172.20.9.122:8888"
NEW_TEXT = "Hindsight 实例地址改为 http://172.20.12.116:8888"


def rec(rid: str, text: str, when: datetime, kind: str = "world", meta=None) -> MemoryRecord:
    return MemoryRecord(
        id=rid, text=text, kind=kind, tags=[], metadata=meta or {},
        created_at=when, updated_at=when, state="valid",
    )


def test_detect_conflict_hits_replacement_words():
    assert "改为" in detect_conflict(NEW_TEXT)
    assert detect_conflict("这是一条普通说明，没有变化") == ""


def test_order_pair_prefers_newer():
    old, new = rec("o", OLD_TEXT, T0), rec("n", NEW_TEXT, T1)
    pair_fwd = CandidatePair(a=old, b=new, score=0.5, shared_anchors=[])
    pair_rev = CandidatePair(a=new, b=old, score=0.5, shared_anchors=[])
    assert order_pair(pair_fwd)[0].id == "n"
    assert order_pair(pair_rev)[0].id == "n"


def test_parse_verdict_keeps_valid_evidence():
    old, new = rec("o", OLD_TEXT, T0), rec("n", NEW_TEXT, T1)
    content = json.dumps(
        {
            "verdict": "SUPERSEDES",
            "confidence": 0.93,
            "evidence_new": "地址改为 http://172.20.12.116:8888",
            "evidence_old": "地址为 http://172.20.9.122:8888",
            "reason": "同一实例地址被替换",
        },
        ensure_ascii=False,
    )
    verdict = parse_verdict(content, "direct_replacement", new, old)
    assert verdict.verdict == "SUPERSEDES"
    assert verdict.confidence == 0.93


def test_parse_verdict_rejects_evidence_not_in_text():
    old, new = rec("o", OLD_TEXT, T0), rec("n", NEW_TEXT, T1)
    content = json.dumps(
        {"verdict": "SUPERSEDES", "confidence": 0.99, "evidence_new": "编造的片段", "evidence_old": "地址为 http://172.20.9.122:8888"},
        ensure_ascii=False,
    )
    verdict = parse_verdict(content, "direct_replacement", new, old)
    assert verdict.verdict == "INDEPENDENT"
    assert verdict.reason == "evidence_new_not_in_text"


def test_parse_verdict_bad_json_is_independent():
    old, new = rec("o", OLD_TEXT, T0), rec("n", NEW_TEXT, T1)
    assert parse_verdict("not json", "x", new, old).verdict == "INDEPENDENT"


def test_supersede_ledger_map_and_revoke(tmp_path):
    led = SupersedeLedger(str(tmp_path / "s.jsonl"))
    led.append({"action": "stamp", "new_id": "n1", "old_id": "o1"})
    led.append({"action": "stamp", "new_id": "n1", "old_id": "o2"})
    assert led.map_by_new() == {"n1": ["o1", "o2"]}
    led.append({"action": "revoke", "new_id": "n1", "old_id": "o1"})
    assert led.map_by_new() == {"n1": ["o2"]}


class FakeJudge:
    def __init__(self, verdict="SUPERSEDES", confidence=0.95):
        self.verdict = verdict
        self.confidence = confidence
        self.seen = 0

    def judge(self, new, old, anchors, conflict):
        self.seen += 1
        return [SupersedeVerdict(f, self.verdict, self.confidence) for f in SUPERSEDE_FRAMINGS]

    def usage(self):
        return {"calls": 0, "cached": 0}


class FakeAdapter:
    name = "memory"

    def __init__(self, records):
        self.records = records

    def list_memories(self, limit=None):
        return self.records[:limit] if limit else list(self.records)


def _config() -> AIConfig:
    return AIConfig(base_url="http://127.0.0.1:1", api_key="x", model="m")


def test_governor_dry_run_proposes_without_writing(tmp_path):
    records = [rec("old1", OLD_TEXT, T0), rec("new1", NEW_TEXT, T1)]
    led = SupersedeLedger(str(tmp_path / "s.jsonl"))
    judge = FakeJudge()
    gov = SupersedeGovernor(
        FakeAdapter(records), _config(), ledger=Ledger(str(tmp_path)),
        supersede_ledger=led, dry_run=True, judge=judge, min_score=0.0, max_pairs=10,
    )
    report = gov.run()
    assert judge.seen == 1
    assert [a.memory_id for a in report.actions] == ["old1"]
    assert report.actions[0].replacement_id == "new1"
    assert report.actions[0].applied is False
    assert led.entries() == []


def test_governor_apply_writes_ledger(tmp_path):
    records = [rec("old1", OLD_TEXT, T0), rec("new1", NEW_TEXT, T1)]
    led = SupersedeLedger(str(tmp_path / "s.jsonl"))
    gov = SupersedeGovernor(
        FakeAdapter(records), _config(), ledger=Ledger(str(tmp_path)),
        supersede_ledger=led, dry_run=False, judge=FakeJudge(), min_score=0.0, max_pairs=10,
    )
    report = gov.run()
    assert report.actions[0].applied is True
    assert led.map_by_new() == {"new1": ["old1"]}


def test_governor_rejects_when_votes_fall_short(tmp_path):
    records = [rec("old1", OLD_TEXT, T0), rec("new1", NEW_TEXT, T1)]
    led = SupersedeLedger(str(tmp_path / "s.jsonl"))
    gov = SupersedeGovernor(
        FakeAdapter(records), _config(), ledger=Ledger(str(tmp_path)),
        supersede_ledger=led, dry_run=False, judge=FakeJudge(verdict="INDEPENDENT"), min_score=0.0,
    )
    report = gov.run()
    assert report.actions == []
    assert led.entries() == []


def test_governor_skips_pairs_without_conflict_signal(tmp_path):
    old = rec("old2", "Hindsight 实例地址为 http://172.20.9.122:8888", T0)
    new = rec("new2", "Hindsight 实例地址为 http://172.20.12.116:8888", T1)
    judge = FakeJudge()
    gov = SupersedeGovernor(
        FakeAdapter([old, new]), _config(), ledger=Ledger(str(tmp_path)),
        supersede_ledger=SupersedeLedger(str(tmp_path / "s.jsonl")), dry_run=True,
        judge=judge, min_score=0.0,
    )
    report = gov.run()
    assert judge.seen == 0
    assert report.actions == []


def test_hindsight_injects_ledger_as_explicit_evidence(monkeypatch):
    adapter = HindsightAdapter("http://127.0.0.1:1", "bank", supersede_ledger={"new1": ["old1"]})
    fake = {
        "items": [
            {"id": "new1", "text": NEW_TEXT, "fact_type": "world", "state": "valid", "metadata": {}},
            {"id": "other", "text": "无关", "fact_type": "world", "state": "valid", "metadata": {}},
        ],
        "total": 2,
    }
    monkeypatch.setattr(HindsightAdapter, "_request", lambda self, method, path, body=None: fake)
    records = adapter.list_memories()
    assert records[0].metadata["supersedes"] == ["old1"]
    assert "supersedes" not in records[1].metadata
