from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def parse_dt(value: Any) -> Optional[datetime]:
    """Parse ISO-8601 (with or without Z) into an aware UTC datetime."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(text[: len(fmt) + 2], fmt)
                break
            except ValueError:
                continue
        else:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def norm_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").lower()
    return re.sub(r"[\s\W_]+", "", text)


def bigrams(text: str) -> set:
    n = norm_text(text)
    if len(n) < 2:
        return {n} if n else set()
    return {n[i : i + 2] for i in range(len(n) - 1)}


def jaccard(a: str, b: str) -> float:
    ga, gb = bigrams(a), bigrams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


@dataclass
class MemoryRecord:
    """Backend-neutral view of one memory."""

    id: str
    text: str
    kind: str = "fact"
    tags: List[str] = field(default_factory=list)
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    state: str = "valid"
    importance: float = 0.5
    use_count: int = 0
    last_used_at: Optional[datetime] = None
    supersedes: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def reference_time(self) -> Optional[datetime]:
        return self.updated_at or self.created_at or self.last_used_at

    def age_days(self, now: datetime) -> float:
        ref = self.reference_time()
        if ref is None:
            return 0.0
        return max(0.0, (now - ref).total_seconds() / 86400.0)

    def effective_score(self, now: datetime, half_life_days: float = 90.0) -> float:
        """Importance decayed by age, slightly boosted by usage.

        Not a truth score: it only orders records inside a governance policy.
        """
        age = self.age_days(now)
        decay = 0.5 ** (age / max(half_life_days, 0.1))
        usage = 1.0 + 0.1 * math.log1p(max(self.use_count, 0))
        return round(max(0.0, self.importance) * decay * usage, 6)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "kind": self.kind,
            "tags": list(self.tags),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "state": self.state,
            "importance": self.importance,
            "use_count": self.use_count,
            "last_used_at": self.last_used_at.isoformat() if self.last_used_at else None,
            "supersedes": list(self.supersedes),
            "metadata": dict(self.metadata),
        }

    def fingerprint(self) -> str:
        return hashlib.sha256(norm_text(self.text).encode("utf-8")).hexdigest()[:16]


@dataclass
class Action:
    """A proposed or applied governance mutation."""

    action: str  # invalidate | delete | archive
    memory_id: str
    policy: str
    reason: str
    replacement_id: Optional[str] = None
    before: Dict[str, Any] = field(default_factory=dict)
    reversible: bool = True
    applied: bool = False
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action,
            "memory_id": self.memory_id,
            "policy": self.policy,
            "reason": self.reason,
            "replacement_id": self.replacement_id,
            "before": self.before,
            "reversible": self.reversible,
            "applied": self.applied,
            "error": self.error,
        }


@dataclass
class ScanReport:
    run_id: str
    started_at: datetime
    adapter: str
    dry_run: bool
    records_scanned: int = 0
    actions: List[Action] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    policy_path: Optional[str] = None
    finished_at: Optional[datetime] = None
    run_file: Optional[str] = None

    def counts(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for a in self.actions:
            key = a.policy if a.action == "invalidate" else a.action
            out[key] = out.get(key, 0) + 1
        return out

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "adapter": self.adapter,
            "dry_run": self.dry_run,
            "policy_path": self.policy_path,
            "records_scanned": self.records_scanned,
            "counts": self.counts(),
            "warnings": list(self.warnings),
            "run_file": self.run_file,
            "actions": [a.to_dict() for a in self.actions],
        }
