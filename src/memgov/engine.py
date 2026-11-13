from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import List, Optional

from .adapters.base import Adapter
from .ledger import Ledger
from .models import Action, ScanReport
from .policies import Policy, PolicyContext


class Engine:
    """Evaluate policies against one backend snapshot, then optionally apply."""

    def __init__(
        self,
        adapter: Adapter,
        policies: List[Policy],
        ledger: Optional[Ledger] = None,
        dry_run: bool = True,
        max_records: Optional[int] = None,
    ):
        self.adapter = adapter
        self.policies = policies
        self.ledger = ledger or Ledger()
        self.dry_run = dry_run
        self.max_records = max_records

    def scan(self) -> ScanReport:
        started = datetime.now(timezone.utc)
        report = ScanReport(
            run_id=uuid.uuid4().hex[:12],
            started_at=started,
            adapter=self.adapter.name,
            dry_run=self.dry_run,
        )
        records = self.adapter.list_memories(limit=self.max_records)
        report.records_scanned = len(records)
        ctx = PolicyContext(now=started, capabilities=self.adapter.capabilities, dry_run=self.dry_run)

        claimed = {}
        for policy in self.policies:
            missing = [r for r in policy.requires if not getattr(ctx.capabilities, r, False)]
            if missing:
                report.warnings.append(
                    f"policy '{policy.name}' skipped: backend lacks {','.join(missing)}"
                )
                continue
            for action in policy.evaluate(records, ctx):
                if action.memory_id in claimed:
                    report.warnings.append(
                        f"policy '{policy.name}' wanted {action.memory_id} "
                        f"already claimed by '{claimed[action.memory_id]}'"
                    )
                    continue
                action = self._normalize(action, report)
                if action is None:
                    continue
                claimed[action.memory_id] = action.policy
                report.actions.append(action)

        report.finished_at = datetime.now(timezone.utc)
        return report

    def _normalize(self, action: Action, report: ScanReport) -> Optional[Action]:
        caps = self.adapter.capabilities
        if action.action == "archive" and not caps.archive:
            action.action = "invalidate"
            action.reason = "archive:" + action.reason
        if action.action == "delete" and not caps.delete:
            report.warnings.append(
                f"delete skipped for {action.memory_id}: backend has no delete capability"
            )
            return None
        if action.action not in ("invalidate", "archive", "delete"):
            report.warnings.append(f"unknown action {action.action} skipped")
            return None
        return action

    def apply(self, report: ScanReport) -> ScanReport:
        if report.dry_run:
            return report
        for action in report.actions:
            try:
                action.applied = bool(self.adapter.apply(action))
            except Exception as exc:  # noqa: BLE001 - recorded per action
                action.applied = False
                action.error = str(exc)[:500]
        return report

    def run(self) -> ScanReport:
        report = self.scan()
        self.apply(report)
        report.run_file = str(self.ledger.write_run(report))
        return report
