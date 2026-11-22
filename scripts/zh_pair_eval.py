import json
import os
import sys

import yaml

sys.path.insert(0, os.path.expanduser("~/agentdt/开源项目/memgov/src"))
from datetime import datetime, timezone  # noqa: E402

from memgov.ai import AICache, AIConfig  # noqa: E402
from memgov.ai_supersede import SupersedeJudge  # noqa: E402
from memgov.models import MemoryRecord  # noqa: E402

cfg = yaml.safe_load(open(os.path.expanduser("~/.hermes/config.yaml"), encoding="utf-8"))
p = next(x for x in cfg["custom_providers"] if x["name"] == "jl-ds-flash")
pairs_path = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser(
    "~/agentdt/开源项目/memgov/data/zh/supersede_pairs_20_value.json"
)
pairs = json.load(open(pairs_path, encoding="utf-8"))
config = AIConfig(
    base_url=p["base_url"], api_key=p["api_key"], model="deepseek-flash",
    confidence_threshold=0.85, cache_path=os.path.expanduser("~/agentdt/开源项目/memgov/runs/zh20-cache.json"),
    prompt_version="supersede-v1",
)
judge = SupersedeJudge(config, cache=AICache(config.cache_path))
out = []
for row in pairs:
    t0 = datetime(2026, 11, 1, tzinfo=timezone.utc)
    old = MemoryRecord(id=row["old_id"], text=row["old_text"], kind="world", created_at=t0)
    new = MemoryRecord(id=row["new_id"], text=row["new_text"], kind="world", created_at=t0)
    verdicts = judge.judge(new, old, [], "zh-set")
    sup = [v for v in verdicts if v.verdict == "SUPERSEDES"]
    dup = [v for v in verdicts if v.verdict == "DUPLICATE"]
    ratio = len(sup) / len(verdicts)
    conf = sum(v.confidence for v in sup) / len(sup) if sup else 0.0
    dup_ratio = len(dup) / len(verdicts)
    dup_conf = sum(v.confidence for v in dup) / len(dup) if dup else 0.0
    accepted = ratio >= 0.6 and conf >= 0.85
    # duplicates sit 0.05 below the supersede gate: invalidating a restatement
    # loses no information, and the judge scores restatements lower (see governor)
    dup_accepted = (not accepted) and dup_ratio >= 0.6 and dup_conf >= 0.80
    kind = "SUPERSEDES" if accepted else ("DUPLICATE" if dup_accepted else "INDEPENDENT")
    out.append({"i": row["i"], "ratio": round(ratio, 3), "conf": round(conf, 3),
                "dup_ratio": round(dup_ratio, 3), "dup_conf": round(dup_conf, 3),
                "accepted": accepted, "duplicate_accepted": dup_accepted, "kind": kind,
                "verdicts": [v.verdict for v in verdicts],
                "reasons": [v.reason for v in verdicts][:3]})
    print(f"[{row['i']:02d}] sup={ratio:.2f}/{conf:.2f} dup={dup_ratio:.2f}/{dup_conf:.2f} -> {kind}")
out_path = sys.argv[2] if len(sys.argv) > 2 else os.path.expanduser("~/agentdt/开源项目/memgov/runs/zh20-eval.json")
json.dump(out, open(out_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print("\n取代接受:", sum(1 for x in out if x["accepted"]), "/", len(out))
print("重复接受:", sum(1 for x in out if x["duplicate_accepted"]), "/", len(out))
print("usage:", json.dumps(judge.usage(), ensure_ascii=False))
