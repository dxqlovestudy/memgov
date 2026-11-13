from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

from ..models import Action, MemoryRecord, parse_dt
from ..policies import Capabilities
from .base import Adapter


class HindsightAdapter(Adapter):
    """Hindsight REST adapter (API 1.x).

    Safety rails:
    - ``list_memories`` hides derived ``observation`` records: Hindsight rejects
      curation of observations (400), so only ``world`` / ``experience`` facts
      are exposed.
    - Writes require ``allow_write=True`` *and* either the constructor flag or
      ``MEMGOV_ALLOW_WRITE=1``; dry-run is the default everywhere else.
    """

    name = "hindsight"
    capabilities = Capabilities(
        invalidate=True, delete=False, archive=False, importance=False,
        usage_signals=False, recall=True,
    )

    def __init__(
        self,
        base_url: str,
        bank: str,
        timeout: int = 30,
        allow_write: bool = False,
        allow_delete: bool = False,
        page_size: int = 500,
        include_kinds: Optional[List[str]] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.bank = bank
        self.timeout = timeout
        self.allow_write = allow_write or os.environ.get("MEMGOV_ALLOW_WRITE") == "1"
        self.allow_delete = allow_delete
        self.page_size = max(1, min(page_size, 500))
        self.include_kinds = include_kinds or ["world", "experience"]
        self.capabilities.delete = allow_delete

    def _request(self, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Any:
        url = self.base_url + path
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            raise RuntimeError(f"HTTP {exc.code} {method} {path}: {detail}") from exc

    def _bank_path(self, suffix: str) -> str:
        return f"/v1/default/banks/{urllib.parse.quote(self.bank)}{suffix}"

    def list_memories(self, limit: Optional[int] = None) -> List[MemoryRecord]:
        out: List[MemoryRecord] = []
        offset = 0
        while True:
            page = self.page_size
            if limit is not None:
                page = min(page, limit - len(out))
                if page <= 0:
                    break
            query = urllib.parse.urlencode(
                {"state": "valid", "limit": page, "offset": offset, "time_field": "updated_at"}
            )
            data = self._request("GET", self._bank_path(f"/memories/list?{query}"))
            items = data.get("items") or []
            total = int(data.get("total") or 0)
            for item in items:
                kind = str(item.get("fact_type") or "")
                if kind and kind not in self.include_kinds:
                    continue
                out.append(self._to_record(item))
                if limit is not None and len(out) >= limit:
                    return out
            offset += len(items)
            if len(items) < page or (total and offset >= total):
                break
        return out

    @staticmethod
    def _as_list(value: Any) -> List[str]:
        if value is None or value == "":
            return []
        if isinstance(value, (list, tuple, set)):
            return [str(x).strip() for x in value if str(x).strip()]
        return [x.strip() for x in str(value).split(",") if x.strip()]

    @staticmethod
    def _to_record(item: Dict[str, Any]) -> MemoryRecord:
        meta = item.get("metadata") or {}
        return MemoryRecord(
            id=str(item.get("id") or ""),
            text=str(item.get("text") or ""),
            kind=str(item.get("fact_type") or "fact"),
            tags=list(item.get("tags") or []),
            created_at=parse_dt(item.get("date") or item.get("mentioned_at")),
            updated_at=parse_dt(item.get("updated_at")),
            state=str(item.get("state") or "valid"),
            importance=float(meta.get("importance") or 0.5),
            use_count=int(meta.get("use_count") or 0),
            last_used_at=parse_dt(meta.get("last_used_at")),
            supersedes=HindsightAdapter._as_list(meta.get("supersedes")),
            metadata=dict(meta),
        )

    def _require_write(self) -> None:
        if not self.allow_write:
            raise PermissionError(
                "refusing to mutate Hindsight without allow_write=True "
                "and MEMGOV_ALLOW_WRITE=1"
            )

    def apply(self, action: Action) -> bool:
        self._require_write()
        if action.action in ("invalidate", "archive"):
            body = {"state": "invalidated", "reason": action.reason}
            self._request(
                "PATCH", self._bank_path(f"/memories/{action.memory_id}"), body
            )
            return True
        if action.action == "delete":
            if not self.allow_delete:
                raise PermissionError("delete is disabled for this adapter")
            self._request("DELETE", self._bank_path(f"/memories/{action.memory_id}"))
            return True
        raise ValueError(f"unknown action: {action.action}")

    def restore(self, memory_id: str) -> bool:
        self._require_write()
        self._request(
            "PATCH",
            self._bank_path(f"/memories/{memory_id}"),
            {"state": "valid", "reason": "memgov rollback"},
        )
        return True

    def recall(self, query: str, limit: int = 5) -> List[Dict]:
        data = self._request(
            "POST", self._bank_path("/memories/recall"), {"query": query, "max_tokens": 800}
        )
        return list(data.get("results") or [])[:limit]
