from __future__ import annotations

from datetime import datetime, timedelta, timezone

from memgov.adapters.memory import MemoryAdapter
from memgov.ai import AIConfig, AIDecision
from memgov.ai_governor import AIGovernor
from memgov.ledger import Ledger
from memgov.models import MemoryRecord


def now():
    return datetime(2026, 11, 12, tzinfo=timezone.utc)


def records():
    return [
        MemoryRecord(
            id="old", text="Hindsight 实例地址是 http://192.0.2.10:8888",
            kind="world", created_at=now() - timedelta(days=2),
        ),
        MemoryRecord(
            id="new", text="Hindsight 实例地址改为 http://192.0.2.10:9999",
            kind="world", created_at=now(),
        ),
    ]


class FakeJudge:
    def __init__(self, decision: AIDecision):
        self.decision = decision
        self.calls = 0

    def judge(self, a, b):
        self.calls += 1
        return self.decision, False

    def usage(self):
        return {"calls": self.calls, "cached": 0, "prompt_tokens": 100,
                "completion_tokens": 50, "total_tokens": 150}


class StickyAdapter(MemoryAdapter):
    """Recall ignores invalidation, to exercise auto-rollback."""

    def recall(self, query: str, limit: int = 5):
        return [{"id": r.id, "text": r.text} for r in self.records][:limit]


def config(tmp_path, **over):
    data = dict(
        base_url="http://x", api_key="k", model="m",
        confidence_threshold=0.85, max_pairs=10, max_invalidations=5,
        cache_path=None, auto_rollback=True, verify_failure_threshold=0.1,
    )
    data.update(over)
    return AIConfig(**data)


def decision():
    return AIDecision(
        decision="invalidate_a", relation="supersede", keep_id="new", invalidate_id="old",
        confidence=0.95,
        evidence=[{"id": "old", "quote": "192.0.2.10"}, {"id": "new", "quote": "192.0.2.10"}],
        reason="地址更新",
    )


def test_dry_run_does_not_mutate(tmp_path):
    adapter = MemoryAdapter(records())
    gov = AIGovernor(adapter, config(tmp_path), Ledger(str(tmp_path)),
                     dry_run=True, judge=FakeJudge(decision()))
    report = gov.run()
    assert len(report.actions) == 1
    assert report.actions[0].applied is False
    assert [r.id for r in adapter.list_memories()] == ["old", "new"]
    assert report.ai["candidate_pairs"] >= 1


def test_apply_and_verify(tmp_path):
    adapter = MemoryAdapter(records())
    gov = AIGovernor(adapter, config(tmp_path), Ledger(str(tmp_path)),
                     dry_run=False, judge=FakeJudge(decision()))
    report = gov.run()
    assert report.actions[0].applied is True
    assert [r.id for r in adapter.list_memories()] == ["new"]
    assert report.verification["failures"] == 0
    assert report.rolled_back is False


def test_auto_rollback_when_forgetting_fails(tmp_path):
    adapter = StickyAdapter(records())
    gov = AIGovernor(adapter, config(tmp_path), Ledger(str(tmp_path)),
                     dry_run=False, judge=FakeJudge(decision()))
    report = gov.run()
    assert report.verification["failure_rate"] == 1.0
    assert report.rolled_back is True
    assert report.actions[0].applied is False
    assert [r.id for r in adapter.records if r.state == "valid"] == ["old", "new"]


class DynamicJudge:
    """Always invalidates the older side of the pair, with real evidence."""

    def __init__(self):
        self.calls = 0

    def judge(self, a, b):
        self.calls += 1
        return AIDecision(
            decision="invalidate_a", relation="supersede", keep_id=b.id, invalidate_id=a.id,
            confidence=0.95, evidence=[{"id": a.id, "quote": a.text[:10]}], reason="newer wins",
        ), False

    def usage(self):
        return {"calls": self.calls, "cached": 0, "prompt_tokens": 10,
                "completion_tokens": 5, "total_tokens": 15}


def test_max_invalidations_cap(tmp_path):
    recs = records() + [
        MemoryRecord(id="third", text="Hindsight 实例地址是 http://192.0.2.10:7777",
                     kind="world", created_at=now() - timedelta(days=3)),
    ]
    adapter = MemoryAdapter(recs)
    gov = AIGovernor(adapter, config(tmp_path, max_invalidations=1),
                     Ledger(str(tmp_path)), dry_run=True, judge=DynamicJudge())
    report = gov.run()
    assert len(report.actions) == 1
    assert report.ai.get("cap_reached") is True


def test_candidates_only_skips_model(tmp_path):
    class Boom:
        def judge(self, a, b):
            raise AssertionError("model must not be called")

        def usage(self):
            return {}

    adapter = MemoryAdapter(records())
    gov = AIGovernor(adapter, config(tmp_path), Ledger(str(tmp_path)),
                     dry_run=True, candidates_only=True, judge=Boom())
    report = gov.run()
    assert report.actions == []
    assert len(report.ai["candidates"]) >= 1
