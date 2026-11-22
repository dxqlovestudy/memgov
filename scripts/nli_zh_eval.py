"""nli-conflict 中文子集评测：验证取代判定在跨数据集上的召回与假阳性。

口径说明（重要）：
- 该数据集的 true = 「矛盾、需更新旧记忆」，但它把三种情况都算 true：
  * 取值更新（改喝拿铁 / 房租涨到）  → 我们的 SUPERSEDES
  * 违约事件（戒烟了 vs 抽了烟）      → 旧记忆没被取代，只是被违反，期望 INDEPENDENT
  * 违背医嘱（胃炎忌辣 vs 点了火锅）  → 约束仍然有效，期望 INDEPENDENT
- false = 「一致或无关」：
  * compat（逻辑兼容）→ 我们的 INDEPENDENT（该数据集只标「不矛盾」，不等于「信息等价」）
  * unrelated（无关） → 我们的 INDEPENDENT
所以按 kind 分桶算指标，不把 true 一律当「应取代」。
"""
import json
import os
import random
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone

import yaml

REPO = os.path.expanduser("~/agentdt/开源项目/memgov")
sys.path.insert(0, os.path.join(REPO, "src"))

from memgov.ai import AICache, AIConfig  # noqa: E402
from memgov.ai_supersede import SupersedeJudge  # noqa: E402
from memgov.models import MemoryRecord  # noqa: E402

SRC = os.path.join(REPO, "data/zh/nli_conflict_train.jsonl")
N_PER_BUCKET = 8  # 每个 kind 抽多少（8 类 × 8 = 64 条，192 次判官调用）

# 期望映射：kind -> 我们期望的结论
EXPECT = {
    "true": "SUPERSEDES", "cf_true": "SUPERSEDES", "perturb": "SUPERSEDES",
    "negation": "INDEPENDENT", "neg_true": "INDEPENDENT",
    "hardconstraint": "INDEPENDENT",
    # compat 在该数据集里是「逻辑兼容」，不是「信息等价」：
    # 例："车是小鹏" vs "保险单上写小鹏" 算兼容，但很多条只是相关不同属性
    # （"猫叫煤球" vs "猫是橘猫"），所以期望是 INDEPENDENT 而不是 DUPLICATE。
    "compat": "INDEPENDENT", "unrelated": "INDEPENDENT",
}


def zh_ratio(s):
    return len(re.findall(r"[\u4e00-\u9fff]", s)) / max(1, len(s))


rows = []
with open(SRC, encoding="utf-8", errors="ignore") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        st = json.loads(r["state"])
        if zh_ratio(st["known"] + st["new"]) <= 0.3:
            continue
        kind = (r.get("meta") or {}).get("kind", "")
        if kind not in EXPECT:
            continue
        rows.append({"known": st["known"], "new": st["new"], "kind": kind,
                     "cat": (r.get("meta") or {}).get("cat", "")})

by_kind = defaultdict(list)
for r in rows:
    by_kind[r["kind"]].append(r)
random.seed(20261122)
picked = []
for _kind, items in sorted(by_kind.items()):
    random.shuffle(items)
    picked.extend(items[:N_PER_BUCKET])
print(f"候选 {len(rows)} 条，按 kind 抽样后 {len(picked)} 条：{dict(Counter(x['kind'] for x in picked))}")

cfg = yaml.safe_load(open(os.path.expanduser("~/.hermes/config.yaml"), encoding="utf-8"))
prov = next(x for x in cfg["custom_providers"] if x["name"] == "jl-ds-flash")
config = AIConfig(
    base_url=prov["base_url"], api_key=prov["api_key"], model="deepseek-flash",
    confidence_threshold=0.85, cache_path=os.path.join(REPO, "runs/nli-zh-cache.json"),
    prompt_version="supersede-v2",
)
judge = SupersedeJudge(config, cache=AICache(config.cache_path))
t0 = datetime(2026, 11, 1, tzinfo=timezone.utc)

out = []
for i, row in enumerate(picked, 1):
    old = MemoryRecord(id=f"nli-old-{i}", text=row["known"], kind="world", created_at=t0)
    new = MemoryRecord(id=f"nli-new-{i}", text=row["new"], kind="world", created_at=t0)
    vs = judge.judge(new, old, [], "")
    sup = [v for v in vs if v.verdict == "SUPERSEDES"]
    dup = [v for v in vs if v.verdict == "DUPLICATE"]
    sr = len(sup) / len(vs) if vs else 0.0
    sc = sum(v.confidence for v in sup) / len(sup) if sup else 0.0
    dr = len(dup) / len(vs) if vs else 0.0
    dc = sum(v.confidence for v in dup) / len(dup) if dup else 0.0
    if sr >= 0.6 and sc >= 0.85:
        verdict = "SUPERSEDES"
    elif dr >= 0.6 and dc >= 0.80:
        verdict = "DUPLICATE"
    else:
        verdict = "INDEPENDENT"
    out.append({"kind": row["kind"], "cat": row["cat"], "known": row["known"], "new": row["new"],
                "expect": EXPECT[row["kind"]], "verdict": verdict,
                "sup": [round(sr, 2), round(sc, 2)], "dup": [round(dr, 2), round(dc, 2)]})
    if i % 10 == 0:
        print(f"  进度 {i}/{len(picked)}")

path = os.path.join(REPO, "runs/nli-zh-60.json")
json.dump(out, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

print("\n== 按 kind 分桶（期望 vs 实际）==")
by = defaultdict(Counter)
for x in out:
    by[x["kind"]][x["verdict"]] += 1
for kind, c in sorted(by.items()):
    total = sum(c.values())
    row = " / ".join(f"{k}={v}" for k, v in c.most_common())
    print(f"  {kind:14s} (n={total:2d}, 期望 {EXPECT[kind]:11s}) {row}")

print("\n== 关键指标 ==")
update = [x for x in out if x["expect"] == "SUPERSEDES"]
noupd = [x for x in out if x["expect"] == "INDEPENDENT"]
compat = [x for x in out if x["expect"] == "DUPLICATE"]
hit = sum(1 for x in update if x["verdict"] == "SUPERSEDES")
fp = sum(1 for x in noupd + compat if x["verdict"] == "SUPERSEDES")
print(f"  取值更新（应取代）召回: {hit}/{len(update)} = {hit/max(1,len(update)):.1%}")
print(f"  不该取代却被判取代（假阳性）: {fp}/{len(noupd)+len(compat)} = {fp/max(1,len(noupd)+len(compat)):.1%}")
dup_hit = sum(1 for x in compat if x["verdict"] == "DUPLICATE")
print(f"  一致类（compat）判为重复: {dup_hit}/{len(compat)} = {dup_hit/max(1,len(compat)):.1%}")
print(f"\n结果: {path}")
print("usage:", json.dumps(judge.usage(), ensure_ascii=False))
