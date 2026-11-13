from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


class Ledger:
    """Append-only-ish audit trail: one JSON run file + one JSONL action stream."""

    def __init__(self, runs_dir: str = "runs"):
        self.runs_dir = Path(runs_dir)
        self.runs_dir.mkdir(parents=True, exist_ok=True)

    def write_run(self, report, append_actions: bool = True) -> Path:
        payload = report.to_dict()
        stamp = report.started_at.strftime("%Y%m%d-%H%M%S")
        path = self.runs_dir / f"{stamp}-{report.run_id}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if not append_actions:
            return path
        with (self.runs_dir / "ledger.jsonl").open("a", encoding="utf-8") as fh:
            for action in report.actions:
                fh.write(
                    json.dumps(
                        {
                            "run_id": report.run_id,
                            "ts": datetime.now(timezone.utc).isoformat(),
                            "dry_run": report.dry_run,
                            "applied": action.applied,
                            **action.to_dict(),
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        return path

    @staticmethod
    def load_run(path: str) -> Dict[str, Any]:
        return json.loads(Path(path).read_text(encoding="utf-8"))

    def latest_run(self) -> Optional[Path]:
        runs = sorted(self.runs_dir.glob("*.json"))
        return runs[-1] if runs else None


def verify_forgetting(
    run: Dict[str, Any],
    recall_fn,
    *,
    min_similarity: float = 0.9,
    top: int = 5,
) -> Dict[str, Any]:
    """Check whether invalidated memories still come back from recall.

    Returns ``failure_rate`` (F) over the invalidate/archive actions in the run.
    A hit by id OR by near-identical text counts as a failure: backends may
    resurface derived copies of the same fact.
    """
    from .models import jaccard

    actions: List[Dict[str, Any]] = [
        a for a in run.get("actions", []) if a.get("action") in ("invalidate", "archive")
    ]
    checked = []
    failures = 0
    for action in actions:
        text = (action.get("before") or {}).get("text") or ""
        results = recall_fn(text, top)
        hit = False
        reason = ""
        for item in results:
            item_id = str(item.get("id") or item.get("memory_id") or "")
            if item_id and item_id == action.get("memory_id"):
                hit, reason = True, "same id"
                break
            if text and jaccard(text, str(item.get("text") or "")) >= min_similarity:
                hit, reason = True, "near-identical text"
                break
        if hit:
            failures += 1
        checked.append(
            {
                "memory_id": action.get("memory_id"),
                "policy": action.get("policy"),
                "still_recalled": hit,
                "reason": reason,
                "returned": len(results),
            }
        )
    total = len(checked)
    return {
        "checked": total,
        "failures": failures,
        "failure_rate": round(failures / total, 3) if total else 0.0,
        "min_similarity": min_similarity,
        "items": checked,
    }
