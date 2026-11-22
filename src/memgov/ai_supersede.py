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
from .models import Action, MemoryRecord, ScanReport, bigrams
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


def conflict_seeded_pairs(
    records: List[MemoryRecord],
    *,
    min_overlap: float = 0.22,
    top_k: int = 3,
    max_pairs: int = 200,
) -> List[CandidatePair]:
    """Pair every conflict-bearing record with its closest older neighbours.

    Anchor blocking (``candidate_pairs``) misses the common case where a rule is
    restated in different words: "推送须逐次同意" vs "push 要单独放行" share no
    anchor. This second channel seeds from the lexical conflict signal instead and
    ranks by text overlap, so a restated-and-updated pair can still meet a judge.
    It only proposes; nothing here can retire a record.
    """
    valid = [r for r in records if r.state == "valid" and (r.text or "").strip()]
    if len(valid) < 2:
        return []
    grams = {r.id: bigrams(r.text) for r in valid}
    seed_records = [r for r in valid if detect_conflict(r.text, "")]
    if not seed_records:
        return []

    scored: Dict[Tuple[str, str], CandidatePair] = {}
    for new in seed_records:
        candidates: List[Tuple[float, MemoryRecord]] = []
        g_new = grams[new.id]
        for old in valid:
            if old.id == new.id:
                continue
            if _when(old) >= _when(new):
                continue
            g_old = grams[old.id]
            if not g_old or not g_new:
                continue
            inter = len(g_new & g_old)
            union = len(g_new | g_old)
            overlap = inter / union if union else 0.0
            if overlap >= min_overlap:
                candidates.append((overlap, old))
        candidates.sort(key=lambda x: -x[0])
        for overlap, old in candidates[:top_k]:
            key = (old.id, new.id)
            existing = scored.get(key)
            if existing is None or existing.score < overlap:
                scored[key] = CandidatePair(a=old, b=new, score=overlap, shared_anchors=[])
    ranked = sorted(scored.values(), key=lambda p: -p.score)
    return ranked[:max_pairs]


def merge_candidate_pairs(
    anchor_pairs: List[CandidatePair],
    seeded_pairs: List[CandidatePair],
    *,
    max_pairs: int,
    seeded_quota: Optional[int] = None,
) -> List[CandidatePair]:
    """Union both channels without comparing their scores against each other.

    The two channels produce different score semantics (anchor blocking score vs
    raw text overlap), so a single sort would always let one channel crowd out the
    other. Instead the seeded channel gets a fixed quota and the anchor channel
    fills the rest; duplicates collapse on ``(old_id, new_id)``.
    """
    deduped: Dict[Tuple[str, str], CandidatePair] = {}

    def _add(pair: CandidatePair) -> Optional[Tuple[str, str]]:
        new, old = order_pair(pair)
        key = (old.id, new.id)
        if key in deduped:
            return None
        deduped[key] = CandidatePair(
            a=old, b=new, score=pair.score, shared_anchors=list(pair.shared_anchors)
        )
        return key

    quota = max_pairs if seeded_quota is None else max(0, min(seeded_quota, max_pairs))
    order: List[Tuple[str, str]] = []
    for pair in sorted(seeded_pairs, key=lambda p: -p.score)[:quota]:
        key = _add(pair)
        if key:
            order.append(key)
    for pair in sorted(anchor_pairs, key=lambda p: -p.score):
        if len(order) >= max_pairs:
            break
        key = _add(pair)
        if key:
            order.append(key)
    return [deduped[key] for key in order[:max_pairs]]


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


def _loads_loose(text: str):
    """Try progressively looser JSON parses of one judge reply."""
    candidates = [text]
    fixed = re.sub(r",\s*([}\]])", r"\1", text)
    if fixed != text:
        candidates.append(fixed)
    swapped = fixed.replace("'", '"') if "'" in fixed else None
    if swapped and swapped not in candidates:
        candidates.append(swapped)
    for candidate in list(candidates):
        opens = candidate.count("{") - candidate.count("}")
        if opens > 0:
            candidates.append(candidate + "}" * opens)
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return None


def parse_verdict(content: str, framing: str, new: MemoryRecord, old: MemoryRecord) -> SupersedeVerdict:
    """Parse one judge reply and downgrade it to INDEPENDENT when the contract fails."""
    raw = _strip_fences(content)
    start, end = raw.find("{"), raw.rfind("}")
    data = _loads_loose(raw[start : end + 1]) if start != -1 and end > start else None
    if data is None:
        # Last resort: the verdict word often survives even when the JSON does not.
        # Confidence stays 0 so a malformed reply can never clear the accept gate.
        match = re.search(r"(?i)\b(SUPERSEDES|DUPLICATE|INDEPENDENT)\b", raw)
        if not match:
            return SupersedeVerdict(framing, "INDEPENDENT", 0.0, reason="unparsable")
        return SupersedeVerdict(
            framing,
            match.group(1).upper(),
            0.0,
            reason="verdict_only_no_json",
        )

    verdict = str(data.get("verdict", "")).strip().upper()
    if verdict not in {"SUPERSEDES", "DUPLICATE", "INDEPENDENT"}:
        match = re.search(r"(?i)\b(SUPERSEDES|DUPLICATE|INDEPENDENT)\b", raw)
        if not match:
            return SupersedeVerdict(framing, "INDEPENDENT", 0.0, reason="bad_verdict")
        verdict = match.group(1).upper()
    try:
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    new_ev = str(data.get("evidence_new", "") or "").strip()
    old_ev = str(data.get("evidence_old", "") or "").strip()
    if verdict in {"SUPERSEDES", "DUPLICATE"}:
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
        # Re-running ai-supersede over the same bank re-certifies the same pairs, so
        # appends must be idempotent: a repeated certification of the same
        # (action, old_id, new_id) is already in the ledger and must not double count.
        self._seen = {
            (e.get("action"), e.get("old_id"), e.get("new_id")) for e in self.entries()
        }

    def append(self, entry: Dict[str, object]) -> None:
        key = (entry.get("action"), entry.get("old_id"), entry.get("new_id"))
        if key in self._seen:
            return
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self._seen.add(key)

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
            if entry.get("action") == "duplicate":
                continue
            accepted.setdefault(new_id, [])
            if old_id not in accepted[new_id]:
                accepted[new_id].append(old_id)
        return {k: v for k, v in accepted.items() if v}


    def duplicate_map(self) -> Dict[str, str]:
        """old_id -> new_id for accepted duplicates; a later revoke removes one."""
        out: Dict[str, str] = {}
        for entry in self.entries():
            action = entry.get("action")
            old_id = str(entry.get("old_id", ""))
            new_id = str(entry.get("new_id", ""))
            if not old_id or not new_id:
                continue
            if action == "duplicate":
                out[old_id] = new_id
            elif action == "revoke" and entry.get("kind") == "duplicate":
                out.pop(old_id, None)
        return out


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
        min_confidence: float = 0.85,
        seeded_overlap: float = 0.22,
        seeded_top_k: int = 3,
        seeded_quota_ratio: float = 0.7,
        use_seeded: bool = True,
        use_duplicate: bool = True,
        duplicate_confidence: Optional[float] = None,
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
        self.seeded_overlap = seeded_overlap
        self.seeded_top_k = seeded_top_k
        self.seeded_quota_ratio = seeded_quota_ratio
        self.use_seeded = use_seeded
        self.use_duplicate = use_duplicate
        # A duplicate is a restatement, not a value change: invalidating the copy
        # loses no information, while the judge scores restatements lower than
        # supersessions (0.61-0.90 vs 0.86-0.95 on the Chinese pair set).
        self.duplicate_confidence = (
            min_confidence - 0.05 if duplicate_confidence is None else duplicate_confidence
        )
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
        anchor_pairs = list(pairs)
        seeded_pairs = (
            conflict_seeded_pairs(
                records,
                min_overlap=self.seeded_overlap,
                top_k=self.seeded_top_k,
                max_pairs=self.max_pairs,
            )
            if self.use_seeded
            else []
        )
        pairs = merge_candidate_pairs(
            anchor_pairs,
            seeded_pairs,
            max_pairs=self.max_pairs,
            seeded_quota=max(1, int(self.max_pairs * self.seeded_quota_ratio)),
        )
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
            dup = [v for v in verdicts if v.verdict == "DUPLICATE"]
            ratio = len(sup) / len(verdicts) if verdicts else 0.0
            mean_conf = sum(v.confidence for v in sup) / len(sup) if sup else 0.0
            dup_ratio = len(dup) / len(verdicts) if verdicts else 0.0
            dup_conf = sum(v.confidence for v in dup) / len(dup) if dup else 0.0
            supersede_ok = (
                bool(verdicts) and ratio >= self.vote_threshold and mean_conf >= self.min_confidence
            )
            duplicate_ok = (
                bool(verdicts)
                and not supersede_ok
                and self.use_duplicate
                and dup_ratio >= self.vote_threshold
                and dup_conf >= self.duplicate_confidence
            )
            kind = "supersede" if supersede_ok else ("duplicate" if duplicate_ok else "none")
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
                    "duplicate_ratio": round(dup_ratio, 3),
                    "duplicate_confidence": round(dup_conf, 3),
                    "kind": kind,
                    "accepted": kind != "none",
                    "reason": "; ".join(v.reason for v in verdicts if v.reason)[:160],
                }
            )
            if kind == "none":
                continue
            if accepted >= self.max_accepts:
                report.warnings.append(f"max_accepts={self.max_accepts} reached; remaining pairs skipped")
                break
            accepted += 1
            evidence = next(
                (v for v in (sup if kind == "supersede" else dup) if v.evidence_new),
                (sup or dup)[0] if (sup or dup) else None,
            )
            if kind == "supersede":
                reason = f"supersede-v1 {len(sup)}/{len(verdicts)} frames, mean_conf={mean_conf:.2f}"
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
                ledger_action = "stamp"
            else:
                reason = f"duplicate-v1 {len(dup)}/{len(verdicts)} frames, mean_conf={dup_conf:.2f}"
                action = Action(
                    action="dedupe",
                    memory_id=old.id,
                    policy="dedup",
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
                ledger_action = "duplicate"
            if not self.dry_run:
                self.supersede_ledger.append(
                    {
                        "action": ledger_action,
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "new_id": new.id,
                        "old_id": old.id,
                        "prompt_version": SUPERSEDE_PROMPT_VERSION,
                        "votes": {v.framing: v.verdict for v in verdicts},
                        "ratio": round(ratio if kind == "supersede" else dup_ratio, 3),
                        "mean_confidence": round(mean_conf if kind == "supersede" else dup_conf, 3),
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
            "anchor_pairs": len(anchor_pairs),
            "seeded_pairs": len(seeded_pairs),
            "judged_pairs": judged,
            "accepted": accepted,
            "accepted_supersede": sum(1 for x in proposed if x.get("kind") == "supersede"),
            "accepted_duplicate": sum(1 for x in proposed if x.get("kind") == "duplicate"),
            "limits": {
                "max_pairs": self.max_pairs,
                "max_accepts": self.max_accepts,
                "min_score": self.min_score,
                "min_similarity": self.min_similarity,
                "vote_threshold": self.vote_threshold,
                "min_confidence": self.min_confidence,
                "seeded_overlap": self.seeded_overlap,
                "seeded_top_k": self.seeded_top_k,
                "seeded_quota_ratio": self.seeded_quota_ratio,
                "use_seeded": self.use_seeded,
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
        f"- accepted: {ai.get('accepted', 0)}"
        f" (supersede {ai.get('accepted_supersede', 0)} / duplicate {ai.get('accepted_duplicate', 0)})",
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
