from __future__ import annotations

from datetime import datetime, timezone

from memgov.models import MemoryRecord
from memgov.policies import Capabilities, PolicyContext, SupersedePolicy


def now():
    return datetime(2026, 11, 12, tzinfo=timezone.utc)


def ctx():
    return PolicyContext(now=now(), capabilities=Capabilities(), dry_run=True)


def test_metadata_supersedes_invalidates_old():
    old = MemoryRecord(id="old", text="地址 A", kind="world")
    new = MemoryRecord(id="new", text="地址 B", kind="world",
                       metadata={"supersedes": ["old"]})
    actions = SupersedePolicy().evaluate([old, new], ctx())
    assert [(a.memory_id, a.replacement_id) for a in actions] == [("old", "new")]
    assert actions[0].reason == "superseded_by:new"


def test_tag_form_and_string_form():
    old = MemoryRecord(id="old", text="A")
    new = MemoryRecord(id="new", text="B", tags=["supersedes:old"])
    assert len(SupersedePolicy().evaluate([old, new], ctx())) == 1

    old2 = MemoryRecord(id="old2", text="A2")
    new2 = MemoryRecord(id="new2", text="B2", metadata={"supersedes": "old2"})
    assert len(SupersedePolicy().evaluate([old2, new2], ctx())) == 1


def test_similarity_is_not_used_as_supersede_evidence():
    old = MemoryRecord(id="old", text="Hindsight 实例地址是 192.0.2.10")
    new = MemoryRecord(id="new", text="Hindsight 实例地址已改为 192.0.2.20")
    assert SupersedePolicy().evaluate([old, new], ctx()) == []


def test_self_and_missing_targets_ignored():
    old = MemoryRecord(id="old", text="A")
    new = MemoryRecord(id="new", text="B", metadata={"supersedes": ["new", "ghost"]})
    assert SupersedePolicy().evaluate([old, new], ctx()) == []


def test_already_invalid_target_skipped():
    old = MemoryRecord(id="old", text="A", state="invalidated")
    new = MemoryRecord(id="new", text="B", metadata={"supersedes": ["old"]})
    assert SupersedePolicy().evaluate([old, new], ctx()) == []
