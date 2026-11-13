from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..models import Action, MemoryRecord, parse_dt
from ..policies import Capabilities
from .base import Adapter


def record_from_dict(raw: Dict[str, Any]) -> MemoryRecord:
    return MemoryRecord(
        id=str(raw.get("id") or raw.get("memory_id") or ""),
        text=str(raw.get("text") or ""),
        kind=str(raw.get("kind") or raw.get("fact_type") or "fact"),
        tags=list(raw.get("tags") or []),
        created_at=parse_dt(raw.get("created_at") or raw.get("date")),
        updated_at=parse_dt(raw.get("updated_at")),
        state=str(raw.get("state") or "valid"),
        importance=float(raw.get("importance") or 0.5),
        use_count=int(raw.get("use_count") or 0),
        last_used_at=parse_dt(raw.get("last_used_at")),
        supersedes=list(raw.get("supersedes") or []),
        metadata=dict(raw.get("metadata") or {}),
    )


class JsonFileAdapter(Adapter):
    """A file-backed adapter for offline evaluation and CI demos.

    File shape: ``{"records": [ {id, text, kind, tags, created_at, ...} ]}``
    or a bare list of records.
    """

    name = "jsonfile"
    capabilities = Capabilities(
        invalidate=True, delete=True, archive=True, importance=True,
        usage_signals=True, recall=True,
    )

    def __init__(self, path: str):
        self.path = Path(path)
        self._records: List[MemoryRecord] = []
        self._load()

    def _load(self) -> None:
        data = json.loads(self.path.read_text(encoding="utf-8"))
        items = data.get("records") if isinstance(data, dict) else data
        self._records = [record_from_dict(x) for x in (items or [])]

    def _save(self) -> None:
        payload = {"records": [r.to_dict() for r in self._records]}
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    def list_memories(self, limit: Optional[int] = None) -> List[MemoryRecord]:
        out = [r for r in self._records if r.state == "valid"]
        return out[:limit] if limit else out

    def apply(self, action: Action) -> bool:
        for rec in self._records:
            if rec.id != action.memory_id:
                continue
            if action.action in ("invalidate", "archive"):
                rec.state = "invalidated"
                rec.metadata["governance_reason"] = action.reason
            elif action.action == "delete":
                self._records.remove(rec)
            else:
                raise ValueError(f"unknown action: {action.action}")
            self._save()
            return True
        raise KeyError(f"memory not found: {action.memory_id}")

    def restore(self, memory_id: str) -> bool:
        for rec in self._records:
            if rec.id == memory_id:
                rec.state = "valid"
                rec.metadata.pop("governance_reason", None)
                self._save()
                return True
        raise KeyError(f"memory not found: {memory_id}")

    def recall(self, query: str, limit: int = 5) -> List[Dict]:
        from ..models import jaccard

        scored = []
        for rec in self._records:
            if rec.state != "valid":
                continue
            score = jaccard(query, rec.text)
            if score > 0:
                scored.append((score, rec))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [
            {"id": rec.id, "text": rec.text, "scores": {"final": score}}
            for score, rec in scored[:limit]
        ]
