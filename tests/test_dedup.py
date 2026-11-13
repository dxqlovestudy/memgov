from __future__ import annotations

from datetime import datetime, timedelta, timezone

from memgov.models import MemoryRecord
from memgov.policies import Capabilities, DedupPolicy, PolicyContext


def now():
    return datetime(2026, 11, 12, tzinfo=timezone.utc)


def ctx():
    return PolicyContext(now=now(), capabilities=Capabilities(), dry_run=True)


def test_near_duplicate_keeps_newest():
    older = MemoryRecord(
        id="old", text="交付前必须跑全量 mvn -o test 并附原始输出。", kind="world",
        created_at=now() - timedelta(days=2), importance=0.9,
    )
    newer = MemoryRecord(
        id="new", text="交付前必须跑全量 mvn -o test，并附原始输出", kind="world",
        created_at=now(), importance=0.5,
    )
    actions = DedupPolicy(similarity=0.92).evaluate([older, newer], ctx())
    assert [(a.memory_id, a.replacement_id) for a in actions] == [("old", "new")]
    assert "duplicate_of:new" in actions[0].reason


def test_exact_duplicate_always_collapsed():
    a = MemoryRecord(id="a", text="同一句话。", kind="world", created_at=now())
    b = MemoryRecord(id="b", text="同一句话", kind="world", created_at=now() - timedelta(days=1))
    actions = DedupPolicy(similarity=1.0).evaluate([a, b], ctx())
    assert len(actions) == 1
    assert actions[0].memory_id == "b"


def test_different_kind_not_deduped():
    a = MemoryRecord(id="a", text="同一句话。", kind="world", created_at=now())
    b = MemoryRecord(id="b", text="同一句话。", kind="observation", created_at=now())
    assert DedupPolicy(similarity=0.9).evaluate([a, b], ctx()) == []


def test_unrelated_text_not_deduped():
    a = MemoryRecord(id="a", text="今天要发布 dev 环境。", kind="world", created_at=now())
    b = MemoryRecord(id="b", text="任何 git push 必须逐次同意。", kind="world", created_at=now())
    assert DedupPolicy(similarity=0.9).evaluate([a, b], ctx()) == []


def test_blocking_finds_near_duplicate_in_large_kind():
    base = "交付前必须跑全量 mvn -o test 并附原始输出，且保留日志。"
    variant = base.replace("且保留日志", "并保留日志")
    import uuid

    records = [
        MemoryRecord(id=f"r{i}", text=f"unrelated record {uuid.uuid4().hex}", kind="world",
                     created_at=now())
        for i in range(50)
    ]
    records.append(MemoryRecord(id="new", text=base, kind="world", created_at=now()))
    records.append(MemoryRecord(id="old", text=variant, kind="world",
                                created_at=now() - __import__("datetime").timedelta(days=1)))
    actions = DedupPolicy(similarity=0.85).evaluate(records, ctx())
    assert [a.memory_id for a in actions] == ["old"]
    assert actions[0].replacement_id == "new"
