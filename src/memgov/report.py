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

    if report.mode == "ai":
        ai = report.ai or {}
        lines += [
            "",
            "## AI judge",
            "",
            f"- model: `{ai.get('model', '-')}` (prompt `{ai.get('prompt_version', '-')}`)",
            f"- candidate pairs: {ai.get('candidate_pairs', 0)}; judged: {ai.get('judged_pairs', 0)}",
            f"- confidence threshold: {ai.get('confidence_threshold', '-')}",
            f"- calls: {ai.get('calls', 0)} (cached {ai.get('cached', 0)}); "
            f"tokens: {ai.get('total_tokens', 0)}",
        ]
        if ai.get("rejected"):
            lines.append(f"- rejected: `{ai['rejected']}`")
        if ai.get("errors"):
            lines.append(f"- errors: {len(ai['errors'])} (see run JSON)")

    if report.verification:
        v = report.verification
        lines += [
            "",
            "## Forgetting verification",
            "",
            f"- checked: {v.get('checked', 0)}; failures: {v.get('failures', 0)}; "
            f"failure rate: {v.get('failure_rate', 0):.2f}",
        ]
    if report.rolled_back:
        lines += ["", "> **auto-rollback triggered**: applied actions were restored."]

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
