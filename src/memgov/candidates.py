"""Candidate pair generation.

Calling a model for every pair is O(n²) and pointless: most pairs have nothing to do
with each other. Candidates are produced by cheap blocking (anchors + character-bigram
signatures) and scored before any model call.
"""

from __future__ import annotations

import re
import zlib
from dataclasses import dataclass
from typing import Dict, List, Set, Tuple

from .models import MemoryRecord, bigrams

ANCHOR_PATTERNS = [
    re.compile(r"https?://[^\s，。；）)]+"),
    re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"),
    re.compile(r"\b[A-Z][A-Z0-9_]{2,}_[A-Z0-9_]{2,}\b"),
    re.compile(r"\bMR !\d+\b"),
    re.compile(r"#[A-Za-z0-9_-]{2,}"),
    re.compile(r"[\w./-]+\.(?:java|py|ts|tsx|js|md|json|yaml|yml|sql)\b"),
    re.compile(r"「([^」]{2,24})」"),
]


@dataclass
class CandidatePair:
    a: MemoryRecord
    b: MemoryRecord
    score: float
    shared_anchors: List[str]

    def key(self) -> str:
        return f"{self.a.id}|{self.b.id}"


def extract_anchors(text: str) -> Set[str]:
    anchors: Set[str] = set()
    for pattern in ANCHOR_PATTERNS:
        for match in pattern.finditer(text or ""):
            value = match.group(1) if match.groups() else match.group(0)
            value = value.strip().lower()
            if value:
                anchors.add(value)
    return anchors


def _norm_len(text: str) -> str:
    return re.sub(r"[\s\W_]+", "", (text or "").lower())


def _set_jaccard(a: Set[str], b: Set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _signature(text: str, size: int = 8) -> List[str]:
    grams = bigrams(text)
    if not grams:
        return []
    ranked = sorted((zlib.crc32(g.encode("utf-8")), g) for g in grams)[:size]
    return [str(h) for h, _ in ranked]


def candidate_pairs(
    records: List[MemoryRecord],
    *,
    max_pairs: int = 100,
    min_score: float = 0.22,
    same_kind_only: bool = False,
    max_block_size: int = 2000,
    max_candidates_per_record: int = 40,
) -> List[CandidatePair]:
    """Return at most ``max_pairs`` pairs ordered by decreasing similarity."""
    records = [r for r in records if r.state == "valid" and (r.text or "").strip()]
    if len(records) < 2:
        return []

    anchor_index: Dict[str, List[int]] = {}
    sig_index: Dict[str, List[int]] = {}
    anchors: List[Set[str]] = []
    signatures: List[List[str]] = []
    grams: List[Set[str]] = []
    lengths: List[int] = []

    for i, rec in enumerate(records):
        a = extract_anchors(rec.text)
        anchors.append(a)
        grams.append(bigrams(rec.text))
        lengths.append(len(_norm_len(rec.text)))
        for anchor in a:
            anchor_index.setdefault(anchor, []).append(i)
        sig = _signature(rec.text)
        signatures.append(sig)
        for s in sig:
            sig_index.setdefault(s, []).append(i)

    pair_scores: Dict[Tuple[int, int], Tuple[float, List[str]]] = {}

    for i, rec in enumerate(records):
        candidate_ids: Set[int] = set()
        for anchor in anchors[i]:
            bucket = anchor_index.get(anchor) or []
            if len(bucket) <= max_block_size:
                candidate_ids.update(bucket)
        for s in signatures[i]:
            bucket = sig_index.get(s) or []
            if len(bucket) <= max_block_size:
                candidate_ids.update(bucket)
        candidate_ids.discard(i)

        scored: List[Tuple[float, int, List[str]]] = []
        for j in candidate_ids:
            other = records[j]
            if same_kind_only and rec.kind != other.kind:
                continue
            shared = sorted(anchors[i] & anchors[j])
            text_score = _set_jaccard(grams[i], grams[j])
            anchor_score = len(shared) / max(1, len(anchors[i] | anchors[j]))
            score = max(text_score, anchor_score)
            if lengths[i] and lengths[j]:
                ratio = min(lengths[i], lengths[j]) / max(lengths[i], lengths[j])
                if ratio < 0.25:
                    continue
            if not shared and text_score < min_score:
                continue
            if shared and text_score < min_score and anchor_score < 0.5:
                continue
            scored.append((score, j, shared))

        scored.sort(key=lambda x: (-x[0], records[x[1]].id))
        for score, j, shared in scored[:max_candidates_per_record]:
            key = (i, j) if i < j else (j, i)
            if key not in pair_scores or pair_scores[key][0] < score:
                pair_scores[key] = (score, shared)

    ranked = sorted(
        pair_scores.items(), key=lambda kv: (-kv[1][0], records[kv[0][0]].id)
    )[:max_pairs]
    out: List[CandidatePair] = []
    for (i, j), (score, shared) in ranked:
        a, b = records[i], records[j]
        # order by time when available, so "B is newer" holds for the prompt
        if a.created_at and b.created_at and a.created_at > b.created_at:
            a, b = b, a
        out.append(
            CandidatePair(a=a, b=b, score=round(score, 3), shared_anchors=shared[:8])
        )
    return out
