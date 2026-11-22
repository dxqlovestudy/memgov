"""AI-assisted supersede stamping.

Why this exists
---------------
``SupersedePolicy`` only honours *explicit* evidence (``metadata.supersedes`` or a
``supersedes:<id>`` tag) and never guesses from similarity — that rule stays. The
problem is that nothing on the write path ever produces that evidence, so in
practice the policy matches zero records.

MEMOIR (arXiv:2611.07631) §3.3 shows how to certify a temporal supersession:
cheap blocking -> conflict signal -> three judge framings -> majority vote ->
mark the old item retired, never delete it.

Hindsight 1.2.0 cannot store metadata through ``PATCH /memories/{id}`` (the schema
accepts text / context / fact_type / entities / state / reason only), so a verdict
cannot be written onto the new record in the backend. Instead accepted pairs land in
a local JSONL ledger and are injected into the new record's ``metadata.supersedes``
at read time. The policy still sees explicit evidence; nothing is guessed at apply
time; every verdict carries evidence substrings, confidence and a rollback path.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .ai import AICache, AIConfig, OpenAICompatibleClient
from .candidates import CandidatePair, candidate_pairs
from .ledger import Ledger
from .models import Action, MemoryRecord, ScanReport
from .prompts import SUPERSEDE_FRAMINGS, SUPERSEDE_PROMPT_VERSION, supersede_messages

# Lexical conflict signal (the TRIAGE-alpha stand-in for Chinese text). This only
# *proposes* a pair for judging; it never decides anything on its own.
CONFLICT_RE = re.compile(
    "改为|改成|换成|改走|切换为|切换到|切到|更新为|现在用|不再|已下线|下线|已停用|停用|"
    "已废弃|废弃|已迁移|迁移到|替代|取代|已改名|改名|统一走|作废"
)

# Never send secrets to a judge, even if they already leaked into memory.
SUSPICIOUS_RE = re.compile(r"(?i)\b(api[_-]?key|secret|token|password|passwd)\b\s*[:=]")


def detect_conflict(new_text: str, old_text: str = "") -> str:
    """Return the conflict markers found in the newer text (comma joined)."""
    hits = sorted({m.group(0) for m in CONFLICT_RE.finditer(new_text or "")})
    return ",".join(hits)


def _when(rec: MemoryRecord) -> datetime:
    return rec.created_at or rec.updated_at or datetime.min.replace(tzinfo=timezone.utc)


def order_pair(pair: CandidatePair) -> Tuple[MemoryRecord, MemoryRecord]:
    """Return ``(newer, older)``; ties break on id so the result is deterministic."""
    a, b = pair.a, pair.b
    if _when(a) == _when(b):
        return (a, b) if a.id <= b.id else (b, a)
    return (a, b) if _when(a) > _when(b) else (b, a)


@dataclass
class SupersedeVerdict:
    framing: str
    verdict: str  # SUPERSEDES | INDEPENDENT
    confidence: float = 0.0
    evidence_new: str = ""
    evidence_old: str = ""
    reason: str = ""

    def to_dict(self) -> Dict[str, object]:
        return {
            "framing": self.framing,
            "verdict": self.verdict,
            "confidence": round(self.confidence, 4),
            "evidence_new": self.evidence_new,
            "evidence_old": self.evidence_old,
            "reason": self.reason,
        }


def _strip_fences(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def parse_verdict(content: str, framing: str, new: MemoryRecord, old: MemoryRecord) -> SupersedeVerdict:
    """Parse one judge reply and downgrade it to INDEPENDENT when the contract fails."""
    raw = _strip_fences(content)
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        return SupersedeVerdict(framing, "INDEPENDENT", 0.0, reason="unparsable")
    try:
        data = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return SupersedeVerdict(framing, "INDEPENDENT", 0.0, reason="invalid_json")

    verdict = str(data.get("verdict", "")).strip().upper()
    if verdict not in {"SUPERSEDES", "INDEPENDENT"}:
        return SupersedeVerdict(framing, "INDEPENDENT", 0.0, reason="bad_verdict")
    try:
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    new_ev = str(data.get("evidence_new", "") or "").strip()
    old_ev = str(data.get("evidence_old", "") or "").strip()
    if verdict == "SUPERSEDES":
        # Evidence must be a contiguous substring of the quoted record.
        if not new_ev or new_ev not in (new.text or ""):
            return SupersedeVerdict(framing, "INDEPENDENT", 0.0, reason="evidence_new_not_in_text")
        if not old_ev or old_ev not in (old.text or ""):
            return SupersedeVerdict(framing, "INDEPENDENT", 0.0, reason="evidence_old_not_in_text")

    return SupersedeVerdict(
        framing=framing,
        verdict=verdict,
        confidence=confidence,
        evidence_new=new_ev[:200],
        evidence_old=old_ev[:200],
        reason=str(data.get("reason", ""))[:80],
    )


class SupersedeJudge:
    """Judge one candidate pair from all three framings and return the vote."""

    def __init__(self, config: AIConfig, client=None, cache: Optional[AICache] = None):
        self.config = config
        self.client = client or OpenAICompatibleClient(config)
        self.cache = cache if cache is not None else AICache(config.cache_path)
        self.calls = 0
        self.cached = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0

    @staticmethod
    def _cache_key(framing: str, new: MemoryRecord, old: MemoryRecord) -> str:
        payload = "|".join(
            [SUPERSEDE_PROMPT_VERSION, framing, new.id, old.id, new.text or "", old.text or ""]
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def judge(
        self, new: MemoryRecord, old: MemoryRecord, anchors: List[str], conflict: str
    ) -> List[SupersedeVerdict]:
        if SUSPICIOUS_RE.search(new.text or "") or SUSPICIOUS_RE.search(old.text or ""):
            return [
                SupersedeVerdict(f, "INDEPENDENT", 0.0, reason="suspicious_content")
                for f in SUPERSEDE_FRAMINGS
            ]
        verdicts: List[SupersedeVerdict] = []
        for framing in SUPERSEDE_FRAMINGS:
            key = self._cache_key(framing, new, old)
            hit = self.cache.get(key)
            if hit is not None:
                self.cached += 1
                verdicts.append(
                    SupersedeVerdict(
                        framing=str(hit.get("framing", framing)),
                        verdict=str(hit.get("verdict", "INDEPENDENT")),
                        confidence=float(hit.get("confidence", 0.0) or 0.0),
                        evidence_new=str(hit.get("evidence_new", "")),
                        evidence_old=str(hit.get("evidence_old", "")),
                        reason=str(hit.get("reason", "")) + "|cached",
                    )
                )
                continue
            content, usage = self.client.chat(supersede_messages(new, old, framing, anchors, conflict))
            self.calls += 1
            self.prompt_tokens += usage.get("prompt_tokens", 0)
            self.completion_tokens += usage.get("completion_tokens", 0)
            verdict = parse_verdict(content, framing, new, old)
            self.cache.put(key, verdict.to_dict())
            verdicts.append(verdict)
        return verdicts

    def usage(self) -> Dict[str, int]:
        return {
            "calls": self.calls,
            "cached": self.cached,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.prompt_tokens + self.completion_tokens,
        }


class SupersedeLedger:
    """Append-only JSONL ledger of accepted supersessions (new_id -> old_ids)."""

    def __init__(self, path: str = "runs/supersede-ledger.jsonl"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, entry: Dict[str, object]) -> None:
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def entries(self) -> List[Dict[str, object]]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def map_by_new(self) -> Dict[str, List[str]]:
        """Accepted old ids per new id; a later ``revoke`` entry removes one."""
        accepted: Dict[str, List[str]] = {}
        for entry in self.entries():
            new_id, old_id = str(entry.get("new_id", "")), str(entry.get("old_id", ""))
            if not new_id or not old_id:
                continue
            if entry.get("action") == "revoke":
                if old_id in accepted.get(new_id, []):
                    accepted[new_id].remove(old_id)
                continue
            accepted.setdefault(new_id, [])
            if old_id not in accepted[new_id]:
                accepted[new_id].append(old_id)
        return {k: v for k, v in accepted.items() if v}


class SupersedeGovernor:
    """Propose, judge and (optionally) stamp supersessions; dry-run by default."""

    def __init__(
        self,
        adapter,
        config: AIConfig,
        ledger: Optional[Ledger] = None,
        supersede_ledger: Optional[SupersedeLedger] = None,
        *,
        dry_run: bool = True,
        max_records: Optional[int] = None,
        max_pairs: int = 100,
        max_accepts: int = 10,
        min_score: float = 0.30,
        min_similarity: float = 0.0,
        vote_threshold: float = 0.6,
        min_confidence: float = 0.90,
        judge: Optional[SupersedeJudge] = None,
    ):
        self.adapter = adapter
        self.config = config
        self.ledger = ledger or Ledger()
        self.supersede_ledger = supersede_ledger or SupersedeLedger()
        self.dry_run = dry_run
        self.max_records = max_records
        self.max_pairs = max_pairs
        self.max_accepts = max_accepts
        self.min_score = min_score
        self.min_similarity = min_similarity
        self.vote_threshold = vote_threshold
        self.min_confidence = min_confidence
        self.judge = judge or SupersedeJudge(
            config, cache=AICache(config.cache_path) if config.cache_path else AICache(None)
        )

    def run(self) -> ScanReport:
        started = datetime.now(timezone.utc)
        report = ScanReport(
            run_id=__import__("uuid").uuid4().hex[:12],
            started_at=started,
            adapter=self.adapter.name,
            dry_run=self.dry_run,
            mode="ai-supersede",
        )
        records = self.adapter.list_memories(limit=self.max_records)
        report.records_scanned = len(records)

        pairs = candidate_pairs(records, max_pairs=self.max_pairs, min_score=self.min_score)
        proposed: List[Dict[str, object]] = []
        judged = accepted = 0
        for pair in pairs:
            new, old = order_pair(pair)
            if pair.score < self.min_similarity:
                continue
            conflict = detect_conflict(new.text, old.text)
            if not conflict:
                continue
            verdicts = self.judge.judge(new, old, pair.shared_anchors, conflict)
            judged += 1
            sup = [v for v in verdicts if v.verdict == "SUPERSEDES"]
            ratio = len(sup) / len(verdicts) if verdicts else 0.0
            mean_conf = sum(v.confidence for v in sup) / len(sup) if sup else 0.0
            ok = bool(verdicts) and ratio >= self.vote_threshold and mean_conf >= self.min_confidence
            proposed.append(
                {
                    "new_id": new.id,
                    "old_id": old.id,
                    "score": round(pair.score, 4),
                    "shared_anchors": pair.shared_anchors[:6],
                    "conflict": conflict,
                    "votes": {v.framing: v.verdict for v in verdicts},
                    "supersede_ratio": round(ratio, 3),
                    "mean_confidence": round(mean_conf, 3),
                    "accepted": ok,
                    "reason": "; ".join(v.reason for v in verdicts if v.reason)[:160],
                }
            )
            if not ok:
                continue
            if accepted >= self.max_accepts:
                report.warnings.append(f"max_accepts={self.max_accepts} reached; remaining pairs skipped")
                break
            accepted += 1
            evidence = next((v for v in sup if v.evidence_new), sup[0] if sup else None)
            reason = (
                f"supersede-v1 {len(sup)}/{len(verdicts)} frames, mean_conf={mean_conf:.2f}"
            )
            action = Action(
                action="stamp",
                memory_id=old.id,
                policy="supersede",
                reason=reason,
                replacement_id=new.id,
                before={
                    "old_text": (old.text or "")[:300],
                    "new_text": (new.text or "")[:300],
                    "evidence_new": evidence.evidence_new if evidence else "",
                    "evidence_old": evidence.evidence_old if evidence else "",
                },
                reversible=True,
            )
            if not self.dry_run:
                self.supersede_ledger.append(
                    {
                        "action": "stamp",
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "new_id": new.id,
                        "old_id": old.id,
                        "prompt_version": SUPERSEDE_PROMPT_VERSION,
                        "votes": {v.framing: v.verdict for v in verdicts},
                        "supersede_ratio": round(ratio, 3),
                        "mean_confidence": round(mean_conf, 3),
                        "evidence_new": evidence.evidence_new if evidence else "",
                        "evidence_old": evidence.evidence_old if evidence else "",
                        "new_text": (new.text or "")[:400],
                        "old_text": (old.text or "")[:400],
                    }
                )
                action.applied = True
            report.actions.append(action)

        report.finished_at = datetime.now(timezone.utc)
        report.ai = {
            "model": self.config.model,
            "prompt_version": SUPERSEDE_PROMPT_VERSION,
            "records_scanned": len(records),
            "candidate_pairs": len(pairs),
            "judged_pairs": judged,
            "accepted": accepted,
            "limits": {
                "max_pairs": self.max_pairs,
                "max_accepts": self.max_accepts,
                "min_score": self.min_score,
                "min_similarity": self.min_similarity,
                "vote_threshold": self.vote_threshold,
                "min_confidence": self.min_confidence,
            },
            "proposals": proposed[:60],
            "usage": self.judge.usage(),
        }
        report.run_file = str(self.ledger.write_run(report))
        return report


def summarize_supersede(report: ScanReport) -> str:
    ai = report.ai or {}
    lines = [
        "# memgov ai-supersede report",
        "",
        f"- run: `{report.run_id}`",
        f"- adapter: `{report.adapter}`",
        f"- mode: **{'dry-run' if report.dry_run else 'APPLY'}**",
        f"- records scanned: {report.records_scanned}",
        f"- candidate pairs: {ai.get('candidate_pairs', 0)}  judged: {ai.get('judged_pairs', 0)}",
        f"- accepted: {ai.get('accepted', 0)}",
        "",
        f"usage: {json.dumps(ai.get('usage', {}), ensure_ascii=False)}",
        "",
    ]
    if report.warnings:
        lines.append("warnings:")
        lines.extend(f"- {w}" for w in report.warnings)
        lines.append("")
    rows = [p for p in ai.get("proposals", []) if p.get("accepted")]
    if rows:
        lines.append("## Accepted")
        lines.append("")
        lines.append("| new | old | ratio | conf | conflict |")
        lines.append("|---|---|---|---|---|")
        for p in rows:
            lines.append(
                f"| `{str(p['new_id'])[:12]}` | `{str(p['old_id'])[:12]}` | "
                f"{p['supersede_ratio']} | {p['mean_confidence']} | {p['conflict']} |"
            )
        lines.append("")
    lines.append(f"run file: {report.run_file}")
    if report.dry_run:
        lines.append("dry-run only; re-run with --apply to write the supersede ledger")
    else:
        lines.append("ledger written; run `memgov scan --apply` to retire the superseded records")
    return "\n".join(lines)
