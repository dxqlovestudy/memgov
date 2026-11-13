from __future__ import annotations

from typing import Dict, List, Optional

from ..models import Action, MemoryRecord, jaccard
from ..policies import Capabilities
from .base import Adapter


class MemoryAdapter(Adapter):
    """In-memory adapter for tests, demos and policy development."""

    name = "memory"
    capabilities = Capabilities(
        invalidate=True, delete=True, archive=True, importance=True,
        usage_signals=True, recall=True,
    )

    def __init__(self, records: Optional[List[MemoryRecord]] = None):
        self.records: List[MemoryRecord] = list(records or [])

    def list_memories(self, limit: Optional[int] = None) -> List[MemoryRecord]:
        out = [r for r in self.records if r.state == "valid"]
        return out[:limit] if limit else out

    def apply(self, action: Action) -> bool:
        for rec in self.records:
            if rec.id != action.memory_id:
                continue
            if action.action in ("invalidate", "archive"):
                rec.state = "invalidated"
                rec.metadata["governance_reason"] = action.reason
            elif action.action == "delete":
                self.records.remove(rec)
            else:
                raise ValueError(f"unknown action: {action.action}")
            return True
        raise KeyError(f"memory not found: {action.memory_id}")

    def restore(self, memory_id: str) -> bool:
        for rec in self.records:
            if rec.id == memory_id:
                rec.state = "valid"
                rec.metadata.pop("governance_reason", None)
                return True
        raise KeyError(f"memory not found: {memory_id}")

    def recall(self, query: str, limit: int = 5) -> List[Dict]:
        scored = []
        for rec in self.records:
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
