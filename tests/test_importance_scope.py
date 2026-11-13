from __future__ import annotations

from datetime import datetime, timedelta, timezone

from memgov.models import MemoryRecord
from memgov.policies import (
    Capabilities,
    ImportancePolicy,
    PolicyContext,
    ScopeLimitPolicy,
)


def now():
    return datetime(2026, 11, 12, tzinfo=timezone.utc)


def test_importance_policy_skipped_without_capability():
    r = MemoryRecord(id="r", text="x", kind="world", importance=0.01,
                     created_at=now() - timedelta(days=1000))
    policy = ImportancePolicy(half_life_days=30, archive_below=0.2)
    ctx = PolicyContext(now=now(), capabilities=Capabilities(importance=False), dry_run=True)
    assert policy.evaluate([r], ctx) == []


def test_importance_policy_archives_low_score():
    old = MemoryRecord(id="old", text="x", kind="world", importance=0.01,
                       created_at=now() - timedelta(days=1000))
    fresh = MemoryRecord(id="fresh", text="y", kind="world", importance=0.9,
                         created_at=now())
    ctx = PolicyContext(now=now(), capabilities=Capabilities(importance=True), dry_run=True)
    policy = ImportancePolicy(half_life_days=30, archive_below=0.2)
    actions = policy.evaluate([old, fresh], ctx)
    assert [a.memory_id for a in actions] == ["old"]
    assert actions[0].action == "archive"


def test_scope_limit_evicts_lowest_score():
    records = [
        MemoryRecord(id=f"r{i}", text=f"x{i}", kind="world", importance=0.1 * i,
                     tags=["scope:s1"], created_at=now())
        for i in range(1, 5)
    ]
    other = MemoryRecord(id="o", text="o", kind="world", tags=["scope:s2"],
                         importance=0.1, created_at=now())
    ctx = PolicyContext(now=now(), capabilities=Capabilities(importance=True), dry_run=True)
    actions = ScopeLimitPolicy(max_per_scope=2, tag_prefix="scope:").evaluate(
        records + [other], ctx
    )
    assert sorted(a.memory_id for a in actions) == ["r1", "r2"]
    assert all("s1>" in a.reason for a in actions)
