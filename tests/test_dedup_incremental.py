from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np

from memgov.dedup_incremental import IncrementalDeduper, signature, strip_meta
from memgov.embeddings import EmbeddingIndex
from memgov.ledger import Ledger
from memgov.models import MemoryRecord


def now():
    return datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)


def rec(rid, text, ts=None, kind="world"):
    return MemoryRecord(id=rid, text=text, kind=kind, created_at=ts or now())


class FakeAdapter:
    """Serves a fixed record set; apply() records what was invalidated."""

    name = "fake"

    def __init__(self, records):
        self.records = {r.id: r for r in records}
        self.invalidated = []

    def list_memories(self, limit=None, since=None):
        return list(self.records.values())

    def get_memory(self, memory_id):
        return self.records.get(memory_id)

    def apply(self, action):
        self.invalidated.append(action.memory_id)
        self.records.pop(action.memory_id, None)
        return True

    def restore(self, memory_id):
        return True

    def describe(self):
        return {"name": self.name}


class FakeEmbedder:
    """Returns vectors from a dict; unknown texts get an orthogonal vector."""

    def __init__(self, mapping):
        self.mapping = mapping
        self.calls = 0
        self._ortho = 0

    def embed(self, texts):
        self.calls += 1
        out = []
        for t in texts:
            v = self.mapping.get(t)
            if v is None:
                self._ortho += 1
                v = [0.0] * 8
                v[self._ortho % 8] = 1.0
            out.append(list(v))
        return out


def unit(*vals):
    v = np.asarray(vals, dtype=np.float32)
    return (v / np.linalg.norm(v)).tolist()


def make_index(tmp_path, records, embedder):
    idx = EmbeddingIndex(str(tmp_path / "idx.npz"))
    pairs = list(zip([r.id for r in records], embedder.embed([r.text for r in records])))
    idx.upsert(pairs)
    idx.save()
    return idx


def make(deduper_records, embedder, tmp_path, **kw):
    adapter = FakeAdapter(deduper_records)
    idx = make_index(tmp_path, deduper_records, embedder)
    ledger = Ledger(str(tmp_path))
    return adapter, IncrementalDeduper(
        adapter, embedder, idx, ledger, dry_run=kw.pop("dry_run", True), **kw
    )


# -- signature ---------------------------------------------------------------
def test_strip_meta_removes_server_tail():
    text = "dev 环境镜像已更新为 333126-6cd22a9a。 | When: 2026-11-22 | Involving: 助手"
    assert strip_meta(text) == "dev 环境镜像已更新为 333126-6cd22a9a。"


def test_signature_captures_numbers_and_ids():
    a = signature("镜像 333126-6cd22a9a，端口 8888")
    assert "333126" in " ".join(a) or any("333126" in x for x in a)
    assert "8888" in a


def test_signature_differs_by_issue_id():
    a = signature("用户被指派的 issue ID 为 01a1c487-f7e2-7a61")
    b = signature("用户被分配的 issue ID 为 01a1c34b-6c1e-7376")
    assert a != b


# -- decisions ---------------------------------------------------------------
def test_high_cosine_with_matching_signature_is_invalidated(tmp_path):
    old_text = "dev 环境 mng-agent 镜像 = 333126-6cd22a9a"
    new_text = "dev 环境 mng-agent 的镜像为 333126-6cd22a9a"
    v = unit(1, 0, 0, 0, 0, 0, 0, 0)
    old = rec("old", old_text, now() - timedelta(days=1))
    new = rec("new", new_text, now())
    adapter, d = make([old, new], FakeEmbedder({old_text: v, new_text: v}), tmp_path,
                      dry_run=False, threshold=0.92)
    report = d.run(window_minutes=10)
    assert [a.memory_id for a in report.actions] == ["old"]
    assert adapter.invalidated == ["old"]


def test_signature_gate_blocks_near_identical_ids(tmp_path):
    old_text = "用户被指派的 issue ID 为 01a1c487-f7e2-7a61"
    new_text = "用户被分配的 issue ID 为 01a1c34b-6c1e-7376"
    v = unit(1, 0, 0, 0, 0, 0, 0, 0)
    old = rec("old2", old_text, now() - timedelta(days=1))
    new = rec("new2", new_text, now())
    adapter, d = make([old, new], FakeEmbedder({old_text: v, new_text: v}), tmp_path,
                      dry_run=False, threshold=0.92)
    report = d.run(window_minutes=10)
    assert report.actions == []
    assert report.ai["summary"]["signature_blocked"] == 1
    assert adapter.invalidated == []


def test_boundary_band_without_judge_is_uncertain(tmp_path):
    old_text = "白名单为两条域名"
    new_text = "白名单是两条域名入口"
    v_old = unit(1, 0, 0, 0, 0, 0, 0, 0)
    v_new = unit(0.9, 0.435, 0, 0, 0, 0, 0, 0)  # cosine ~0.90
    old = rec("old3", old_text, now() - timedelta(days=1))
    new = rec("new3", new_text, now())
    adapter, d = make([old, new], FakeEmbedder({old_text: v_old, new_text: v_new}), tmp_path,
                      dry_run=False, threshold=0.95, boundary_low=0.85)
    report = d.run(window_minutes=10)
    assert report.actions == []
    assert report.ai["summary"]["uncertain"] == 1


def test_boundary_band_with_judge_confirming_duplicate(tmp_path):
    old_text = "白名单为两条域名"
    new_text = "白名单是两条域名入口"
    v_old = unit(1, 0, 0, 0, 0, 0, 0, 0)
    v_new = unit(0.9, 0.435, 0, 0, 0, 0, 0, 0)
    old = rec("old4", old_text, now() - timedelta(days=1))
    new = rec("new4", new_text, now())
    adapter, d = make([old, new], FakeEmbedder({old_text: v_old, new_text: v_new}), tmp_path,
                      dry_run=False, threshold=0.95, boundary_low=0.85,
                      verify_duplicate=lambda keep, drop: "DUPLICATE")
    report = d.run(window_minutes=10)
    assert [a.memory_id for a in report.actions] == ["old4"]


def test_judge_rejecting_near_duplicate_leaves_record_alone(tmp_path):
    old_text = "配置项 A 为 true"
    new_text = "配置项 A 为 true，附带说明"
    v_old = unit(1, 0, 0, 0, 0, 0, 0, 0)
    v_new = unit(0.9, 0.435, 0, 0, 0, 0, 0, 0)
    old = rec("old5", old_text, now() - timedelta(days=1))
    new = rec("new5", new_text, now())
    adapter, d = make([old, new], FakeEmbedder({old_text: v_old, new_text: v_new}), tmp_path,
                      dry_run=False, threshold=0.95, boundary_low=0.85,
                      verify_duplicate=lambda keep, drop: None)
    report = d.run(window_minutes=10)
    assert report.actions == []
    assert report.ai["summary"]["uncertain"] == 1


def test_max_actions_is_enforced(tmp_path):
    v = unit(1, 0, 0, 0, 0, 0, 0, 0)
    recs = [rec("keep", "同一件事的表述", now())]
    mapping = {"同一件事的表述": v}
    for i, suffix in enumerate(["甲", "乙", "丙", "丁", "戊"]):
        text = f"同一件事的表述副本{suffix}"
        mapping[text] = v
        recs.append(rec(f"dup{i}", text, now() - timedelta(minutes=i + 1)))
    adapter, d = make(recs, FakeEmbedder(mapping), tmp_path, dry_run=False, max_actions=2)
    report = d.run(window_minutes=10)
    assert len(report.actions) == 2
    assert any("max_actions" in w for w in report.warnings)


def test_dry_run_does_not_mutate(tmp_path):
    text = "dev 环境镜像 = 333126"
    v = unit(1, 0, 0, 0, 0, 0, 0, 0)
    old = rec("old6", text, now() - timedelta(days=1))
    new = rec("new6", text + " 已确认", now())
    adapter, d = make([old, new], FakeEmbedder({text: v, text + " 已确认": v}), tmp_path,
                      dry_run=True)
    report = d.run(window_minutes=10)
    assert len(report.actions) == 1
    assert adapter.invalidated == []


# -- index -------------------------------------------------------------------
def test_index_roundtrip_and_knn(tmp_path):
    path = str(tmp_path / "i.npz")
    idx = EmbeddingIndex(path)
    idx.upsert([("a", unit(1, 0, 0, 0)), ("b", unit(0, 1, 0, 0))])
    idx.save()
    reloaded = EmbeddingIndex(path)
    assert len(reloaded) == 2
    hits = reloaded.knn(unit(1, 0, 0, 0), top_k=2, min_cos=0.5)
    assert hits[0][0] == "a"
    assert hits[0][1] > 0.99


def test_index_incremental_append_keeps_existing(tmp_path):
    idx = EmbeddingIndex(str(tmp_path / "j.npz"))
    idx.upsert([("a", unit(1, 0, 0, 0))])
    added = idx.upsert([("a", unit(1, 0, 0, 0)), ("b", unit(0, 1, 0, 0))])
    assert added == 1
    assert len(idx) == 2
