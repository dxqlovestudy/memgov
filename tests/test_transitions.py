from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from memgov.ai import AIError
from memgov.models import MemoryRecord
from memgov.transitions import (
    TransitionDecision,
    build_assertion_index,
    detect_transition,
    extract_entities,
    parse_transition_decision,
    rank_candidates,
    transition_decision_to_action,
)


def rec(mid: str, text: str, created: datetime, kind: str = "world", tags=None) -> MemoryRecord:
    return MemoryRecord(id=mid, text=text, kind=kind, created_at=created, tags=tags or [])


OLD = datetime(2026, 10, 16, tzinfo=timezone.utc)
NEW = datetime(2026, 11, 13, tzinfo=timezone.utc)


def test_detect_transition_and_negation() -> None:
    fact = detect_transition(
        rec("t1", "aether-vm 上的 1092143 联调环境已于 2026-11-12 下线，统一走 dev 环境。", NEW)
    )
    assert fact is not None
    assert fact.transition_type == "OFFLINE"
    assert fact.for_time is not None and fact.for_time.date().isoformat() == "2026-11-12"
    assert detect_transition(rec("t2", "该环境尚未下线，继续观察。", NEW)) is None
    assert detect_transition(rec("t3", "如果 aether-vm 下线，就切到 dev。", NEW)) is None


def test_extract_entities_alias_and_generic() -> None:
    entities = extract_entities("aether-vm（172.20.12.116）入口 agent-dev.jlpay.com/p/agent/")
    ids = {entity.canonical_id for entity in entities}
    assert "host:aether-vm" in ids
    assert "env:dev" in ids
    assert "path:/p/agent" in ids
    assert "host:172.20.12.116" not in ids


def test_rank_candidates_scope_and_time() -> None:
    old = rec("a-old", "合伙人前端联调环境位于 aether-vm（172.20.12.116）。", OLD)
    later = rec("a-later", "合伙人前端联调环境位于 aether-vm。", NEW)
    unrelated = rec("a-ssh", "Aether VM 的 SSH 端口为 22。", OLD)
    transition = detect_transition(
        rec("t-new", "aether-vm 上的 1092143 联调环境已于 2026-11-12 下线，统一走 dev 环境。", NEW)
    )
    assert transition is not None
    index = build_assertion_index([old, later, unrelated])
    candidates = rank_candidates([transition], index, max_candidates=10)
    ids = {candidate.assertion.record.id for candidate in candidates}
    assert "a-old" in ids
    assert "a-ssh" in ids
    assert "a-later" not in ids
    assert "t-new" not in ids


def test_transition_action_gates() -> None:
    old = rec("a-old", "合伙人前端联调环境位于 aether-vm（172.20.12.116）。", OLD)
    transition = detect_transition(
        rec("t-new", "aether-vm 上的 1092143 联调环境已于 2026-11-12 下线，统一走 dev 环境。", NEW)
    )
    assert transition is not None
    assertion = [a for a in build_assertion_index([old]).get("host:aether-vm", [])][0]

    decision = TransitionDecision(
        verdict="obsolete_current",
        scope_entity="host:aether-vm",
        confidence=0.95,
        evidence_old="位于 aether-vm",
        evidence_transition="已于 2026-11-12 下线",
        historical_markers=[],
        reason="环境下线",
    )
    action, reason = transition_decision_to_action(
        transition, assertion, decision, confidence_threshold=0.90
    )
    assert reason is None
    assert action is not None
    assert action.policy == "state_transition"
    assert action.memory_id == "a-old"
    assert action.replacement_id == "t-new"

    low = TransitionDecision(**{**decision.__dict__, "confidence": 0.5})
    assert transition_decision_to_action(transition, assertion, low, confidence_threshold=0.90)[1] == "low_confidence:0.50"

    historical = TransitionDecision(**{**decision.__dict__, "verdict": "historical_ok"})
    assert transition_decision_to_action(transition, assertion, historical, confidence_threshold=0.90)[1] == "historical_ok"


def test_parse_transition_decision_contract() -> None:
    old = rec("a-old", "合伙人前端联调环境位于 aether-vm（172.20.12.116）。", OLD)
    transition = detect_transition(
        rec("t-new", "aether-vm 上的 1092143 联调环境已于 2026-11-12 下线，统一走 dev 环境。", NEW)
    )
    assert transition is not None
    assertion = build_assertion_index([old])["host:aether-vm"][0]
    payload = {
        "verdict": "obsolete_current",
        "scope_entity": "host:aether-vm",
        "confidence": 0.95,
        "evidence_old": "位于 aether-vm",
        "evidence_transition": "已于 2026-11-12 下线",
        "historical_markers": [],
        "reason": "环境下线",
    }
    decision = parse_transition_decision(json.dumps(payload, ensure_ascii=False), transition, assertion)
    assert decision.verdict == "obsolete_current"
    assert decision.to_dict()["scope_entity"] == "host:aether-vm"

    with pytest.raises(AIError):
        parse_transition_decision(
            json.dumps({**payload, "scope_entity": "host:unknown"}, ensure_ascii=False),
            transition,
            assertion,
        )
    with pytest.raises(AIError):
        parse_transition_decision(
            json.dumps({**payload, "evidence_old": "不在原文中"}, ensure_ascii=False),
            transition,
            assertion,
        )
