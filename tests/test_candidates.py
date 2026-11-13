from __future__ import annotations

from datetime import datetime, timedelta, timezone

from memgov.candidates import candidate_pairs, extract_anchors
from memgov.models import MemoryRecord


def now():
    return datetime(2026, 11, 12, tzinfo=timezone.utc)


def test_extract_anchors():
    text = "实例 http://192.0.2.10:8888，表 T_PARTNER_PROTOCOL，文件 Foo.java，MR !812"
    anchors = extract_anchors(text)
    assert any("192.0.2.10" in a for a in anchors)
    assert any("t_partner_protocol" in a for a in anchors)
    assert any("foo.java" in a for a in anchors)
    assert any("mr !812" in a for a in anchors)


def test_candidate_pair_by_shared_anchor():
    a = MemoryRecord(id="a", text="实例地址是 http://192.0.2.10:8888", kind="world")
    b = MemoryRecord(id="b", text="实例地址改为 http://192.0.2.10:8899", kind="world")
    c = MemoryRecord(id="c", text="今天午饭吃什么", kind="world")
    pairs = candidate_pairs([a, b, c], max_pairs=10, min_score=0.3)
    keys = {p.key() for p in pairs}
    assert "a|b" in keys
    assert "a|c" not in keys


def test_candidate_order_by_created_at():
    older = MemoryRecord(id="old", text="Hindsight 地址是 http://192.0.2.10:8888",
                         kind="world", created_at=now() - timedelta(days=2))
    newer = MemoryRecord(id="new", text="Hindsight 地址改为 http://192.0.2.10:9999",
                         kind="world", created_at=now())
    pair = candidate_pairs([newer, older], max_pairs=1)[0]
    assert pair.a.id == "old"
    assert pair.b.id == "new"


def test_candidate_max_pairs():
    records = [
        MemoryRecord(id=f"r{i}", text=f"同一主题 http://192.0.2.10:8888 记录 {i}",
                     kind="world")
        for i in range(10)
    ]
    pairs = candidate_pairs(records, max_pairs=3)
    assert len(pairs) == 3


def test_same_kind_only():
    a = MemoryRecord(id="a", text="同一主题 http://192.0.2.10:8888", kind="world")
    b = MemoryRecord(id="b", text="同一主题 http://192.0.2.10:8888", kind="experience")
    assert candidate_pairs([a, b], same_kind_only=True) == []
    assert len(candidate_pairs([a, b], same_kind_only=False)) == 1
