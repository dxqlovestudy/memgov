"""State-transition governance primitives (memgov v0.3).

A transition fact T declares that an entity went offline, moved, was renamed or
was replaced. The unit of judgement is T -> A, where A is an older *current-state*
assertion about the same entity. Candidates are found by entity scope, not by
text similarity: an old IP and a new domain often share no useful n-grams.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import yaml

from .models import Action, MemoryRecord

TRANSITION_PROMPT_VERSION = "transition-v1"
VERDICTS = {"obsolete_current", "historical_ok", "unrelated", "uncertain"}

TRANSITION_PATTERNS: Tuple[Tuple[str, str], ...] = (
    ("OFFLINE", r"下线|停用|禁用|退役|关闭|decommission(?:ed)?|disabled|offline"),
    ("MIGRATE", r"迁移|搬迁|切换(?:到|至|为)?|统一走|migrated|switched\s+to"),
    ("RENAME", r"改名|更名|renamed"),
    ("REPLACE", r"替换(?:为|成)?|取代|replaced"),
    ("SCOPE_CHANGE", r"移出|撤销|不再使用|停止使用|removed\s+from"),
)

NEGATION_RE = re.compile(
    r"(尚未|并未|没有|不会|不要|不应|无需|不需要|计划|准备|待|如果|假如|假设|预期|拟)"
)
DATE_RE = re.compile(r"(\d{4})[-年/](\d{1,2})[-月/](\d{1,2})日?")
IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
DOMAIN_RE = re.compile(
    r"\b(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"(?:com|cn|net|io|ai|org|dev|local|internal)\b",
    re.IGNORECASE,
)
PATH_RE = re.compile(r"(?<![\w:])/(?:[\w.\-]+/)+[\w.\-]*")

STATE_MARKERS: Tuple[str, ...] = (
    "入口", "位于", "在役", "在用", "可访问", "地址", "总入口", "运行在",
    "部署在", "指向", "静态目录", "承载", "当前", "访问方式", "端口",
)
HISTORICAL_MARKERS: Tuple[str, ...] = (
    "当时", "此前", "曾", "历史", "已下线", "已停用", "已迁移", "已替换",
    "实测于", "上线于", "原为", "原本", "过去",
)

DEFAULT_ALIASES: Dict[str, List[str]] = {
    "host:aether-vm": ["aether-vm", "172.20.12.116", "100.69.57.44", "Aether VM"],
    "env:dev": ["agent-dev.jlpay.com", "manage-dev.jlpay.com", "dev 环境", "dev环境"],
    "env:1092143": ["1092143", "1092143 联调环境", "VM 联调环境"],
    "service:partner-frontend": ["合伙人前端", "partner-web", "/srv/partner-web"],
    "path:/p/agent": ["/p/agent/", "/p/agent"],
    "service:hindsight": ["Hindsight", "hindsight"],
}


@dataclass(frozen=True)
class ScopeEntity:
    type: str
    canonical_id: str
    value: str


@dataclass
class TransitionFact:
    record: MemoryRecord
    transition_type: str
    entities: List[ScopeEntity]
    effective_at: Optional[datetime]
    scope_entities: List[ScopeEntity] = field(default_factory=list)

    @property
    def for_time(self) -> Optional[datetime]:
        return self.effective_at or self.record.created_at


@dataclass
class StateAssertion:
    record: MemoryRecord
    entities: List[ScopeEntity]
    predicate: str
    historical: bool = False


@dataclass
class TransitionCandidate:
    transition: TransitionFact
    assertion: StateAssertion
    entity: ScopeEntity
    score: float


@dataclass
class TransitionDecision:
    verdict: str
    scope_entity: str
    confidence: float
    evidence_old: str
    evidence_transition: str
    historical_markers: List[str]
    reason: str
    raw: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "verdict": self.verdict,
            "scope_entity": self.scope_entity,
            "confidence": self.confidence,
            "evidence_old": self.evidence_old,
            "evidence_transition": self.evidence_transition,
            "historical_markers": list(self.historical_markers),
            "reason": self.reason,
        }


def load_aliases(path: Optional[str] = None) -> Dict[str, List[str]]:
    aliases = {key: list(values) for key, values in DEFAULT_ALIASES.items()}
    if not path:
        return aliases
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    extra = data.get("aliases") if isinstance(data, dict) else None
    if not isinstance(extra, dict):
        return aliases
    for key, values in extra.items():
        if isinstance(values, str):
            values = [values]
        if isinstance(values, (list, tuple)):
            merged = aliases.setdefault(str(key), [])
            for value in values:
                if str(value).strip() and str(value) not in merged:
                    merged.append(str(value))
    return aliases


def _alias_lookup(aliases: Dict[str, List[str]]) -> List[Tuple[str, str, str]]:
    rows: List[Tuple[str, str, str]] = []
    for canonical, values in aliases.items():
        for value in values:
            rows.append((value.casefold(), canonical, value))
    rows.sort(key=lambda row: len(row[0]), reverse=True)
    return rows


def extract_entities(text: str, aliases: Optional[Dict[str, List[str]]] = None) -> List[ScopeEntity]:
    aliases = aliases or DEFAULT_ALIASES
    text = text or ""
    lower = text.casefold()
    found: Dict[str, ScopeEntity] = {}
    alias_values: Set[str] = set()
    for needle, canonical, value in _alias_lookup(aliases):
        if needle and needle in lower:
            kind = canonical.split(":", 1)[0]
            found.setdefault(canonical, ScopeEntity(kind, canonical, value))
            alias_values.add(value.casefold())
    for ip in IP_RE.findall(text):
        if ip.casefold() in alias_values:
            continue
        found.setdefault(f"host:{ip}", ScopeEntity("host", f"host:{ip}", ip))
    for domain in DOMAIN_RE.findall(text):
        if domain.casefold() in alias_values:
            continue
        found.setdefault(f"domain:{domain}", ScopeEntity("domain", f"domain:{domain}", domain))
    for path in PATH_RE.findall(text):
        if path.casefold() in alias_values:
            continue
        found.setdefault(f"path:{path}", ScopeEntity("path", f"path:{path}", path))
    return sorted(found.values(), key=lambda entity: entity.canonical_id)


def _effective_at(text: str, fallback: Optional[datetime]) -> Optional[datetime]:
    matches = list(DATE_RE.finditer(text or ""))
    if not matches:
        return fallback
    year, month, day = (int(part) for part in matches[-1].groups())
    try:
        return datetime(year, month, day, tzinfo=timezone.utc)
    except ValueError:
        return fallback


SOURCE_MARKERS: Tuple[str, ...] = (
    "下线", "停用", "禁用", "退役", "关闭", "迁移", "搬迁",
    "切换到", "切换至", "切换为", "统一走", "替换为", "改名", "更名",
)


def _source_entities(
    text: str,
    entities: List[ScopeEntity],
    aliases: Optional[Dict[str, List[str]]] = None,
) -> List[ScopeEntity]:
    """Entities before the transition verb are the subject; after it is the target.

    Example: "1092143 VM 联调环境已下线，统一走 dev" -> the source is 1092143/VM;
    dev is the destination and must not expand the candidate set.
    """
    best = None
    for marker in SOURCE_MARKERS:
        idx = text.find(marker)
        if idx > 0 and (best is None or idx < best[0]):
            best = (idx, marker)
    if best is not None:
        prefix = extract_entities(text[:best[0]], aliases)
        if prefix:
            return prefix
    return entities


def detect_transition(
    record: MemoryRecord,
    aliases: Optional[Dict[str, List[str]]] = None,
) -> Optional[TransitionFact]:
    text = record.text or ""
    for kind, pattern in TRANSITION_PATTERNS:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            prefix = text[max(0, match.start() - 16):match.start()]
            if NEGATION_RE.search(prefix):
                continue
            suffix = text[match.end():match.end() + 6]
            if suffix.lstrip().startswith(("吗", "？", "?")):
                continue
            entities = extract_entities(text, aliases)
            if not entities:
                continue
            return TransitionFact(
                record=record,
                transition_type=kind,
                entities=entities,
                effective_at=_effective_at(text, record.created_at),
                scope_entities=_source_entities(text, entities, aliases),
            )
    return None


def is_state_assertion(record: MemoryRecord) -> bool:
    text = record.text or ""
    if not text or detect_transition(record) is not None:
        return False
    if any(marker in text for marker in HISTORICAL_MARKERS):
        return False
    return any(marker in text for marker in STATE_MARKERS)


def build_assertion_index(
    records: Iterable[MemoryRecord],
    aliases: Optional[Dict[str, List[str]]] = None,
) -> Dict[str, List[StateAssertion]]:
    aliases = aliases or DEFAULT_ALIASES
    index: Dict[str, List[StateAssertion]] = {}
    for record in records:
        if record.state != "valid" or not is_state_assertion(record):
            continue
        entities = extract_entities(record.text, aliases)
        if not entities:
            continue
        predicate = next((marker for marker in STATE_MARKERS if marker in record.text), "-")
        assertion = StateAssertion(record=record, entities=entities, predicate=predicate)
        for entity in entities:
            index.setdefault(entity.canonical_id, []).append(assertion)
    return index


def rank_candidates(
    transitions: Sequence[TransitionFact],
    index: Dict[str, List[StateAssertion]],
    *,
    max_transitions: int = 10,
    max_candidates: int = 5,
    aliases: Optional[Dict[str, List[str]]] = None,
) -> List[TransitionCandidate]:
    ordered = sorted(
        transitions,
        key=lambda item: item.for_time.timestamp() if item.for_time else 0.0,
        reverse=True,
    )[:max_transitions]
    out: List[TransitionCandidate] = []
    for transition in ordered:
        per_assertion: Dict[str, TransitionCandidate] = {}
        transition_time = transition.for_time
        scope_entities = transition.scope_entities or transition.entities
        for entity in scope_entities:
            for assertion in index.get(entity.canonical_id, []):
                if assertion.record.id == transition.record.id:
                    continue
                created = assertion.record.created_at
                if transition_time and created and created.timestamp() >= transition_time.timestamp():
                    continue
                shared = len({e.canonical_id for e in scope_entities} &
                             {e.canonical_id for e in assertion.entities})
                if assertion.predicate in ("静态目录", "总入口"):
                    predicate_boost = 3.0
                elif assertion.predicate in ("承载", "位于"):
                    predicate_boost = 1.5
                elif assertion.predicate in ("地址", "访问方式"):
                    predicate_boost = 1.0
                elif assertion.predicate == "入口":
                    predicate_boost = 0.8
                elif assertion.predicate in ("端口", "在役", "在用", "可访问"):
                    predicate_boost = 0.3
                else:
                    predicate_boost = 0.1
                text_lower = (assertion.record.text or "").casefold()
                code_noise = -0.6 if re.search(
                    r"代码|提交|分支|镜像|测试|用例|mvn|\bsha\b|commit|pod", text_lower
                ) else 0.0
                alias_map = aliases or DEFAULT_ALIASES
                text_hits = sum(
                    1
                    for scope_entity in scope_entities
                    if any(
                        value.casefold() in text_lower
                        for value in alias_map.get(scope_entity.canonical_id, [scope_entity.value])
                    )
                )
                score = 1.0 + 0.2 * shared + predicate_boost + 0.15 * text_hits + code_noise
                candidate = TransitionCandidate(transition, assertion, entity, round(score, 3))
                current = per_assertion.get(assertion.record.id)
                if current is None or candidate.score > current.score:
                    per_assertion[assertion.record.id] = candidate
        ranked = sorted(
            per_assertion.values(),
            key=lambda item: (
                item.score,
                item.assertion.record.created_at.timestamp()
                if item.assertion.record.created_at else 0.0,
            ),
            reverse=True,
        )[:max_candidates]
        out.extend(ranked)
    return out


def collect_transitions(
    records: Iterable[MemoryRecord],
    aliases: Optional[Dict[str, List[str]]] = None,
) -> List[TransitionFact]:
    return [
        fact
        for record in records
        if (fact := detect_transition(record, aliases)) is not None
    ]


def parse_transition_decision(content: str, transition: TransitionFact, assertion: StateAssertion) -> TransitionDecision:
    from .ai import AIError, _norm_ws, _strip_fences

    try:
        import json

        raw = json.loads(_strip_fences(content))
    except Exception as exc:  # noqa: BLE001
        raise AIError(f"transition decision is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise AIError("transition decision must be a JSON object")
    verdict = str(raw.get("verdict") or "").strip()
    if verdict not in VERDICTS:
        raise AIError(f"invalid verdict: {verdict!r}")
    try:
        raw_confidence = raw.get("confidence")
        if raw_confidence is None:
            raise TypeError("confidence is missing")
        confidence = max(0.0, min(1.0, float(raw_confidence)))
    except (TypeError, ValueError) as exc:
        raise AIError("confidence must be a number") from exc
    scope_entity = str(raw.get("scope_entity") or "").strip()
    entity_ids = {entity.canonical_id for entity in (transition.scope_entities or transition.entities)}
    if scope_entity not in entity_ids:
        raise AIError(f"scope_entity is not a transition entity: {scope_entity!r}")
    evidence_old = str(raw.get("evidence_old") or "").strip()
    evidence_transition = str(raw.get("evidence_transition") or "").strip()
    if verdict in ("obsolete_current", "historical_ok"):
        if len(evidence_old) < 2 or _norm_ws(evidence_old) not in _norm_ws(assertion.record.text):
            raise AIError("evidence_old is not a substring of the old assertion")
        if len(evidence_transition) < 2 or _norm_ws(evidence_transition) not in _norm_ws(transition.record.text):
            raise AIError("evidence_transition is not a substring of the transition fact")
    markers = raw.get("historical_markers") or []
    if not isinstance(markers, list):
        raise AIError("historical_markers must be a list")
    reason = str(raw.get("reason") or "").strip()[:80]
    return TransitionDecision(
        verdict=verdict,
        scope_entity=scope_entity,
        confidence=confidence,
        evidence_old=evidence_old,
        evidence_transition=evidence_transition,
        historical_markers=[str(x) for x in markers if str(x).strip()],
        reason=reason,
        raw=raw,
    )


def _authoritative(record: MemoryRecord) -> bool:
    return (record.metadata or {}).get("authority") == "official" or "authority:official" in record.tags


def transition_decision_to_action(
    transition: TransitionFact,
    assertion: StateAssertion,
    decision: TransitionDecision,
    *,
    confidence_threshold: float,
) -> Tuple[Optional[Action], Optional[str]]:
    if decision.verdict != "obsolete_current":
        return None, decision.verdict
    if decision.confidence < confidence_threshold:
        return None, f"low_confidence:{decision.confidence:.2f}"
    if _authoritative(assertion.record):
        return None, "authority_conflict"
    transition_time = transition.for_time
    created = assertion.record.created_at
    if transition_time is None or created is None:
        return None, "missing_time"
    if created.timestamp() >= transition_time.timestamp():
        return None, "not_older"
    reason = (
        f"state_transition:{transition.record.id} conf={decision.confidence:.2f} "
        f"prompt={TRANSITION_PROMPT_VERSION} {decision.reason}"
    )[:300]
    return (
        Action(
            action="invalidate",
            memory_id=assertion.record.id,
            policy="state_transition",
            reason=reason,
            replacement_id=transition.record.id,
            before=assertion.record.to_dict(),
        ),
        None,
    )
