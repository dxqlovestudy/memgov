"""Full-auto state-transition governance (memgov v0.3, dry-run by default)."""
from __future__ import annotations

import hashlib
import json
import uuid
from collections import Counter
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from .adapters.base import Adapter
from .ai import SUSPICIOUS_RE, AICache, AIConfig, AIError, OpenAICompatibleClient
from .engine import Engine
from .ledger import Ledger, verify_forgetting
from .models import ScanReport
from .prompts import TRANSITION_PROMPT_VERSION, transition_messages
from .transitions import (
    TransitionDecision,
    TransitionFact,
    build_assertion_index,
    collect_transitions,
    load_aliases,
    parse_transition_decision,
    rank_candidates,
    transition_decision_to_action,
)


class TransitionJudge:
    """Judge one T -> A pair with transition-v1 and validate the JSON contract."""

    def __init__(self, config: AIConfig, client=None, cache: Optional[AICache] = None):
        self.config = config
        self.client = client or OpenAICompatibleClient(config)
        self.cache = cache if cache is not None else AICache(config.cache_path)
        self.calls = 0
        self.cached = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0

    @staticmethod
    def _cache_key(transition: TransitionFact, assertion, entity) -> str:
        payload = "|".join(
            [
                TRANSITION_PROMPT_VERSION,
                transition.record.id,
                assertion.record.id,
                entity.canonical_id,
                transition.record.text,
                assertion.record.text,
            ]
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def judge(self, transition: TransitionFact, assertion, entity) -> Tuple[TransitionDecision, bool]:
        if SUSPICIOUS_RE.search(transition.record.text or "") or SUSPICIOUS_RE.search(
            assertion.record.text or ""
        ):
            return (
                TransitionDecision(
                    verdict="uncertain",
                    scope_entity=entity.canonical_id,
                    confidence=0.0,
                    evidence_old="",
                    evidence_transition="",
                    historical_markers=[],
                    reason="suspicious_content",
                ),
                True,
            )
        key = self._cache_key(transition, assertion, entity)
        hit = self.cache.get(key)
        if hit is not None:
            self.cached += 1
            return parse_transition_decision(json.dumps(hit, ensure_ascii=False), transition, assertion), True
        content, usage = self.client.chat(transition_messages(transition, assertion, entity))
        self.calls += 1
        self.prompt_tokens += usage.get("prompt_tokens", 0)
        self.completion_tokens += usage.get("completion_tokens", 0)
        decision = parse_transition_decision(content, transition, assertion)
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


class TransitionGovernor:
    """Scan transitions, expand old current-state assertions by scope, judge, gate."""

    def __init__(
        self,
        adapter: Adapter,
        config: AIConfig,
        ledger: Optional[Ledger] = None,
        *,
        dry_run: bool = True,
        max_records: Optional[int] = None,
        candidates_only: bool = False,
        aliases_path: Optional[str] = None,
        max_transitions: int = 10,
        max_candidates: int = 5,
        max_judgements: int = 25,
        max_invalidations: int = 3,
        only_transition: Optional[List[str]] = None,
        judge: Optional[TransitionJudge] = None,
    ):
        self.adapter = adapter
        self.config = config
        self.ledger = ledger or Ledger()
        self.dry_run = dry_run
        self.max_records = max_records
        self.candidates_only = candidates_only
        self.aliases_path = aliases_path
        self.max_transitions = max_transitions
        self.max_candidates = max_candidates
        self.max_judgements = max_judgements
        self.max_invalidations = max_invalidations
        self.only_transition = [x.strip() for x in (only_transition or []) if x.strip()]
        self.judge = judge or TransitionJudge(
            config, cache=AICache(config.cache_path) if config.cache_path else AICache(None)
        )

    def run(self) -> ScanReport:
        started = datetime.now(timezone.utc)
        report = ScanReport(
            run_id=uuid.uuid4().hex[:12],
            started_at=started,
            adapter=self.adapter.name,
            dry_run=self.dry_run,
            mode="ai-transition",
        )
        records = self.adapter.list_memories(limit=self.max_records)
        aliases = load_aliases(self.aliases_path)
        transitions = collect_transitions(records, aliases)
        if self.only_transition:
            transitions = [
                item for item in transitions
                if any(item.record.id.startswith(prefix) for prefix in self.only_transition)
            ]
        index = build_assertion_index(records, aliases)
        candidates = rank_candidates(
            transitions,
            index,
            max_transitions=self.max_transitions,
            max_candidates=self.max_candidates,
            aliases=aliases,
        )
        report.records_scanned = len(records)
        report.ai = {
            "model": self.config.model,
            "prompt_version": TRANSITION_PROMPT_VERSION,
            "confidence_threshold": self.config.confidence_threshold,
            "records_scanned": len(records),
            "transition_count": len(transitions),
            "assertion_index_entities": len(index),
            "candidate_count": len(candidates),
            "limits": {
                "max_transitions": self.max_transitions,
                "max_candidates": self.max_candidates,
                "max_judgements": self.max_judgements,
                "max_invalidations": self.max_invalidations,
            },
            "transitions": [
                {
                    "id": t.record.id,
                    "type": t.transition_type,
                    "effective_at": t.for_time.isoformat() if t.for_time else None,
                    "text": t.record.text[:180],
                    "entities": [e.canonical_id for e in t.entities],
                    "scope_entities": [e.canonical_id for e in (t.scope_entities or t.entities)],
                }
                for t in transitions[:20]
            ],
            "candidates": [
                {
                    "transition_id": c.transition.record.id,
                    "assertion_id": c.assertion.record.id,
                    "entity": c.entity.canonical_id,
                    "score": c.score,
                    "transition_text": c.transition.record.text[:120],
                    "assertion_text": c.assertion.record.text[:120],
                }
                for c in candidates
            ],
        }

        if self.candidates_only:
            report.finished_at = datetime.now(timezone.utc)
            report.run_file = str(self.ledger.write_run(report))
            return report

        claimed: Dict[str, str] = {}
        rejected: Counter[str] = Counter()
        errors: List[str] = []
        judged = 0
        report.ai["decisions"] = []
        for candidate in candidates:
            if len(report.actions) >= self.max_invalidations:
                report.ai["cap_reached"] = "max_invalidations"
                break
            if judged >= self.max_judgements:
                report.ai["cap_reached"] = "max_judgements"
                break
            judged += 1
            try:
                decision, _cached = self.judge.judge(
                    candidate.transition, candidate.assertion, candidate.entity
                )
            except AIError as exc:
                errors.append(f"{candidate.assertion.record.id}: {exc}")
                continue
            report.ai["decisions"].append({
                "transition_id": candidate.transition.record.id,
                "assertion_id": candidate.assertion.record.id,
                "entity": candidate.entity.canonical_id,
                "verdict": decision.verdict,
                "confidence": decision.confidence,
                "scope_entity": decision.scope_entity,
                "evidence_old": decision.evidence_old,
                "evidence_transition": decision.evidence_transition,
                "reason": decision.reason,
            })
            action, reason = transition_decision_to_action(
                candidate.transition,
                candidate.assertion,
                decision,
                confidence_threshold=self.config.confidence_threshold,
            )
            if action is None:
                rejected[(reason or "rejected").split(":", 1)[0]] += 1
                continue
            if action.memory_id in claimed:
                rejected["already_claimed"] += 1
                continue
            claimed[action.memory_id] = candidate.transition.record.id
            report.actions.append(action)

        report.ai.update(self.judge.usage())
        report.ai["judged"] = judged
        report.ai["rejected"] = dict(rejected)
        report.ai["errors"] = errors[:20]
        report.finished_at = datetime.now(timezone.utc)

        if not self.dry_run and report.actions:
            Engine(self.adapter, [], self.ledger, dry_run=False).apply(report)
            self._verify_and_maybe_rollback(report)

        report.run_file = str(self.ledger.write_run(report))
        return report

    def _verify_and_maybe_rollback(self, report: ScanReport) -> None:
        applied = [a for a in report.actions if a.applied]
        if not applied:
            return
        try:
            result = verify_forgetting(
                report.to_dict(),
                lambda q, n: self.adapter.recall(q, n),
                top=5,
            )
        except Exception as exc:  # noqa: BLE001
            report.ai["verification_error"] = str(exc)[:300]
            return
        report.verification = result
        if result["failure_rate"] <= self.config.verify_failure_threshold:
            return
        if not self.config.auto_rollback:
            return
        rollback_errors: List[str] = []
        for action in applied:
            try:
                self.adapter.restore(action.memory_id)
                action.applied = False
                action.error = "auto_rollback"
            except Exception as exc:  # noqa: BLE001
                rollback_errors.append(f"{action.memory_id}: {exc}")
        report.rolled_back = True
        report.ai["rollback_errors"] = rollback_errors
        report.warnings.append(
            "auto-rollback: forgetting failure rate "
            f"{result['failure_rate']:.2f} > {self.config.verify_failure_threshold:.2f}"
        )


def summarize_transition(report: ScanReport) -> str:
    ai = report.ai or {}
    lines = [
        "# state-transition candidates",
        "",
        f"- transitions: {ai.get('transition_count', 0)}; candidates: {ai.get('candidate_count', 0)}; "
        f"judged: {ai.get('judged', 0)}; actions: {len(report.actions)}",
        "",
        "| transition | assertion | entity | score |",
        "|---|---|---|---|",
    ]
    for row in (ai.get("candidates") or [])[:50]:
        lines.append(
            f"| `{row['transition_id'][:12]}` {row['transition_text'][:44]} "
            f"| `{row['assertion_id'][:12]}` {row['assertion_text'][:44]} "
            f"| `{row['entity']}` | {row['score']} |"
        )
    return "\n".join(lines) + "\n"
