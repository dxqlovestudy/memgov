from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional

from .adapters.base import Adapter
from .ai import AICache, AIConfig, AIError, AIJudge, decision_to_action
from .candidates import candidate_pairs
from .engine import Engine
from .ledger import Ledger, verify_forgetting
from .models import ScanReport


class AIGovernor:
    """Full-AI governance run: candidates -> model judgement -> actions -> verify.

    This is the only component allowed to turn model output into actions. It keeps
    four machine gates (schema/evidence validation, confidence, keep-newer/authority,
    per-run caps) and verifies the outcome after applying. No human approval step.
    """

    def __init__(
        self,
        adapter: Adapter,
        config: AIConfig,
        ledger: Optional[Ledger] = None,
        dry_run: bool = True,
        max_records: Optional[int] = None,
        candidates_only: bool = False,
        judge: Optional[AIJudge] = None,
    ):
        self.adapter = adapter
        self.config = config
        self.ledger = ledger or Ledger()
        self.dry_run = dry_run
        self.max_records = max_records
        self.candidates_only = candidates_only
        self.judge = judge or AIJudge(
            config, cache=AICache(config.cache_path) if config.cache_path else AICache(None)
        )

    def run(self) -> ScanReport:
        started = datetime.now(timezone.utc)
        report = ScanReport(
            run_id=uuid.uuid4().hex[:12],
            started_at=started,
            adapter=self.adapter.name,
            dry_run=self.dry_run,
            mode="ai",
        )
        records = self.adapter.list_memories(limit=self.max_records)
        report.records_scanned = len(records)
        pairs = candidate_pairs(
            records,
            max_pairs=self.config.max_pairs,
            min_score=self.config.min_candidate_score,
        )
        report.ai = {
            "model": self.config.model,
            "prompt_version": self.config.prompt_version,
            "confidence_threshold": self.config.confidence_threshold,
            "candidate_pairs": len(pairs),
        }

        if self.candidates_only:
            report.ai["candidates"] = [
                {
                    "score": p.score,
                    "a_id": p.a.id,
                    "a_text": p.a.text[:120],
                    "b_id": p.b.id,
                    "b_text": p.b.text[:120],
                    "shared_anchors": p.shared_anchors,
                }
                for p in pairs
            ]
            report.finished_at = datetime.now(timezone.utc)
            report.run_file = str(self.ledger.write_run(report))
            return report

        claimed: Dict[str, str] = {}
        rejected: Dict[str, int] = {}
        errors: List[str] = []
        attempted = 0
        for pair in pairs:
            if len(report.actions) >= self.config.max_invalidations:
                report.ai["cap_reached"] = True
                break
            attempted += 1
            try:
                decision, _cached = self.judge.judge(pair.a, pair.b)
            except AIError as exc:
                errors.append(f"{pair.key()}: {exc}")
                continue
            action, reason = decision_to_action(
                pair.a,
                pair.b,
                decision,
                confidence_threshold=self.config.confidence_threshold,
            )
            if action is None:
                key = (reason or "rejected").split(":", 1)[0]
                rejected[key] = rejected.get(key, 0) + 1
                continue
            if action.memory_id in claimed:
                rejected["already_claimed"] = rejected.get("already_claimed", 0) + 1
                continue
            claimed[action.memory_id] = pair.key()
            report.actions.append(action)

        report.ai.update(self.judge.usage())
        report.ai["judged_pairs"] = attempted - len(errors)
        report.ai["rejected"] = rejected
        report.ai["errors"] = errors[:20]
        report.finished_at = datetime.now(timezone.utc)

        if not self.dry_run and report.actions:
            Engine(self.adapter, [], self.ledger, dry_run=False).apply(report)
            self._verify_and_maybe_rollback(report)

        report.run_file = str(self.ledger.write_run(report))
        return report

    def _verify_and_maybe_rollback(self, report: ScanReport) -> None:
        applied = [a for a in report.actions if a.applied]
        if not applied:
            return
        try:
            result = verify_forgetting(
                report.to_dict(),
                lambda q, n: self.adapter.recall(q, n),
                top=5,
            )
        except Exception as exc:  # noqa: BLE001
            report.ai["verification_error"] = str(exc)[:300]
            return
        report.verification = result
        if result["failure_rate"] <= self.config.verify_failure_threshold:
            return
        if not self.config.auto_rollback:
            return

        rollback_errors: List[str] = []
        for action in applied:
            try:
                self.adapter.restore(action.memory_id)
                action.applied = False
                action.error = "auto_rollback"
            except Exception as exc:  # noqa: BLE001
                rollback_errors.append(f"{action.memory_id}: {exc}")
        report.rolled_back = True
        report.ai["rollback_errors"] = rollback_errors
        report.warnings.append(
            "auto-rollback: forgetting failure rate "
            f"{result['failure_rate']:.2f} > {self.config.verify_failure_threshold:.2f}"
        )


def summarize_candidates(report: ScanReport) -> str:
    rows = (report.ai or {}).get("candidates") or []
    lines = [
        f"# candidate pairs ({len(rows)})",
        "",
        "| score | A | B | shared anchors |",
        "|---|---|---|---|",
    ]
    for row in rows:
        lines.append(
            f"| {row['score']} | `{row['a_id'][:12]}` {row['a_text'][:40]} "
            f"| `{row['b_id'][:12]}` {row['b_text'][:40]} "
            f"| {', '.join(row['shared_anchors']) or '-'} |"
        )
    return "\n".join(lines) + "\n"
