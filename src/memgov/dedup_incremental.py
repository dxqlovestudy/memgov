"""Incremental deduplication over a recent time window.

Why a sweep instead of write-time interception: the Hermes turn path must stay
untouched, and every mechanism here (ledger, rollback, verify-forgetting) is
already proven. The cost is a bounded lag (window length), not a risk of dropping
a write.

Two decisions encoded here, both from measurement:

- Signature gate (numeric/ID sets must match). On the observation layer 48% of
  cos>=0.92 clusters were false duplicates that differed only by an issue ID
  ("issue ID 为 01a1c487" vs "01a1c34b", cos=1.000), so cosine alone is not
  enough to invalidate a record.
- Two-tier decision. cos >= threshold is treated as duplicate without an LLM;
  0.85 <= cos < threshold is handed to the caller-supplied verify_duplicate
  callback (the three-framing judge). Anything the judge does not confirm counts
  as "uncertain" and is left untouched — no default action on doubt.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, Optional, Tuple

from .adapters.base import Adapter
from .embeddings import EmbeddingClient, EmbeddingIndex
from .ledger import Ledger
from .models import Action, MemoryRecord, ScanReport

# The server appends these fields after extraction and they cannot be removed by
# prompt (verified on retain_extraction_mode=custom and =verbatim). Strip them
# before computing signatures, otherwise the tail's dates/numbers pollute it.
META_RE = re.compile(r"\s*\|\s*(When|Where|Involving):.*$", re.M)
TOKEN_RE = re.compile(r"[0-9a-f]{8,}|\d+\.\d+|\d+")


def strip_meta(text: str) -> str:
    return META_RE.sub("", text or "").strip()


def signature(text: str) -> Tuple[str, ...]:
    """Set of numeric/ID tokens, used as a hard gate before invalidating."""
    return tuple(sorted(set(TOKEN_RE.findall(strip_meta(text)))))


class IncrementalDeduper:
    """Find and invalidate duplicates among records written in the last window."""

    def __init__(
        self,
        adapter: Adapter,
        embedder: EmbeddingClient,
        index: EmbeddingIndex,
        ledger: Ledger,
        *,
        threshold: float = 0.92,
        boundary_low: float = 0.85,
        require_signature: bool = True,
        max_actions: int = 50,
        dry_run: bool = True,
        verify_duplicate: Optional[Callable[[MemoryRecord, MemoryRecord], Optional[str]]] = None,
        top_k: int = 5,
    ):
        self.adapter = adapter
        self.embedder = embedder
        self.index = index
        self.ledger = ledger
        self.threshold = threshold
        self.boundary_low = boundary_low
        self.require_signature = require_signature
        self.max_actions = max_actions
        self.dry_run = dry_run
        self.verify_duplicate = verify_duplicate
        self.top_k = top_k

    # -- helpers -------------------------------------------------------------
    @staticmethod
    def _is_newer(a: MemoryRecord, b: MemoryRecord) -> bool:
        """Pick the survivor of a duplicate pair.

        Newer wins only when the gap is real (>1 day), i.e. the record reflects a
        later state. Within the same vintage, keep the longer text: measured on a
        14-action sample, three board-delivery pairs lost the 'must attach a link'
        and 'ASCII filename' constraints because the newer record happened to be a
        terser paraphrase.
        """
        ta, tb = a.reference_time(), b.reference_time()
        if ta and tb:
            gap = abs((ta - tb).total_seconds())
            if gap > 86400:
                return ta > tb
        return len(a.text or "") >= len(b.text or "")

    def _judge(self, keep: MemoryRecord, drop: MemoryRecord, cos: float) -> Tuple[str, str]:
        """Return (decision, reason). decision is 'duplicate' or 'uncertain'."""
        if cos >= self.threshold:
            return "duplicate", f"cosine {cos:.3f} >= {self.threshold:.2f}"
        if self.verify_duplicate is None:
            return "uncertain", f"cosine {cos:.3f} in boundary band, no judge"
        verdict = self.verify_duplicate(keep, drop)
        if verdict == "DUPLICATE":
            return "duplicate", f"cosine {cos:.3f} + judge DUPLICATE"
        return "uncertain", f"cosine {cos:.3f}, judge={verdict or 'none'}"

    # -- main ----------------------------------------------------------------
    def run(self, window_minutes: int = 10) -> ScanReport:
        now = datetime.now(timezone.utc)
        since = now - timedelta(minutes=window_minutes)
        report = ScanReport(
            run_id=uuid.uuid4().hex[:12],  # write_run prefixes the timestamp itself
            started_at=now,
            adapter=getattr(self.adapter, "name", "hindsight"),
            dry_run=self.dry_run,
            mode="dedup-incremental",
        )
        report.ai = {
            "window_minutes": window_minutes,
            "window_start": since.isoformat(),
            "threshold": self.threshold,
            "boundary_low": self.boundary_low,
            "require_signature": self.require_signature,
            "max_actions": self.max_actions,
        }

        records = self.adapter.list_memories(since=since)
        report.records_scanned = len(records)
        if not records:
            report.ai["summary"] = {"scanned": 0, "actions": 0}
            report.finished_at = datetime.now(timezone.utc)
            report.run_file = str(self.ledger.write_run(report))
            return report

        vectors = self.embedder.embed([r.text for r in records])
        pairs = [(r.id, v) for r, v in zip(records, vectors) if v is not None]
        added = self.index.upsert(pairs)
        self.index.save()
        report.ai.update(
            {
                "embedded": len(pairs),
                "embed_failed": len(records) - len(pairs),
                "index_size": len(self.index),
                "index_added": added,
                "embed_calls": self.embedder.calls,
            }
        )

        cache: Dict[str, Optional[MemoryRecord]] = {r.id: r for r in records}
        acted: set = set()
        seen_pairs: set = set()
        uncertain = 0
        sig_blocked = 0
        duplicates = 0

        for rec, vec in zip(records, vectors):
            if vec is None or len(report.actions) >= self.max_actions:
                break
            for other_id, cos in self.index.knn(
                vec, top_k=self.top_k, exclude=[rec.id], min_cos=self.boundary_low
            ):
                if other_id in acted or other_id == rec.id:
                    continue
                pair_key = tuple(sorted((rec.id, other_id)))
                if pair_key in seen_pairs:
                    continue
                seen_pairs.add(pair_key)
                other = cache.get(other_id)
                if other is None:
                    other = self.adapter.get_memory(other_id)
                    cache[other_id] = other
                if other is None:
                    continue

                keep, drop = (rec, other) if self._is_newer(rec, other) else (other, rec)
                if drop.id in acted:
                    continue
                if self.require_signature and signature(keep.text) != signature(drop.text):
                    sig_blocked += 1
                    continue

                decision, why = self._judge(keep, drop, cos)
                if decision != "duplicate":
                    uncertain += 1
                    continue

                action = Action(
                    action="invalidate",
                    memory_id=drop.id,
                    policy="dedup",
                    reason=f"dup-of:{keep.id} {why}",
                    replacement_id=keep.id,
                    before=drop.to_dict(),
                )
                if not self.dry_run:
                    try:
                        self.adapter.apply(action)
                        action.applied = True
                    except Exception as exc:  # noqa: BLE001 - one failure must not stop the sweep
                        action.error = str(exc)
                report.actions.append(action)
                acted.add(drop.id)
                duplicates += 1
                if len(report.actions) >= self.max_actions:
                    report.warnings.append(
                        f"max_actions={self.max_actions} reached; remaining pairs skipped"
                    )
                    break

        report.ai["summary"] = {
            "scanned": len(records),
            "duplicates": duplicates,
            "uncertain": uncertain,
            "signature_blocked": sig_blocked,
            "applied": sum(1 for a in report.actions if a.applied),
            "errors": sum(1 for a in report.actions if a.error),
        }
        report.finished_at = datetime.now(timezone.utc)
        report.run_file = str(self.ledger.write_run(report))
        return report


def summarize(report: ScanReport) -> str:
    s = report.ai.get("summary", {}) if isinstance(report.ai, dict) else {}
    lines = [
        "## memgov 增量去重（dry-run）" if report.dry_run else "## memgov 增量去重",
        f"- 窗口 {report.ai.get('window_minutes')} 分钟，扫描 {report.records_scanned} 条",
        f"- 重复判定 {s.get('duplicates', 0)} 条，不确定 {s.get('uncertain', 0)} 条，"
        f"签名拦截 {s.get('signature_blocked', 0)} 条",
        f"- 已执行 {s.get('applied', 0)} 条，错误 {s.get('errors', 0)} 条",
        f"- run: {report.run_file}",
    ]
    if report.warnings:
        lines.append("- 警告: " + "; ".join(report.warnings))
    return "\n".join(lines)
