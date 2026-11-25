# 字段定义

| 字段 | 类型 | 说明 |
|---|---|---|
| `claim_id` | text | 主键 |
| `member_id` | text | 业务字段 |
| `service_date` | date | 业务字段 |
| `payer` | text | 业务字段 |
| `billed_cents` | int | 业务字段 |
| `approved_cents` | int | 业务字段 |
| `deductible_cents` | int | 业务字段 |

空字符串、`-`、`NA`、`null` 和 `未提供` 按空值处理。金额字段在整理层转为整数分或整数最小单位；日期按 ISO 日期解析；带时间的字段按本地时间解析。标识字段保持为文本，前导零不会被去除。

## 银行到账对账

| 字段 | 类型 | 说明 |
|---|---|---|
| `payable_id` | text | 应付单号；也兼容 `id`、`ap_id`、`bill_id` |
| `status` | text | `ISSUED/APPROVED/SETTLED` 等视为已签发；`DRAFT/未签发` 不参与对账 |
| `amount_cents` | int | 应付金额（整数最小单位）；普通 `amount` 支持整数或定点小数 |
| `remaining_amount` / `remaining_cents` | int | 尚未结清的剩余份额 |
| `bank_transaction_id` | text | 到账流水号与幂等键；兼容 `transaction_id`、`flow_no`、`serial_no` |
| `received_at` / `arrival_date` | date/datetime | 银行到账日期 |
| `payable_ids` / `allocations` | list | 明确归属；`allocations` 可携带每笔 `payable_id` 与 `amount_cents` |
| `candidate_sets` | list | 金额相符的候选归属组合，用于说明“归属不唯一” |
| `allocation` | object | 已结清份额，关联银行流水、应付、金额和结清日期 |
| `suspense` / `hanging_item` | object | 挂账份额；`status=HELD` 计入 `hanging_total` |
| `replay_count` | int | 同一银行流水的重试次数；重试不新增 allocation |
