from __future__ import annotations

import re
import zlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from .models import Action, MemoryRecord, bigrams, norm_text


@dataclass
class Capabilities:
    invalidate: bool = True
    delete: bool = False
    archive: bool = False
    importance: bool = False
    usage_signals: bool = False
    recall: bool = False


@dataclass
class PolicyContext:
    now: datetime
    capabilities: Capabilities
    dry_run: bool = True


class Policy:
    name = "policy"
    requires: Sequence[str] = ()

    def evaluate(self, records: List[MemoryRecord], ctx: PolicyContext) -> List[Action]:
        raise NotImplementedError

    def _unsupported(self, ctx: PolicyContext) -> List[str]:
        return [r for r in self.requires if not getattr(ctx.capabilities, r, False)]


class TTLPolicy(Policy):
    """Invalidate records older than a per-kind/tag/regex limit.

    Rules are evaluated top-down; the first matching rule wins.
    """

    name = "ttl"

    def __init__(self, rules: List[Dict[str, Any]]):
        self.rules = rules or []

    _MATCH_KEYS = ("kind", "tag", "tag_prefix", "text_regex")

    def _match(self, rec: MemoryRecord, rule: Dict[str, Any]) -> bool:
        # A rule without any selector would match every record and expire the bank.
        if not any(k in rule for k in self._MATCH_KEYS):
            return False
        if "kind" in rule and rec.kind != rule["kind"]:
            return False
        if "tag" in rule and rule["tag"] not in rec.tags:
            return False
        if "tag_prefix" in rule and not any(t.startswith(rule["tag_prefix"]) for t in rec.tags):
            return False
        if "text_regex" in rule and not re.search(rule["text_regex"], rec.text or "", re.I):
            return False
        return True

    def evaluate(self, records: List[MemoryRecord], ctx: PolicyContext) -> List[Action]:
        actions: List[Action] = []
        for rec in records:
            if rec.state != "valid":
                continue
            for rule in self.rules:
                if not self._match(rec, rule):
                    continue
                max_age = float(rule.get("max_age_days", 0))
                if max_age > 0 and rec.age_days(ctx.now) > max_age:
                    actions.append(
                        Action(
                            action="invalidate",
                            memory_id=rec.id,
                            policy=self.name,
                            reason=f"ttl:{max_age:g}d (age={rec.age_days(ctx.now):.1f}d)",
                            before=rec.to_dict(),
                        )
                    )
                break
        return actions


class SupersedePolicy(Policy):
    """Invalidate records that a newer record explicitly claims to replace.

    Only explicit evidence is used: ``metadata.supersedes`` (list or comma string),
    ``metadata.replaces``, ``tags`` of the form ``supersedes:<id>`` or
    ``replaces:<id>``. Similarity is deliberately NOT used: semantic guessing
    produces silent data loss.
    """

    name = "supersede"

    def __init__(self, max_chain: int = 1000):
        self.max_chain = max_chain

    def _claimed_ids(self, rec: MemoryRecord) -> List[str]:
        ids: List[str] = []
        for key in ("supersedes", "replaces", "superseded_ids"):
            raw = rec.metadata.get(key) if rec.metadata else None
            if isinstance(raw, str):
                ids.extend(x.strip() for x in raw.split(",") if x.strip())
            elif isinstance(raw, (list, tuple, set)):
                ids.extend(str(x).strip() for x in raw if str(x).strip())
        ids.extend(rec.supersedes)
        for tag in rec.tags:
            for prefix in ("supersedes:", "replaces:"):
                if tag.startswith(prefix):
                    ids.append(tag[len(prefix) :].strip())
        # stable de-dup, drop self references
        seen = set()
        out = []
        for i in ids:
            if i and i != rec.id and i not in seen:
                seen.add(i)
                out.append(i)
        return out[: self.max_chain]

    def evaluate(self, records: List[MemoryRecord], ctx: PolicyContext) -> List[Action]:
        by_id = {r.id: r for r in records}
        actions: List[Action] = []
        for rec in records:
            if rec.state != "valid":
                continue
            for target_id in self._claimed_ids(rec):
                target = by_id.get(target_id)
                if target is None or target.state != "valid":
                    continue
                actions.append(
                    Action(
                        action="invalidate",
                        memory_id=target.id,
                        policy=self.name,
                        reason=f"superseded_by:{rec.id}",
                        replacement_id=rec.id,
                        before=target.to_dict(),
                    )
                )
        return actions


class DedupPolicy(Policy):
    """Invalidate duplicate records inside one kind.

    Exact normalized-text matches are always collapsed. Near-duplicates use
    character-bigram signature blocking (a small MinHash-style candidate index)
    so the policy stays near-linear on large banks instead of comparing every
    pair. The newest record (then highest importance) is kept.
    """

    name = "dedup"

    def __init__(self, similarity: float = 0.95, signature_size: int = 16,
                 max_block_size: int = 2000, max_candidates: int = 300):
        self.similarity = float(similarity)
        self.signature_size = max(1, int(signature_size))
        self.max_block_size = int(max_block_size)
        self.max_candidates = max(1, int(max_candidates))

    @staticmethod
    def _signature(text: str, size: int) -> List[str]:
        """Blocking keys: the ``size`` smallest-hash bigrams of the text.

        Two near-identical texts share almost all bigrams, so their smallest-hash
        subsets overlap with high probability. Using several keys (instead of one
        MinHash band) keeps recall high without pairwise comparison.
        """
        grams = bigrams(text)
        if not grams:
            return []
        ranked = sorted((zlib.crc32(g.encode("utf-8")), g) for g in grams)[:size]
        return [str(h) for h, _ in ranked]

    def evaluate(self, records: List[MemoryRecord], ctx: PolicyContext) -> List[Action]:
        actions: List[Action] = []
        groups: Dict[str, List[MemoryRecord]] = {}
        for rec in records:
            if rec.state != "valid":
                continue
            groups.setdefault(rec.kind or "fact", []).append(rec)

        # Bigrams and lengths are extracted once per record; Jaccard then only
        # does set math, and the cheap length guard avoids most comparisons.
        gram_cache: Dict[str, set] = {}
        len_cache: Dict[str, int] = {}

        def grams(r: MemoryRecord) -> set:
            if r.id not in gram_cache:
                gram_cache[r.id] = bigrams(r.text)
            return gram_cache[r.id]

        def nlen(r: MemoryRecord) -> int:
            if r.id not in len_cache:
                len_cache[r.id] = len(norm_text(r.text))
            return len_cache[r.id]

        def sim(a: MemoryRecord, b: MemoryRecord) -> float:
            ga, gb = grams(a), grams(b)
            if not ga or not gb:
                return 0.0
            return len(ga & gb) / len(ga | gb)

        for _kind, group in groups.items():
            ordered = sorted(
                group,
                key=lambda r: (r.reference_time() or ctx.now, r.importance),
                reverse=True,
            )
            exact: Dict[str, MemoryRecord] = {}
            index: Dict[str, List[MemoryRecord]] = {}
            for rec in ordered:
                key = norm_text(rec.text)
                keep = exact.get(key)
                if keep is not None:
                    actions.append(self._dup(rec, keep, "exact"))
                    continue
                exact[key] = rec

                candidates: List[MemoryRecord] = []
                if self.similarity < 1.0:
                    seen = set()
                    for sig in self._signature(rec.text, self.signature_size):
                        bucket = index.get(sig) or []
                        if len(bucket) > self.max_block_size:
                            continue
                        for other in bucket:
                            if other.id in seen:
                                continue
                            seen.add(other.id)
                            la, lb = nlen(rec), nlen(other)
                            if not la or not lb or min(la, lb) / max(la, lb) < 0.6:
                                continue
                            candidates.append(other)
                            if len(candidates) >= self.max_candidates:
                                break
                        if len(candidates) >= self.max_candidates:
                            break
                match = None
                for other in candidates:
                    if sim(rec, other) >= self.similarity:
                        match = other
                        break
                if match is not None:
                    actions.append(
                        self._dup(rec, match, f"jaccard>={self.similarity:g}")
                    )
                    continue
                for sig in self._signature(rec.text, self.signature_size):
                    index.setdefault(sig, []).append(rec)
        return actions

    def _dup(self, rec: MemoryRecord, keep: MemoryRecord, how: str) -> Action:
        return Action(
            action="invalidate",
            memory_id=rec.id,
            policy=self.name,
            reason=f"duplicate_of:{keep.id} ({how})",
            replacement_id=keep.id,
            before=rec.to_dict(),
        )


class ImportancePolicy(Policy):
    """Invalidate low-value records after importance decay."""

    name = "importance"
    requires = ("importance",)

    def __init__(self, half_life_days: float = 90.0, archive_below: float = 0.15,
                 kinds: Optional[List[str]] = None):
        self.half_life_days = float(half_life_days)
        self.archive_below = float(archive_below)
        self.kinds = kinds or []

    def evaluate(self, records: List[MemoryRecord], ctx: PolicyContext) -> List[Action]:
        if self._unsupported(ctx):
            return []
        actions: List[Action] = []
        for rec in records:
            if rec.state != "valid":
                continue
            if self.kinds and rec.kind not in self.kinds:
                continue
            score = rec.effective_score(ctx.now, self.half_life_days)
            if score < self.archive_below:
                actions.append(
                    Action(
                        action="archive",
                        memory_id=rec.id,
                        policy=self.name,
                        reason=f"decayed_score={score:.3f} < {self.archive_below:g}",
                        before=rec.to_dict(),
                        reversible=True,
                    )
                )
        return actions


class ScopeLimitPolicy(Policy):
    """Keep at most N records per scope, evicting the lowest effective score."""

    name = "scope_limit"
    requires = ("importance",)

    def __init__(self, max_per_scope: int, tag_prefix: str = "scope:",
                 half_life_days: float = 90.0):
        self.max_per_scope = int(max_per_scope)
        self.tag_prefix = tag_prefix
        self.half_life_days = float(half_life_days)

    def _scope(self, rec: MemoryRecord) -> Optional[str]:
        for tag in rec.tags:
            if tag.startswith(self.tag_prefix):
                return tag[len(self.tag_prefix) :]
        return None

    def evaluate(self, records: List[MemoryRecord], ctx: PolicyContext) -> List[Action]:
        if self._unsupported(ctx):
            return []
        scoped: Dict[str, List[MemoryRecord]] = {}
        for rec in records:
            if rec.state != "valid":
                continue
            scope = self._scope(rec)
            if scope:
                scoped.setdefault(scope, []).append(rec)

        actions: List[Action] = []
        for scope, group in scoped.items():
            if len(group) <= self.max_per_scope:
                continue
            ordered = sorted(group, key=lambda r: r.effective_score(ctx.now, self.half_life_days))
            for rec in ordered[: len(group) - self.max_per_scope]:
                actions.append(
                    Action(
                        action="archive",
                        memory_id=rec.id,
                        policy=self.name,
                        reason=f"scope_limit:{scope}>{self.max_per_scope}",
                        before=rec.to_dict(),
                        reversible=True,
                    )
                )
        return actions


def build_policies(config: Dict[str, Any]) -> List[Policy]:
    policies: List[Policy] = []
    for item in (config.get("policies") or []):
        if not item.get("enabled", True):
            continue
        name = item.get("name", "")
        if name == "ttl":
            policies.append(TTLPolicy(item.get("rules") or []))
        elif name == "supersede":
            policies.append(SupersedePolicy(max_chain=int(item.get("max_chain", 1000))))
        elif name == "dedup":
            policies.append(
                DedupPolicy(
                    similarity=float(item.get("similarity", 0.95)),
                    signature_size=int(item.get("signature_size", 16)),
                    max_block_size=int(item.get("max_block_size", 2000)),
                    max_candidates=int(item.get("max_candidates", 300)),
                )
            )
        elif name == "importance":
            policies.append(
                ImportancePolicy(
                    half_life_days=float(item.get("half_life_days", 90)),
                    archive_below=float(item.get("archive_below", 0.15)),
                    kinds=item.get("kinds") or [],
                )
            )
        elif name == "scope_limit":
            policies.append(
                ScopeLimitPolicy(
                    max_per_scope=int(item.get("max_per_scope", 500)),
                    tag_prefix=item.get("tag_prefix", "scope:"),
                    half_life_days=float(item.get("half_life_days", 90)),
                )
            )
    return policies
