"""Embedding client and vector index for dedup.

Two pieces:
- EmbeddingClient  : talks to an OpenAI-compatible /embeddings endpoint, with a
                      JSON text->vector cache so re-runs are free.
- EmbeddingIndex   : a normalised float32 matrix persisted as .npz, supporting
                      incremental append and cosine KNN over the whole bank.

The index is the only durable state the incremental deduper needs; it is rebuilt
from scratch whenever the record-id set on disk no longer matches the bank.
"""

from __future__ import annotations

import json
import math
import os
import time
import urllib.error
import urllib.request
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

EMBED_DIM = 1024
BATCH_LIMIT = 10  # text-embedding-v4 accepts at most 10 inputs per call


class EmbeddingClient:
    """Minimal client for an OpenAI-compatible embeddings endpoint."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str = "text-embedding-v4",
        cache_path: Optional[str] = None,
        timeout: int = 60,
        max_retries: int = 3,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.cache_path = cache_path
        self.timeout = timeout
        self.max_retries = max_retries
        self._cache: Dict[str, List[float]] = {}
        self.calls = 0
        self.failures = 0
        if cache_path and os.path.exists(cache_path):
            try:
                self._cache = json.load(open(cache_path, encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self._cache = {}

    # -- cache ---------------------------------------------------------------
    def save(self) -> None:
        if not self.cache_path:
            return
        os.makedirs(os.path.dirname(self.cache_path) or ".", exist_ok=True)
        tmp = f"{self.cache_path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._cache, fh)
        os.replace(tmp, self.cache_path)  # atomic: readers never see a half file

    # -- http ----------------------------------------------------------------
    def _embed_batch(self, texts: Sequence[str]) -> List[Optional[List[float]]]:
        body = json.dumps({"model": self.model, "input": list(texts)}).encode()
        req = urllib.request.Request(
            f"{self.base_url}/embeddings",
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )
        for attempt in range(self.max_retries):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    data = json.loads(resp.read().decode())
                self.calls += 1
                return [item["embedding"] for item in data["data"]]
            except (urllib.error.URLError, urllib.error.HTTPError, KeyError, ValueError):
                if attempt == self.max_retries - 1:
                    self.failures += 1
                    return [None] * len(texts)
                time.sleep(2 + 2 * attempt)
        return [None] * len(texts)

    def embed(self, texts: Sequence[str]) -> List[Optional[List[float]]]:
        """Return one vector per text (None for texts that failed), cache-first."""
        out: List[Optional[List[float]]] = [self._cache.get(t) for t in texts]
        todo = sorted({t for t, v in zip(texts, out) if v is None})
        if todo:
            fetched: Dict[str, List[float]] = {}
            for i in range(0, len(todo), BATCH_LIMIT):
                chunk = todo[i : i + BATCH_LIMIT]
                for text, vec in zip(chunk, self._embed_batch(chunk)):
                    if vec is not None:
                        fetched[text] = vec
            self._cache.update(fetched)
            self.save()
            out = [self._cache.get(t) for t in texts]
        return out


class EmbeddingIndex:
    """Normalised float32 matrix + record ids, persisted as .npz."""

    def __init__(self, path: str):
        self.path = path
        self.ids: List[str] = []
        self.vectors: Optional[np.ndarray] = None
        if os.path.exists(path):
            self.load()

    # -- persistence ---------------------------------------------------------
    def load(self) -> None:
        data = np.load(self.path, allow_pickle=True)
        self.ids = [str(x) for x in data["ids"].tolist()]
        mat = np.asarray(data["vectors"], dtype=np.float32)
        self.vectors = self._normalise(mat)

    def save(self) -> None:
        if self.vectors is None:
            return
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = f"{self.path}.tmp.npz"
        np.savez(tmp, ids=np.array(self.ids), vectors=self.vectors)
        os.replace(tmp, self.path)

    # -- helpers -------------------------------------------------------------
    @staticmethod
    def _normalise(mat: np.ndarray) -> np.ndarray:
        norms = np.linalg.norm(mat, axis=1, keepdims=True)
        return mat / np.where(norms < 1e-9, 1.0, norms)

    def __len__(self) -> int:
        return len(self.ids)

    def id_set(self) -> set:
        return set(self.ids)

    def upsert(self, pairs: Sequence[Tuple[str, List[float]]]) -> int:
        """Insert vectors for new ids; existing ids keep their old vector.

        Returns how many rows were added.
        """
        if not pairs:
            return 0
        known = {i: n for n, i in enumerate(self.ids)}
        new_pairs = [(i, v) for i, v in pairs if i not in known]
        if new_pairs:
            add = np.asarray([v for _, v in new_pairs], dtype=np.float32)
            add = self._normalise(add)
            self.vectors = add if self.vectors is None else np.vstack([self.vectors, add])
            self.ids.extend(i for i, _ in new_pairs)
        # refresh vectors of known ids only when the caller explicitly re-embeds
        for rec_id, vec in pairs:
            if rec_id in known and self.vectors is not None:
                v = np.asarray(vec, dtype=np.float32)
                v = v / (np.linalg.norm(v) or 1.0)
                self.vectors[known[rec_id]] = v
        return len(new_pairs)

    def knn(
        self,
        vector: Sequence[float],
        top_k: int = 5,
        exclude: Sequence[str] = (),
        min_cos: float = 0.80,
    ) -> List[Tuple[str, float]]:
        """Return up to top_k (id, cosine) pairs above min_cos, excluding ids."""
        if self.vectors is None or not len(self.ids):
            return []
        v = np.asarray(vector, dtype=np.float32)
        norm = float(np.linalg.norm(v)) or 1.0
        v = v / norm
        sims = self.vectors @ v
        exclude_set = set(exclude)
        order = np.argsort(-sims)
        out: List[Tuple[str, float]] = []
        for idx in order:
            rec_id = self.ids[int(idx)]
            if rec_id in exclude_set:
                continue
            score = float(sims[int(idx)])
            if score < min_cos:
                break
            out.append((rec_id, score))
            if len(out) >= top_k:
                break
        return out

    def coverage(self, record_ids: Sequence[str]) -> float:
        """Fraction of record_ids present in the index (diagnostics)."""
        if not record_ids:
            return 1.0
        have = self.id_set()
        return sum(1 for r in record_ids if r in have) / len(record_ids)


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0
