"""AI-assisted judgement for memory governance.

The model is a **proposer**: it returns a structured decision with exact evidence
quotes. This module validates the decision against the pair and the engine's safety
rules before it can become an Action. It never calls the backend.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .models import Action, MemoryRecord
from .prompts import PROMPT_VERSION, messages

SUSPICIOUS_RE = re.compile(
    r"(ignore|disregard).{0,24}(previous|above|all).{0,24}instructions"
    r"|忽略(以上|之前|所有|前面).{0,8}(指令|要求|提示)"
    r"|你现在是.{0,12}(系统|管理员|root)"
    r"|system\s+prompt",
    re.IGNORECASE,
)

DECISIONS = {"keep_both", "invalidate_a", "invalidate_b", "uncertain"}
RELATIONS = {"supersede", "contradiction", "duplicate", "none"}


class AIError(RuntimeError):
    pass


@dataclass
class AIConfig:
    base_url: str
    api_key: str
    model: str
    temperature: float = 0.0
    timeout: int = 120
    max_tokens: int = 1200
    confidence_threshold: float = 0.85
    max_pairs: int = 100
    max_invalidations: int = 10
    min_candidate_score: float = 0.22
    auto_rollback: bool = True
    verify_failure_threshold: float = 0.1
    cache_path: Optional[str] = None
    prompt_version: str = PROMPT_VERSION


@dataclass
class AIDecision:
    decision: str
    relation: str
    keep_id: Optional[str]
    invalidate_id: Optional[str]
    confidence: float
    evidence: List[Dict[str, str]]
    reason: str
    raw: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "decision": self.decision,
            "relation": self.relation,
            "keep_id": self.keep_id,
            "invalidate_id": self.invalidate_id,
            "confidence": self.confidence,
            "evidence": list(self.evidence),
            "reason": self.reason,
        }


class OpenAICompatibleClient:
    """Minimal chat-completions client using only the standard library."""

    def __init__(self, config: AIConfig):
        self.config = config

    def chat(self, msgs: List[Dict[str, str]]) -> Tuple[str, Dict[str, int]]:
        url = self.config.base_url.rstrip("/") + "/chat/completions"
        base = {
            "model": self.config.model,
            "messages": msgs,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
        }
        attempts: List[Dict[str, Any]] = []
        with_format = dict(base, response_format={"type": "json_object"})
        attempts.append(with_format)
        attempts.append(base)
        last_error: Optional[Exception] = None
        for attempt in attempts:
            for retry in range(2):
                try:
                    return self._post(url, attempt)
                except AIError as exc:
                    last_error = exc
                    text = str(exc)
                    if "response_format" in text and attempt is with_format:
                        break
                    if any(code in text for code in ("429", "500", "502", "503", "504")):
                        time.sleep(1 + retry)
                        continue
                    raise
        raise last_error or AIError("chat request failed")

    def _post(self, url: str, body: Dict[str, Any]) -> Tuple[str, Dict[str, int]]:
        req = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8"), method="POST"
        )
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", f"Bearer {self.config.api_key}")
        try:
            with urllib.request.urlopen(req, timeout=self.config.timeout) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            raise AIError(f"HTTP {exc.code}: {detail}") from exc
        except Exception as exc:  # noqa: BLE001
            raise AIError(f"request failed: {exc}") from exc

        choices = data.get("choices") or []
        if not choices:
            raise AIError("no choices in response")
        content = (choices[0].get("message") or {}).get("content") or ""
        usage = data.get("usage") or {}
        return content, {
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
            "total_tokens": int(usage.get("total_tokens") or 0),
        }


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start : end + 1]
    return text


def _norm_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def parse_decision(content: str, a: MemoryRecord, b: MemoryRecord) -> AIDecision:
    try:
        raw = json.loads(_strip_fences(content))
    except json.JSONDecodeError as exc:
        raise AIError(f"decision is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise AIError("decision must be a JSON object")

    decision = str(raw.get("decision") or "").strip()
    relation = str(raw.get("relation") or "none").strip()
    if decision not in DECISIONS:
        raise AIError(f"invalid decision: {decision!r}")
    if relation not in RELATIONS:
        raise AIError(f"invalid relation: {relation!r}")

    keep_id = raw.get("keep_id")
    invalidate_id = raw.get("invalidate_id")
    keep_id = str(keep_id) if keep_id else None
    invalidate_id = str(invalidate_id) if invalidate_id else None

    raw_confidence = raw.get("confidence")
    if raw_confidence is None:
        raise AIError("confidence is required")
    try:
        confidence = float(raw_confidence)
    except (TypeError, ValueError) as exc:
        raise AIError("confidence must be a number") from exc
    confidence = max(0.0, min(1.0, confidence))

    evidence = raw.get("evidence") or []
    if not isinstance(evidence, list):
        raise AIError("evidence must be a list")
    clean_evidence: List[Dict[str, str]] = []
    for item in evidence:
        if not isinstance(item, dict):
            raise AIError("evidence item must be an object")
        eid = str(item.get("id") or "")
        quote = str(item.get("quote") or "")
        if eid not in (a.id, b.id):
            raise AIError(f"evidence references unknown id: {eid!r}")
        if len(quote.strip()) < 2:
            raise AIError("evidence quote is empty")
        source = a if eid == a.id else b
        if _norm_ws(quote) not in _norm_ws(source.text):
            raise AIError("evidence quote is not a substring of the record")
        clean_evidence.append({"id": eid, "quote": quote})

    if decision in ("invalidate_a", "invalidate_b"):
        expected_invalidate = a.id if decision == "invalidate_a" else b.id
        expected_keep = b.id if decision == "invalidate_a" else a.id
        if invalidate_id != expected_invalidate or keep_id != expected_keep:
            raise AIError("keep_id/invalidate_id do not match the decision")
        if relation == "none":
            raise AIError("an invalidation requires a non-none relation")
        if not any(e["id"] == invalidate_id for e in clean_evidence):
            raise AIError("evidence must quote the invalidated record")
        if relation == "contradiction" and not any(e["id"] == keep_id for e in clean_evidence):
            raise AIError("contradiction evidence must quote both records")
    else:
        keep_id = None
        invalidate_id = None

    reason = str(raw.get("reason") or "").strip()[:200]
    return AIDecision(
        decision=decision,
        relation=relation,
        keep_id=keep_id,
        invalidate_id=invalidate_id,
        confidence=confidence,
        evidence=clean_evidence,
        reason=reason,
        raw=raw,
    )


def _authoritative(rec: MemoryRecord) -> bool:
    return (rec.metadata or {}).get("authority") == "official" or "authority:official" in rec.tags


def decision_to_action(
    a: MemoryRecord,
    b: MemoryRecord,
    decision: AIDecision,
    *,
    confidence_threshold: float,
) -> Tuple[Optional[Action], Optional[str]]:
    """Apply engine-level rules on top of the model decision.

    Returns ``(action, rejection_reason)``; exactly one is non-None.
    """
    if decision.decision in ("keep_both", "uncertain"):
        return None, decision.decision
    if decision.confidence < confidence_threshold:
        return None, f"low_confidence:{decision.confidence:.2f}"
    if decision.invalidate_id == a.id:
        invalidated, kept = a, b
    elif decision.invalidate_id == b.id:
        invalidated, kept = b, a
    else:
        return None, "unknown_invalidate_id"

    if invalidated.created_at and kept.created_at and kept.created_at < invalidated.created_at:
        if not _authoritative(kept):
            return None, "keep_older_without_authority"

    reason = (
        f"ai:{decision.relation} conf={decision.confidence:.2f} "
        f"prompt={PROMPT_VERSION} {decision.reason}"
    )[:300]
    return (
        Action(
            action="invalidate",
            memory_id=invalidated.id,
            policy="ai",
            reason=reason,
            replacement_id=kept.id,
            before=invalidated.to_dict(),
        ),
        None,
    )


class AICache:
    """Content-addressed cache so repeated runs do not pay twice for the same pair."""

    def __init__(self, path: Optional[str]):
        self.path = path
        self.data: Dict[str, Any] = {}
        if path and os.path.exists(path):
            try:
                self.data = json.loads(open(path, encoding="utf-8").read())
            except (OSError, json.JSONDecodeError):
                self.data = {}

    @staticmethod
    def key(a: MemoryRecord, b: MemoryRecord) -> str:
        payload = "|".join([PROMPT_VERSION, a.id, b.id, a.text, b.text])
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        return self.data.get(key)

    def put(self, key: str, value: Dict[str, Any]) -> None:
        self.data[key] = value
        if not self.path:
            return
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, ensure_ascii=False)
        os.replace(tmp, self.path)


class AIJudge:
    def __init__(self, config: AIConfig, client=None, cache: Optional[AICache] = None):
        self.config = config
        self.client = client or OpenAICompatibleClient(config)
        self.cache = cache if cache is not None else AICache(config.cache_path)
        self.calls = 0
        self.cached = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0

    def suspicious(self, a: MemoryRecord, b: MemoryRecord) -> bool:
        return bool(SUSPICIOUS_RE.search(a.text or "") or SUSPICIOUS_RE.search(b.text or ""))

    def judge(self, a: MemoryRecord, b: MemoryRecord) -> Tuple[AIDecision, bool]:
        if self.suspicious(a, b):
            return (
                AIDecision(
                    decision="uncertain",
                    relation="none",
                    keep_id=None,
                    invalidate_id=None,
                    confidence=0.0,
                    evidence=[],
                    reason="suspicious_content",
                ),
                True,
            )

        key = self.cache.key(a, b)
        hit = self.cache.get(key)
        if hit is not None:
            self.cached += 1
            return parse_decision(json.dumps(hit, ensure_ascii=False), a, b), True

        content, usage = self.client.chat(messages(a, b))
        self.calls += 1
        self.prompt_tokens += usage.get("prompt_tokens", 0)
        self.completion_tokens += usage.get("completion_tokens", 0)
        decision = parse_decision(content, a, b)
        self.cache.put(key, decision.to_dict())
        return decision, False

    def usage(self) -> Dict[str, int]:
        return {
            "calls": self.calls,
            "cached": self.cached,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.prompt_tokens + self.completion_tokens,
        }
