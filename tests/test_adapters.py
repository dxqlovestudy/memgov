from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from memgov.adapters import HindsightAdapter, JsonFileAdapter
from memgov.models import Action


def sample_file(tmp_path):
    path = tmp_path / "memories.json"
    path.write_text(
        json.dumps(
            {
                "records": [
                    {"id": "a", "text": "A", "kind": "world", "tags": ["t"]},
                    {"id": "b", "text": "B", "kind": "world", "state": "valid"},
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def test_jsonfile_apply_and_restore(tmp_path):
    path = sample_file(tmp_path)
    adapter = JsonFileAdapter(str(path))
    assert [r.id for r in adapter.list_memories()] == ["a", "b"]
    action = Action(action="invalidate", memory_id="a", policy="ttl", reason="test")
    assert adapter.apply(action) is True
    assert [r.id for r in JsonFileAdapter(str(path)).list_memories()] == ["b"]
    assert adapter.restore("a") is True
    assert [r.id for r in JsonFileAdapter(str(path)).list_memories()] == ["a", "b"]


def test_hindsight_list_filters_observations(monkeypatch):
    adapter = HindsightAdapter("http://x", "bank")
    items = [
        {"id": "w1", "text": "world", "fact_type": "world", "metadata": {}},
        {"id": "o1", "text": "obs", "fact_type": "observation", "metadata": {}},
        {"id": "e1", "text": "exp", "fact_type": "experience", "metadata": {}},
    ]
    monkeypatch.setattr(
        adapter, "_request",
        lambda method, path, body=None: {"items": items, "total": len(items)},
    )
    records = adapter.list_memories()
    assert [r.id for r in records] == ["w1", "e1"]


def test_hindsight_maps_metadata_supersedes(monkeypatch):
    adapter = HindsightAdapter("http://x", "bank")
    item = {
        "id": "new", "text": "B", "fact_type": "world",
        "metadata": {"supersedes": "old,older"},
    }
    monkeypatch.setattr(
        adapter, "_request",
        lambda method, path, body=None: {"items": [item], "total": 1},
    )
    rec = adapter.list_memories()[0]
    assert rec.supersedes == ["old", "older"]


def test_hindsight_write_guard():
    adapter = HindsightAdapter("http://x", "bank", allow_write=False)
    with pytest.raises(PermissionError):
        adapter.apply(Action(action="invalidate", memory_id="a", policy="ttl", reason="r"))


def test_hindsight_apply_patches_state(monkeypatch):
    adapter = HindsightAdapter("http://x", "bank", allow_write=True)
    calls = []
    monkeypatch.setattr(
        adapter, "_request",
        lambda method, path, body=None: calls.append((method, path, body)) or {},
    )
    assert adapter.apply(Action(action="invalidate", memory_id="abc", policy="ttl", reason="expired"))
    assert calls[0][0] == "PATCH"
    assert calls[0][1].endswith("/memories/abc")
    assert calls[0][2] == {"state": "invalidated", "reason": "expired"}


def test_hindsight_restore_sets_valid(monkeypatch):
    adapter = HindsightAdapter("http://x", "bank", allow_write=True)
    calls = []
    monkeypatch.setattr(
        adapter, "_request",
        lambda method, path, body=None: calls.append((method, path, body)) or {},
    )
    adapter.restore("abc")
    assert calls[0][2]["state"] == "valid"


def test_hindsight_delete_disabled_by_default(monkeypatch):
    adapter = HindsightAdapter("http://x", "bank", allow_write=True)
    with pytest.raises(PermissionError):
        adapter.apply(Action(action="delete", memory_id="a", policy="ttl", reason="r"))


def test_hindsight_created_at_parsing(monkeypatch):
    adapter = HindsightAdapter("http://x", "bank")
    item = {
        "id": "w", "text": "T", "fact_type": "world",
        "date": "2026-11-01T00:00:00Z", "metadata": {},
    }
    monkeypatch.setattr(
        adapter, "_request",
        lambda method, path, body=None: {"items": [item], "total": 1},
    )
    rec = adapter.list_memories()[0]
    assert rec.created_at == datetime(2026, 11, 1, tzinfo=timezone.utc)
