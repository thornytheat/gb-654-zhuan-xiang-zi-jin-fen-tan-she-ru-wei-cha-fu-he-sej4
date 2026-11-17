# 数据字典

| 字段 | 类型 | 可空 | 定义 |
|---|---|---:|---|
| `run_id` | string | 否 | 输入摘要派生的稳定运行编号 |
| `source_line` | integer | 否 | 原文件物理行号 |
| `reason_code` | string | 是 | 隔离或冲突的稳定原因码 |
| `raw_value` | string | 是 | 未归一的原始证据 |
| `normalized_value` | string | 是 | 按项目规则得到的值 |
| `rule_version` | string | 否 | 计算规则版本 |

字符串编号逐字保留。金额与计量值用 `Decimal`。时间值带偏移并以 UTC 参与比较。区间统一采用左闭右开。

## M7 资金覆盖对账

| 字段 | 类型 | 可空 | 定义 |
|---|---|---:|---|
| `ledger_id` | string | 否 | 资金覆盖台账编号，建账后稳定不变 |
| `commitment_id` | string | 否 | 项目获资承诺编号；承诺金额在撤销流程中不被削减 |
| `source_id` | string | 否 | 只能投向指定项目的资金来源编号 |
| `cap` | decimal | 否 | 来源封顶额度，到分；来源占用合计不得超过 |
| `locked` | boolean | 否 | 锁定来源的占用不参与任何重排，撤销流程保持其金额 |
| `revoked` | boolean | 否 | 来源撤销标记；撤销后停止占用其余额，既有承诺仍有效 |
| `occupation` | decimal | 否 | 某承诺在某来源上的占用边 `(commitment_id, source_id)` |
| `gap` | decimal | 否 | 承诺金额减已覆盖金额；替补不足时保留，不改小承诺 |
| `rev_seq` | integer | 否 | 撤销裁决序号，写事务内单调分配，事件只增不改 |
| `idempotency_key` | string | 是 | 幂等键；同键重试返回原裁决，不重复占款 |

守恒式：`承诺金额 = Σ来源占用 + gap`；`Σ来源占用 ≤ cap`。
原因码：`GAP_NO_ELIGIBLE_SOURCE`（无合规可用来源）、`GAP_SOURCE_CAPACITY`（合规来源余额不足）、
`SOURCE_REVOKED`、`SOURCE_LOCKED`。事件动作：`REVOKE / REPLACE / RESERVE / ADJUST / RELEASE / LOCKED_KEPT / GAP`，
每条事件带 `before`、`after` 原值用于回读。
