from __future__ import annotations

from typing import Any, Dict

from .models import ScanReport


def render_markdown(report: ScanReport, limit: int = 100) -> str:
    lines = [
        "# memgov scan report",
        "",
        f"- run: `{report.run_id}`",
        f"- adapter: `{report.adapter}`",
        f"- mode: **{'dry-run' if report.dry_run else 'apply'}**",
        f"- records scanned: {report.records_scanned}",
        f"- actions: {len(report.actions)}",
        "",
        "## Counts",
        "",
        "| key | count |",
        "|---|---|",
    ]
    for key, value in sorted(report.counts().items()):
        lines.append(f"| {key} | {value} |")

    applied = sum(1 for a in report.actions if a.applied)
    errors = [a for a in report.actions if a.error]
    lines += [
        "",
        f"applied: {applied} / {len(report.actions)}; errors: {len(errors)}",
        "",
        "## Actions",
        "",
        "| action | policy | memory | reason | applied |",
        "|---|---|---|---|---|",
    ]
    for action in report.actions[:limit]:
        reason = action.reason.replace("|", "/")
        lines.append(
            f"| {action.action} | {action.policy} | `{action.memory_id[:12]}` | {reason} | "
            f"{'yes' if action.applied else 'no'} |"
        )
    if len(report.actions) > limit:
        lines.append(f"| … | | | {len(report.actions) - limit} more | |")

    if report.warnings:
        lines += ["", "## Warnings", ""]
        lines += [f"- {w}" for w in report.warnings]

    return "\n".join(lines) + "\n"


def summary(report: ScanReport) -> Dict[str, Any]:
    return {
        "run_id": report.run_id,
        "adapter": report.adapter,
        "dry_run": report.dry_run,
        "records_scanned": report.records_scanned,
        "actions": len(report.actions),
        "counts": report.counts(),
        "applied": sum(1 for a in report.actions if a.applied),
        "errors": sum(1 for a in report.actions if a.error),
        "warnings": report.warnings,
    }
