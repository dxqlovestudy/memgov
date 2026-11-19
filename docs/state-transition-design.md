---
title: 记忆状态迁移治理总设计
status: implemented-dry-run
version: 0.1
date: 2026-11-13
target: memgov v0.3 (`ai-transition`)
related:
  - docs/design.md
  - docs/system-architecture.md
  - docs/ai-governance.md
  - docs/policies.md
  - docs/adapters.md
---

# 记忆状态迁移治理总设计

> 实现进度：P1/P2（候选 + `transition-v1` dry-run）已于 2026-11-19 落地；P3 apply 未执行。

## 0. 摘要

现有 `memgov` 能处理「显式取代」「文本重复」「两两语义冲突」，但处理不了**一对多的状态迁移**：一条新事实宣告某实体下线/迁移/改名后，所有「该实体仍是当前入口/在役/位于」的旧断言应当一起失效。

本设计新增一层 `ai-transition`：

1. 识别迁移事实（T）；
2. 抽取受控 scope 实体（主机/IP/域名/服务/业务ID/环境/路径/表）；
3. 按同一 canonical entity 反查更早的「当前状态断言」（A），**不看文本相似度**；
4. 用专用提示词 `transition-v1` 判 `obsolete_current / historical_ok / unrelated / uncertain`；
5. 只失效 `obsolete_current` 且置信度 ≥ 0.90 的旧断言；
6. 全自动执行、审计、回查、失败自动回滚。

本设计不改变现有规则策略与 `ai-govern` 路径；`ai-transition` 是独立的一次扫描。

## 1. 问题复现（实测证据）

会话 `local-fa757010305bc598638d8b6990daf7ade2e5936b44b227973a77b1276180734d`：

- 文档创建 2026-11-12 08:15，最后写入 2026-11-13 03:10；
- 94 条 world/experience 记忆；
- 旧口径事实仍为 `state=valid`。

查询「合伙人端联调入口地址是什么」的召回前 6：

| reranker | 记忆 ID | 内容 | 性质 |
|---|---|---|---|
| 0.960 | `0b92f28e` | 用户确定联调统一走 dev，合伙人端入口为 `https://agent-dev.jlpay.com/p/agent/` | 新口径 |
| 0.939 | `af075bd3` | 同上，运营端 `manage-dev.jlpay.com/busioperat/` | 新口径 |
| 0.925 | `eb950249` | 合伙人前端联调环境位于 aether-vm（172.20.12.116） | **旧口径，仍 valid** |
| 0.877 | `bb9d833d` | 旧 nginx 静态目录 `/srv/partner-web/dist` | 旧实现细节 |
| 0.854 | `bae6789f` | VM 已于 11-12 下线，统一走 dev | 新口径 |
| 0.853 | `36b0f8c4` | 合伙人前端联调环境（aether-vm 172.20.12.116） | **旧口径，仍 valid** |

其他仍为 `valid` 的旧事实：`be0c154b` / `3fafdef2`（172.20.12.116:80 是总入口）、`9e961281`（VM env2 入口 `:8085/busioperat`）、`ec01d8cd`（VM 全承载联调环境）。

关键反证：

```
candidate_pairs(old=eb950249, new=bae6789f) -> []
```

即现有 AI 治理不会把这对旧/新口径放进候选，模型没有机会判断。

## 2. 根因

| 原因 | 说明 |
|---|---|
| 一对多，不是两两 | 一条迁移事实应影响多条旧断言；现有 `ai-govern` 只做 pair |
| 实体不同名 | 旧事实写 IP `172.20.12.116`，新事实写域名 `agent-dev.jlpay.com`；无共同锚点 |
| 文本相似度低 | 一条「位于 aether-vm」，一条「已下线、统一走 dev」；bigram 重叠不足 |
| 跨来源 | 旧事实 `document_id=None`（迁移自 WeKnora），新事实在当前会话文档；按会话缩小会漏 |
| 状态无生命周期 | Hindsight 只区分 `valid/invalidated`，召回按相关度排序，不按时间判新旧 |

朴素正则方案不可用：对 5,607 条有效事实按「下线/停用/迁移 + VM/1092143/IP/入口」匹配，得到 **2,748 条候选旧事实**，噪声过大，会误杀 Hindsight 服务、Wi-Fi、端口约定等无关事实。

## 3. 目标与非目标

### 目标

1. 识别状态迁移并绑定 scope。
2. 以 scope 反查旧「当前状态断言」，不依赖文本相似度。
3. 用专用 AI 判官区分「当前已过时」与「历史记录」。
4. 全自动失效、可回滚、可回查、可审计。
5. 对现有规则策略零侵入，复用 adapter/ledger/verify/rollback。

### 非目标

1. 不判断记忆内容真假。
2. 不删除任何记录（只失效）。
3. 不替换召回排序或 re-ranker。
4. 不在写入侧做同步拦截（第一阶段仍是批量扫描）。
5. 不在第一阶段处理「阈值/次数」类口径修订（第二阶段）。

## 4. 术语与数据模型

### 4.1 `TransitionFact`（T）

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | str | 迁移事实的记忆 ID |
| `transition_type` | enum | OFFLINE / DISABLE / MIGRATE / RENAME / REPLACE / SCOPE_CHANGE |
| `scope_entities` | list[ScopeEntity] | 受影响的实体 |
| `effective_at` | datetime? | 迁移生效时间（从原文抽取，可为空） |
| `evidence` | str | 原文中的迁移句 |
| `document_id` | str? | 来源文档 |
| `mentioned_at` | datetime? | 事实时间 |

### 4.2 `ScopeEntity`（S）

| 字段 | 类型 | 说明 |
|---|---|---|
| `type` | enum | HOST / IP / DOMAIN / SERVICE / BUSINESS_ID / ENV / PATH / TABLE / DOC |
| `canonical_id` | str | 归一后的实体 ID，如 `host:aether-vm` |
| `aliases` | list[str] | 命中该实体的原文写法 |
| `source` | enum | anchor / alias_table / tag |

别名表是唯一的跨写法归一手段，必须人工维护，不做自动实体解析。

示例：

```yaml
aliases:
  host:aether-vm:
    - "aether-vm"
    - "172.20.12.116"
    - "100.69.57.44"
    - "VM"
  env:dev:
    - "agent-dev.jlpay.com"
    - "manage-dev.jlpay.com"
    - "dev 环境"
```

### 4.3 `StateAssertion`（A）

| 字段 | 类型 | 说明 |
|---|---|---|
| `subject` | ScopeEntity | 主语实体 |
| `predicate` | enum | ENTRY / LOCATION / ACTIVE / ADDRESS / ACCESS / USAGE / VERSION / OWNERSHIP |
| `polarity` | enum | POSITIVE / NEGATIVE |
| `tense` | enum | CURRENT / HISTORICAL / UNKNOWN |
| `time_marker` | str? | 「2026-11-12 实测」「当时」「此前」「曾」 |
| `evidence` | str | 原文连续片段 |

`tense` 只作为 AI 的提示，不单独决定动作；最终由 `transition-v1` 判。

### 4.4 `TransitionVerdict`

```json
{
  "verdict": "obsolete_current | historical_ok | unrelated | uncertain",
  "scope_entity": "host:aether-vm",
  "confidence": 0.0,
  "evidence_old": "<A 原文连续片段>",
  "evidence_transition": "<T 原文连续片段>",
  "historical_markers": ["<原文标记>"],
  "reason": "<= 80 字"
}
```

## 5. 处理流水线

1. **读快照**：列出全部 `state=valid` 的 world/experience；记录 `created_at`、`mentioned_at`、`document_id`。
2. **识别迁移事实**：正则初筛迁移动词，AI 分类器确认（排除否定句、假设句）。
3. **抽取 scope**：受控实体词典 + 锚点 + 别名归一；不做通用分词。
4. **scope 反查**：同一 `canonical_id`、时间早于 T 的所有旧事实。
5. **状态断言过滤**：旧事实需含当前状态措辞；纯历史措辞单独标记，交给 AI 判 `historical_ok`。
6. **AI 判官**：`transition-v1`，一次一对（T → A），输出 verdict + 双方原文引用。
7. **机器闸门**：schema、证据子串、置信度、时间先后、authority、单轮上限；apply 前重读 `state/updated_at`。
8. **执行与回查**：只 `invalidate`；写台账；apply 后召回回查，F > 10% 自动回滚。

## 6. 候选扩展算法

```text
transitions = detect_transitions(valid_facts)
actions = []
for T in transitions[:max_transitions]:
    for S in T.scope_entities:
        olds = entity_index[S.canonical_id].older_than(T)
        olds = [A for A in olds if A.state == valid and has_state_marker(A.text)]
        ranked = rank(olds, key=(same_entity_type, shared_scope_count, recency))
        for A in ranked[:max_candidates_per_transition]:
            verdict = judge_transition(T, A)
            if gate(verdict) == PASS and verdict.verdict == "obsolete_current":
                actions.append(invalidate(A, reason=f"state_transition:{T.id}"))
```

约束：

| 参数 | 默认值 | 作用 |
|---|---|---|
| `max_transitions` | 20 | 单轮迁移事实上限 |
| `max_candidates_per_transition` | 30 | 单条迁移的旧断言上限 |
| `max_invalidations` | 10（首周 3） | 爆炸半径 |
| `confidence` | 0.90 | 状态迁移误杀代价更高 |
| `verify_failure_threshold` | 0.10 | 回查失败率阈值 |
| `max_pairs`（总判官调用） | 200 | 成本上限 |

去重：同一旧事实若被多条迁移事实命中，只保留证据最强的一条，其余记 `shadowed_by`。

## 7. 判定契约与提示词

### 7.1 `transition-v1` 系统提示词

```text
你是记忆状态迁移裁判（transition-v1）。

输入：一条迁移事实 T（较新）和一条旧断言 A（较早）。
任务：判断 A 的"当前状态"是否已被 T 废止。

定义：
- obsolete_current：A 把某实体描述为当前入口/在用/位于/可访问/有效，
  而 T 宣告同一实体已下线/停用/迁移/改名/不再使用。
- historical_ok：A 明确是历史记录（带具体过去时间、"当时/实测/此前/曾"），
  或描述的是不受 T 影响的属性（如主机仍存在、其他服务仍在运行）。
- unrelated：实体或属性不同，或 T 没有覆盖 A。
- uncertain：证据不足。

硬规则：
1. 只依据 A/T 原文，不引入外部知识。
2. 必须引用 A 原文作为 evidence_old，引用 T 原文作为 evidence_transition。
3. 主机仍存在 ≠ 联调入口仍有效；服务仍运行 ≠ 旧入口仍可用。
4. 带明确过去时间且语义为历史记录的，判 historical_ok。
5. 不确定判 uncertain；只输出 JSON。
```

### 7.2 动作矩阵

| verdict | 置信度 | 动作 |
|---|---|---|
| `obsolete_current` | ≥ 0.90 | 失效 A；reason=`state_transition:<T.id>`；replacement=T.id |
| `obsolete_current` | < 0.90 | 不动作；记 `low_confidence` |
| `historical_ok` | 任意 | 不失效；可选打 `status:historical` + `valid_until` |
| `unrelated` / `uncertain` | 任意 | 不动作 |
| A 带 `authority:official` | 任意 | 不自动失效；记 `authority_conflict` |

## 8. 安全与自动回滚

| 闸门 | 规则 |
|---|---|
| 只失效不删除 | 所有动作是 `invalidate`，可 `PATCH state=valid` 回滚 |
| 证据 | `evidence_old` / `evidence_transition` 必须是原文连续子串 |
| 时间 | 只允许失效早于 T 的事实 |
| authority | 旧事实带 `authority:official` 时不自动失效 |
| 并发 | apply 前重读 A 的 `state` / `updated_at`，变化则跳过并告警 |
| 注入 | 原文含「忽略以上指令 / system prompt」时跳过该对 |
| 上限 | transitions / candidates / invalidations / judgments 四重上限 |
| 回查 | 失效后再召回；同 id 或近似原文命中即失败 |
| 自动回滚 | F > 0.10 时恢复本轮全部失效 |
| 审计 | run JSON + `ledger.jsonl`，含 T、A、scope、verdict、confidence、evidence、model、prompt 版本 |

## 9. 与现有模块的关系

| 模块 | 处理 |
|---|---|
| `adapters/` | 复用；Hindsight 仍只暴露 world/experience |
| `ai.py` schema/证据校验、缓存、token 统计 | 复用 |
| `ledger.py` run JSON / rollback / verify_forgetting | 复用 |
| `ai_governor.py` apply + auto-rollback | 复用 |
| `transitions.py` | 新增：迁移识别、scope 抽取、别名归一、候选扩展 |
| `prompts.py` | 新增 `transition-v1`（与 `conflict-v1` 并存） |
| `ai.py` | 新增 transition decision 解析与动作转换 |
| `ai_governor.py` | 新增 `TransitionGovernor` |
| `cli.py` | 新增 `memgov ai-transition` |

不修改现有 `ttl / supersede / dedup / importance / scope_limit` 的行为。

## 10. 验收标准

### 10.1 必须抓到并判 `obsolete_current`

| 旧事实 | 内容 |
|---|---|
| `eb950249` | 合伙人前端联调环境位于 aether-vm（172.20.12.116） |
| `36b0f8c4` | 合伙人前端联调环境（aether-vm 172.20.12.116） |
| `be0c154b` / `3fafdef2` | 172.20.12.116:80 是总入口 |
| `9e961281` | VM env2 入口 `:8085/busioperat` |

### 10.2 必须判 `historical_ok` 或 `unrelated`，不得失效

| 旧事实 | 内容 | 期望 |
|---|---|---|
| `80a7f59c` | Aether VM 的 IP / SSH 访问方式 | unrelated（仍成立） |
| `d1639a4f` | VM 上 Hindsight 8888 / Phoenix 6006 / Excalidraw 6011 | unrelated（仍成立） |
| `6ba243e4` | Wi-Fi / SSH 连通性 | unrelated |
| `48447b02` | VM 上在役… 2026-11-12 实测 | historical_ok |

### 10.3 量化指标

| 指标 | 目标 |
|---|---|
| 30 对人工标注样本的 `obsolete_current` 精确率 | ≥ 90% |
| 已知旧口径召回率 | ≥ 80% |
| 同输入重跑动作集合一致性 | 100%（temperature=0 + prompt 版本 + 缓存） |
| 单对 token 成本 | 900–1,400（预估；conflict-v1 实测 839–849） |
| 回查失败率 | ≤ 10% |

## 11. 分阶段实施

| 阶段 | 内容 | 是否写库 |
|---|---|---|
| P1 | 迁移识别 + scope 索引 + 候选扩展（不调 AI） | 否 |
| P2 | `transition-v1` 判官 + 证据/置信度闸门 + dry-run | 否 |
| P3 | apply + verify + auto-rollback，首周 `max_invalidations=3` | 是 |
| P4 | 提高上限、接定时任务、`status:historical` 过滤 | 是 |

## 12. 待拍板事项

| # | 问题 | 选项 | 推荐 |
|---|---|---|---|
| 1 | 历史事实如何处理 | A 只失效当前状态，历史保留打标；B 历史也失效；C 只打标不失效（需改召回过滤） | **A** |
| 2 | 失效范围 | 只当前会话 / 全库跨会话 | **全库**（旧事实 `document_id=None`） |
| 3 | 实体别名表 | 人工维护 / 自动实体解析 | **人工维护** |
| 4 | authority 标签 | 给画像/领域文档打 `authority:official` / 不打 | **打** |
| 5 | 口径修订类（阈值/次数） | 纳入本设计 / 第二阶段 | **第二阶段** |

## 13. 风险与未覆盖

| 风险 | 缓解 |
|---|---|
| 误杀仍成立的旧事实（VM 主机仍存在） | 状态断言过滤 + `transition-v1` + 置信度 0.90 + 回查回滚 |
| 别名漏配导致抓不到 | 别名表评审；未命中只报告，不动作 |
| 历史证据丢失 | 推荐 A；`historical_ok` 打 `status:historical` |
| 候选爆炸 | scope 索引 + 三重上限；朴素正则 2,748 条的经验已证明不可用 |
| 模型判错 | 证据子串闸门只能防编造；需人工抽检首周 run JSON |
| 快照非事务 | apply 前重读 `state/updated_at` |
| 口径修订类（阈值/次数） | 第二阶段单独设计，不混入本流程 |

## 14. 附录：实测原始输出

```text
# 会话记忆量
document local-fa7570…0734d: memory_unit_count = 94

# 召回（query=合伙人端联调入口地址是什么）
0.960 0b92f28e 用户确定联调/验收统一走 dev 环境，合伙人端入口为 https://agent-dev.jlpay.com/p/agent/。
0.939 af075bd3 用户确定联调/验收取统一走 dev 环境…
0.925 eb950249 合伙人前端联调环境位于 aether-vm（172.20.12.116）。
0.877 bb9d833d 合伙人前端联调环境的 nginx 静态目录为 /srv/partner-web/dist…
0.854 bae6789f 1092143 VM 联调环境已于 2026-11-12 下线…
0.853 36b0f8c4 合伙人前端联调环境（aether-vm 172.20.12.116）…

# 候选配对
candidate_pairs(old=eb950249, new=bae6789f) -> []

# 朴素状态迁移扫描
valid world+experience = 5,607
transition facts = 517
naive candidate old facts = 2,748
```
