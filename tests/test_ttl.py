from __future__ import annotations

from datetime import datetime, timedelta, timezone

from memgov.models import MemoryRecord
from memgov.policies import Capabilities, PolicyContext, TTLPolicy


def now():
    return datetime(2026, 11, 12, tzinfo=timezone.utc)


def rec(**kw: object) -> MemoryRecord:
    base = {"id": "r1", "text": "x", "kind": "experience", "tags": []}
    base.update(kw)
    return MemoryRecord(**base)  # type: ignore[arg-type]


def ctx(**caps):
    return PolicyContext(now=now(), capabilities=Capabilities(**caps), dry_run=True)


def test_tag_ttl_invalidates_old_only():
    old = rec(id="old", tags=["ephemeral"], created_at=now() - timedelta(days=8))
    fresh = rec(id="fresh", tags=["ephemeral"], created_at=now() - timedelta(days=1))
    policy = TTLPolicy([{"tag": "ephemeral", "max_age_days": 7}])
    actions = policy.evaluate([old, fresh], ctx())
    assert [a.memory_id for a in actions] == ["old"]
    assert actions[0].action == "invalidate"
    assert "ttl:7d" in actions[0].reason


def test_kind_ttl_and_first_rule_wins():
    r = rec(id="r", kind="experience", tags=["ephemeral"],
            created_at=now() - timedelta(days=10))
    policy = TTLPolicy([
        {"tag": "ephemeral", "max_age_days": 7},
        {"kind": "experience", "max_age_days": 90},
    ])
    actions = policy.evaluate([r], ctx())
    assert len(actions) == 1
    assert "ttl:7d" in actions[0].reason


def test_ttl_skips_invalid_and_unknown_age():
    invalid = rec(id="i", state="invalidated", tags=["ephemeral"],
                  created_at=now() - timedelta(days=30))
    no_date = rec(id="n", tags=["ephemeral"], created_at=None)
    policy = TTLPolicy([{"tag": "ephemeral", "max_age_days": 7}])
    assert policy.evaluate([invalid, no_date], ctx()) == []


def test_text_regex_rule():
    r = rec(id="r", text="MR !812 已合并，等待部署", created_at=now() - timedelta(days=3))
    policy = TTLPolicy([{"text_regex": r"MR !\d+", "max_age_days": 2}])
    assert len(policy.evaluate([r], ctx())) == 1


def test_rule_without_selector_is_ignored():
    r = rec(id="r", created_at=now() - timedelta(days=365))
    assert TTLPolicy([{"max_age_days": 1}]).evaluate([r], ctx()) == []
