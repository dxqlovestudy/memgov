"""Evaluate the supersede judge against the STALE benchmark.

STALE (arXiv 2605.06527) ships 400 expert-validated implicit-conflict scenarios:
``M_old`` is a belief that a later observation ``M_new`` invalidates without any
explicit negation. Ground truth is therefore "this pair must resolve to the newer
state", i.e. our ``SUPERSEDES`` verdict.

Positive pairs come straight from the dataset. Negative pairs are cross-paired
(A's old belief against B's new observation, different uid) — they should resolve
to ``INDEPENDENT``, which is the only way to measure a false-positive rate; the
dataset itself has no negatives.

The judge runs exactly as in production: three framings, SUPERSEDES ratio >= 0.6,
mean confidence >= 0.90, evidence must be a substring of the quoted record.

Usage:
    MEMGOV_AI_API_KEY=... /usr/bin/python3 scripts/stale_eval.py \
        --data ~/agentdt/开源项目/memgov/data/stale/T1_T2_400_FULL.json \
        --positives 30 --ai-base-url https://oneapi.xgd.com/v1 --ai-model deepseek-flash
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from memgov.ai import AICache, AIConfig  # noqa: E402
from memgov.ai_supersede import SupersedeJudge  # noqa: E402
from memgov.models import MemoryRecord  # noqa: E402

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def make_pair(uid: str, old_text: str, new_text: str) -> Tuple[MemoryRecord, MemoryRecord]:
    old = MemoryRecord(id=f"{uid}-old", text=old_text, kind="world", created_at=T0, updated_at=T0)
    new = MemoryRecord(
        id=f"{uid}-new", text=new_text, kind="world",
        created_at=T0 + timedelta(days=30), updated_at=T0 + timedelta(days=30),
    )
    return new, old


def build_sets(data: List[dict], positives: int, negatives: int, seed: int):
    rng = random.Random(seed)
    shuffled = list(data)
    rng.shuffle(shuffled)
    pos = shuffled[:positives]
    neg = []
    for i in range(min(negatives, len(shuffled) - 1)):
        a, b = shuffled[i], shuffled[(i + 1) % len(shuffled)]
        if a["uid"] == b["uid"]:
            continue
        neg.append({"uid": f"neg-{a['uid'][:8]}-{b['uid'][:8]}", "M_old": a["M_old"], "M_new": b["M_new"], "type": "cross-paired"})
    return pos, neg


def evaluate(judge: SupersedeJudge, rows: List[dict], expect: str, min_conf: float, threshold: float) -> Dict:
    out = {"total": 0, "correct": 0, "supersedes": 0, "mean_conf": 0.0, "rows": []}
    confs = []
    for row in rows:
        new, old = make_pair(row["uid"], row["M_old"], row["M_new"])
        verdicts = judge.judge(new, old, [], "dataset")
        sup = [v for v in verdicts if v.verdict == "SUPERSEDES"]
        ratio = len(sup) / len(verdicts) if verdicts else 0.0
        mean_conf = sum(v.confidence for v in sup) / len(sup) if sup else 0.0
        accepted = bool(verdicts) and ratio >= threshold and mean_conf >= min_conf
        got = "SUPERSEDES" if accepted else "INDEPENDENT"
        out["total"] += 1
        out["correct"] += int(got == expect)
        out["supersedes"] += int(got == "SUPERSEDES")
        confs.append(mean_conf)
        out["rows"].append(
            {
                "uid": row["uid"],
                "type": row.get("type", "-"),
                "expect": expect,
                "got": got,
                "ratio": round(ratio, 3),
                "mean_conf": round(mean_conf, 3),
                "reasons": [v.reason for v in verdicts][:3],
            }
        )
    out["mean_conf"] = round(sum(confs) / len(confs), 3) if confs else 0.0
    out["accuracy"] = round(out["correct"] / out["total"], 3) if out["total"] else 0.0
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=os.path.expanduser("~/agentdt/开源项目/memgov/data/stale/T1_T2_400_FULL.json"))
    ap.add_argument("--positives", type=int, default=30)
    ap.add_argument("--negatives", type=int, default=15)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--confidence", type=float, default=0.85)
    ap.add_argument("--vote-threshold", type=float, default=0.6)
    ap.add_argument("--ai-base-url", default=os.environ.get("MEMGOV_AI_BASE_URL") or os.environ.get("OPENAI_BASE_URL"))
    ap.add_argument("--ai-model", default=os.environ.get("MEMGOV_AI_MODEL") or "")
    ap.add_argument("--ai-cache", default=os.path.expanduser("~/agentdt/开源项目/memgov/runs/stale-ai-cache.json"))
    ap.add_argument("--out", default=os.path.expanduser("~/agentdt/开源项目/memgov/runs/stale-eval.json"))
    args = ap.parse_args()

    api_key = os.environ.get("MEMGOV_AI_API_KEY") or os.environ.get("OPENAI_API_KEY") or ""
    if not args.ai_base_url or not args.ai_model or not api_key:
        raise SystemExit("--ai-base-url/--ai-model and MEMGOV_AI_API_KEY (or OPENAI_API_KEY) are required")

    data = json.load(open(args.data, encoding="utf-8"))
    pos_rows, neg_rows = build_sets(data, args.positives, args.negatives, args.seed)

    config = AIConfig(
        base_url=args.ai_base_url, api_key=api_key, model=args.ai_model,
        confidence_threshold=args.confidence, cache_path=args.ai_cache,
        prompt_version="supersede-v1",
    )
    judge = SupersedeJudge(config, cache=AICache(config.cache_path))

    print(f"STALE 评测：正样本 {len(pos_rows)} 条（应 SUPERSEDES），负样本 {len(neg_rows)} 条（应 INDEPENDENT）")
    pos = evaluate(judge, pos_rows, "SUPERSEDES", args.confidence, args.vote_threshold)
    neg = evaluate(judge, neg_rows, "INDEPENDENT", args.confidence, args.vote_threshold)

    report = {
        "dataset": "STALEproj/STALE T1_T2_400_FULL.json",
        "params": {
            "positives": len(pos_rows), "negatives": len(neg_rows), "seed": args.seed,
            "min_confidence": args.confidence, "vote_threshold": args.vote_threshold,
            "model": args.ai_model,
        },
        "positive_recall": pos["accuracy"], "positive_mean_conf": pos["mean_conf"],
        "negative_specificity": neg["accuracy"], "negative_mean_conf": neg["mean_conf"],
        "false_positive_rate": round(1 - neg["accuracy"], 3),
        "usage": judge.usage(),
        "positives": pos,
        "negatives": neg,
    }
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    json.dump(report, open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    print(f"\n正样本召回（应判 SUPERSEDES）: {pos['accuracy']*100:.1f}%  ({pos['correct']}/{pos['total']})")
    print(f"负样本特异度（应判 INDEPENDENT）: {neg['accuracy']*100:.1f}%  ({neg['correct']}/{neg['total']})")
    print(f"假阳性率: {report['false_positive_rate']*100:.1f}%")
    print(f"平均置信度: 正 {pos['mean_conf']}  负 {neg['mean_conf']}")
    print(f"用量: {json.dumps(judge.usage(), ensure_ascii=False)}")
    print(f"报告: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
