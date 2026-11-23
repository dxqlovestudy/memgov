---
status: design-draft
version: 1.0
date: 2026-11-22
target: memgov 写入侧重复治理（改造 3）
related:
  - docs/zh-eval-2026-11-22.md
  - docs/design-3-write-side-dedup.html
---

# 改造 3 设计稿：写入侧重复治理

## 1. 结论摘要

- **做什么**：给 memgov 增加「增量去重」能力——每 5 分钟处理最近 10 分钟新增的 facts，用「embedding 近邻 + 数字/ID 签名」判定重复，命中即软失效。
- **不做什么**：不改 Hermes 写入插件、不动改造 1 的取代判官、不启用硬删、不做 observation 层治理。
- **最大风险**：cron 滞后 5 分钟，这 5 分钟内的重复仍会参与召回；误判靠台账 + `rollback` 兜底。

## 2. 问题与证据

| 证据 | 数值 | 采集方式 |
|---|---|---|
| 每天新增 facts | 约 330 条 | 11-22 全天 `created_at` 统计 |
| 新增中重复占比 | 62% | 42 对候选判官结果 |
| 全库 cos≥0.92 重复 | 1,135 对 / 622 条 / 153 簇 | 11,548 条全量 embedding |
| 最大重复簇 | 92 条「Multica 身份声明，仅 issue ID 不同」 | 簇内抽样 12 条 |
| 现有清扫速度 | 28 条 / 次 | 今天 `scan --apply` 实测 |
| 产生 vs 清扫 | 330 / 天 vs 28 / 次 | 差一个数量级 |

**P1 第一次 3 天回放暴露的误判类型**（阈值 0.92、无判官,84 条动作抽 20 条):

- `0.92–0.93` 区间有 20/84 条(24%),误判集中在这里
- 唯一误判:「用户要求**核实**三个 MCP 接口是否已完成」(cos 0.924) vs「用户要求把三个接口**派单实现**」——同一批接口、不同意图(核实 vs 实现),余弦无法区分
- 三视角判官对此判 3/3 `INDEPENDENT`,理由「一条是核实完成状态,另一条是派单实现,属于不同任务属性」——判官能拦住余弦拦不住的
- 因此确定性阈值从 0.92 提到 **0.95**,`0.85–0.95` 全部交判官

**现有机制为什么不够**：

1. `dedup` 策略是字面 bigram Jaccard，在 20 对人工金标上 **0/8 命中**（换词就掉分）。
2. `ai-supersede` 的候选池上限 60 对，全库 1,041 条含冲突词记录，配不齐。
3. 两者都靠人工触发，没有定时机制。

## 3. 目标与非目标

| 目标 | 非目标 |
|---|---|
| 新写入的重复在 10 分钟内被清除 | 追求 0 延迟拦截（那要改插件，见方案 A） |
| 拦截率 ≥95%，误判率 ≤1% | 不做 observation 层治理（负收益已实测） |
| 完全复用现有台账 / 回滚 / 回查机制 | 不改改造 1 的判定逻辑 |
| 不改对话关键路径 | 不引入新的 LLM 依赖（先只用确定性比对） |

## 4. 数据模型

### 4.1 新增实体

| 实体 | 字段 | 说明 |
|---|---|---|
| `EmbeddingIndex` | `ids: str[]`、`vectors: float32[n,1024]`、`sig: str[]` | npz 落盘，支持增量 append |
| `DedupCandidate` | `new_id`、`old_id`、`cos`、`sig_match`、`verdict` | 候选对 |
| `IncrementalRun` | `window_start/end`、`scanned`、`candidates`、`actions` | 每次运行报告 |

### 4.2 状态机（复用现有）

```
valid ──(cos≥0.92 且签名一致)──> invalidated
  ↑                                    │
  └──────── rollback ──────────────────┘
```

### 4.3 签名定义

```python
token_re = r"[0-9a-f]{8,}|\d+\.\d+|\d+"
sig(text) = tuple(sorted(set(token_re.findall(text))))   # 数字/ID 集合
```

**为什么加这条**：observation 层实测 673 个簇里，**325 个（48%）是因为数字/ID 不同而误判**（如「issue ID 为 01a1c487」vs「01a1c34b」cos=1.000）。`sig` 不同一律不判重复。

## 5. 流水线

| 步 | 输入 | 动作 | 输出 | 失败处理 |
|---|---|---|---|---|
| 1 | 库内 `updated_at ≥ now-10min` 的 valid 记录 | 拉增量 | `new[]` | 拉取失败 → 本轮跳过，记 warning |
| 2 | `new[]` 文本 | 取 embedding（缓存优先） | 向量 | API 失败 → 重试 3 次，仍失败则本轮跳过 |
| 3 | 向量 + 全量索引 | 余弦 KNN（cos ≥ 0.92） | 候选对 | 索引缺失 → 触发全量重建 |
| 4 | 候选对 | 签名比对（`sig` 必须相等） | 过滤后候选 | — |
| 5 | 候选对 | 生成 `Action(invalidate, policy=dedup)` | 动作 | — |
| 6 | 动作 | dry-run 只记账；apply 则 PATCH | 台账 | 单条失败继续，记 error |
| 7 | run 文件 | 60 分钟后回查 `state` | 验证结果 | 失败率 >10% → 告警 |

## 6. 契约

### 6.1 命令

```bash
memgov dedup-incremental \
  --adapter hindsight --base-url <url> --bank dxq \
  --window-minutes 10 \
  --threshold 0.95 \    # >= 0.95 直接判重复（确定性区）
  --boundary-low 0.85 \  # 0.85..0.95 交 LLM 判官（边界区）
  --require-signature \
  [--apply]          # 不带则 dry-run
```

### 6.2 输出

```json
{
  "run_id": "...",
  "window": {"start": "2026-11-22T17:00:00Z", "end": "2026-11-22T17:10:00Z"},
  "records_scanned": 18,
  "candidates": 3,
  "actions": 3,
  "applied": 3,
  "errors": 0,
  "uncertain": 0
}
```

### 6.3 不确定态

| 情况 | 标记 | 动作 |
|---|---|---|
| cos 在 0.85–0.92 | `uncertain` | **不动作**，记入报告待人工看 |
| 签名不一致 | `sig_mismatch` | 不动作 |
| embedding 取不到 | `skipped` | 不动作 |

## 7. 影响面（本改造无 AI 提示词）

| 被写字段 | 读方 | 判定方 |
|---|---|---|
| `state` | recall（只返回 valid） | `dedup-incremental` |
| `reason` | 审计 / 台账 | 同上 |
| `runs/dedup-ledger.jsonl` | memgov rollback | 同上 |
| `runs/embed-bank.npz` | KNN 查询 | 增量维护 |

**抽取侧已落地的配套**（不属于本改造，但相关）：`retain_extraction_mode=custom` + 6 条规则，解决粒度粗（拆条已实测生效）。

## 8. 动作矩阵与安全闸门

| 条件 | 动作 | 可逆 |
|---|---|---|
| cos ≥ 0.95 且 `sig` 相等且 `state=valid` | `invalidate(old_id)` + 台账 | ✅ |
| 0.85 ≤ cos < 0.95,判官 3 视角 ≥2 票 DUPLICATE 且平均置信 ≥0.80 | `invalidate(old_id)` + 台账 | ✅ |
| 0.85 ≤ cos < 0.95,判官未确认 | 标记 `uncertain`,不动作 | — |
| `sig` 不等 | 仅记录 | — |
| 记录属于最近 1 分钟内写入 | 跳过（避免与新写入竞争） | — |
| 单轮动作 > 50 条 | **中止并告警**（防索引错乱导致批量误删） | — |

**闸门**：
- `--apply` + `MEMGOV_ALLOW_WRITE=1` 双开关。
- 每轮上限 50 条（`max_actions`）。
- 台账 before 快照必填。
- 回滚：`memgov rollback runs/<run_id>.json --apply`。

## 9. 验收

### 9.1 必须命中的正例

| 样本 | 判据 |
|---|---|
| 92 条 Multica 声明簇（同 issue ID 的变体） | 应清到 1 条 |
| 「该次机检的结果为 GATE\|exit=0\|red=0\|report=reply.md」同簇 12 条 | 应清到 1 条 |
| 「状态板当前没有进行中事项」同簇 12 条 | 应清到 1 条 |

### 9.2 必须保留的反例

| 样本 | 判据 |
|---|---|
| 「issue ID 为 01a1c487」vs「01a1c34b」（cos=1.000） | **必须不动作**（签名不同） |
| 「白名单两条」vs「白名单三条」（不同属性） | 必须不动作 |
| 「模板ID可重复」vs「模板ID必填」 | 必须不动作（属性不同） |

### 9.3 量化阈值

| 指标 | 目标 | 命令 |
|---|---|---|
| 拦截率 | ≥95% 新增重复被清 | 每日 dry-run 报告「新增重复」应为 0 |
| 误判率 | ≤1% | 抽样 20 条人工复核 |
| 遗忘验证 | failure_rate ≤10% | `memgov verify-forgetting <run>` |
| 召回不退化 | MRR ≥ 0.94 | 每周 27 题 A/B |

### 9.3.1 P1 验收记录（2026-11-22 至 11-23，生产库 dxq）

| 轮次 | 窗口 | 扫描 | 动作 | 阈值/判官 | 抽样 | 误判 |
|---|---|---|---|---|---|---|
| 1 | 3 天 | 4,810 | 84 | 0.92、无判官 | 20 | 1 条（5%）→ 据此改阈值 |
| 2 | 3 天 | — | SIGBUS 中断 | 0.95、判官 | — | 无结果 |
| 3 | 1 天 | 1,193 | 52 | 0.95、判官 | 14 | 0 |
| 4 | 3 天 | 4,640 | 121 | 0.95、判官 | 30 | **0** |

第 4 轮完整计数:`duplicates 121 / uncertain 173 / signature_blocked 953 / errors 0`。

抽样 30 条中 15 条是具体业务事实(协议 181 续签批次、AccountTypeEnum 取值、
fat 的 T_PROTOCOL_CONFIG 缺 PROTOCOL_SUB_TYPE、removeNoUpdateReq 的 realName
剔除坑、agent-front→mng-agent→Apollo 上线顺序),逐条比对 OLD 与 KEEP,全部
保留等价或更精确的版本;其余 15 条是会话丢失/Multica 身份/看板地址等模板变体。

误判率:**44 条抽样(第 3+4 轮)0 误判**。

第 4 轮动作数(121)高于第 1 轮(84),原因是判官接管了 0.85–0.95 区间:
第 1 轮被 0.92 阈值挡在门外的那批对,现在经三视角确认后可以执行,不是判定变松。

### 9.4 复跑命令

```bash
# 回放过去 3 天数据做验证（不写库）
memgov dedup-incremental --window-minutes 4320 --threshold 0.92 --require-signature --json

# 单轮 dry-run
memgov dedup-incremental --window-minutes 10 --json

# 真执行
MEMGOV_ALLOW_WRITE=1 memgov dedup-incremental --window-minutes 10 --apply
```

## 10. 阶段

| 阶段 | 内容 | 退出条件 |
|---|---|---|
| P1 | 只读：拉增量 + 生成候选 + 出报告 | 历史 3 天回放，误判率 ≤1% |
| P2 | dry-run cron（每 5 分钟，只报告） | 连续 3 天无异常告警 |
| P3 | 小批量 apply（上限 10 条/轮） | 一周无误判 |
| P4 | 全量 apply（上限 50 条/轮）+ 定时 | 保持 30 天 |

## 11. 风险与待拍板项

| 序 | 决策点 | 推荐 | 影响面 | 回滚 |
|---|---|---|---|---|
| 1 | 主方案选 B（增量清扫）还是 A（写入前拦截） | **B** | A 要改 Hermes 插件，影响每轮对话 | A 未实施无需回滚 |
| 2 | 增量窗口 | 5 分钟跑一次、窗口 10 分钟 | 窗口越大越省调用，滞后越久 | 改 cron 频率 |
| 3 | 是否同时对 cos 0.85–0.92 启用 LLM 判官 | **先关闭** | 开启后成本翻倍，收益待测 | 关参数 |
| 4 | 是否用硬删 `DELETE /memories` | **不用** | 硬删不可回滚 | — |
| 5 | 每轮动作上限 | 50 条 | 越高越激进 | 改参数 |
| 6 | 签名约束是否强制 | **强制** | 不强制时 48% 的簇是误判 | 改 `--require-signature` |

### 11.1 对象状态台账

| 对象 | 所在 | 状态 |
|---|---|---|
| `src/memgov/embeddings.py` | memgov 仓库 | **新增** |
| `src/memgov/dedup_incremental.py` | memgov 仓库 | **新增** |
| `src/memgov/cli.py` | memgov 仓库 | 修改（加子命令） |
| `src/memgov/policies.py` | memgov 仓库 | 不动（复用 DedupPolicy 的 `_dup`） |
| `src/memgov/adapters/hindsight.py` | memgov 仓库 | 修改（加增量拉取参数） |
| `runs/embed-bank.npz` | 本地 | 新增（已有一次性产物，改为增量维护） |
| `~/.hermes/scripts/memgov-dedup-incremental.sh` | Hermes | **新增** |
| cron job | Hermes | **新增** |
| Hermes Hindsight 插件 | 插件目录 | **不动** |
| Hindsight 服务端 | 远端 | 不动（只调已有 PATCH） |

### 11.2 文件级实现清单

| 文件 | 动作 | 说明 |
|---|---|---|
| `src/memgov/embeddings.py` | 新增 | `EmbeddingClient`（调 text-embedding-v4，带缓存）+ `EmbeddingIndex`（npz 读写、append、KNN） |
| `src/memgov/dedup_incremental.py` | 新增 | `IncrementalDeduper`：窗口拉取 → 签名过滤 → 候选生成 → 动作；`signature()` 工具函数 |
| `src/memgov/cli.py` | 修改 | 新增 `dedup-incremental` 子命令（参数见 §6.1）；`build_adapter` 增加 `since` 参数透传 |
| `src/memgov/adapters/hindsight.py` | 修改 | `list_memories` 增加 `since: datetime` 过滤（走 `updated_at` 参数） |
| `tests/test_dedup_incremental.py` | 新增 | 签名过滤、窗口边界、上限闸门、不确定态不动作 |
| `tests/test_embeddings.py` | 新增 | npz 往返、增量 append、KNN 正确性 |
| `~/.hermes/scripts/memgov-dedup-incremental.sh` | 新增 | 调 CLI，输出摘要供 cron 投递 |
| `docs/design-3-write-side-dedup.md` | 已有 | 本文件 |
| `docs/design-3-write-side-dedup.html` | 新增 | 人读版 |
