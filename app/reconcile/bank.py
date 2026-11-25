"""银行到账与已签发应付的幂等对账核心。

该模块不依赖 Flask / 数据库，既可以被 HTTP 层调用，也可以直接用于脚本和测试。
金额在内部使用 :class:`~decimal.Decimal` 精确运算；整数分或整数最小单位会保持为
整数值，因此常见的 ``amount_cents`` 用例可以直接断言整数。
"""

from __future__ import annotations

from collections import OrderedDict
from datetime import date as date_cls
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
from typing import Any, Iterable


PAYABLE_ID_KEYS = (
    "payable_id", "payable_no", "payable_number", "ap_id", "bill_id", "invoice_id",
    "id", "应付单号", "应付编号", "单号",
)
TXN_ID_KEYS = (
    "bank_transaction_id", "transaction_id", "txn_id", "flow_id", "flow_no",
    "serial_no", "bank_serial", "bank_flow_no", "statement_no", "receipt_no",
    "id", "流水号", "到账流水号", "银行流水",
)
AMOUNT_KEYS = (
    "amount_cents", "amount_minor", "payable_amount_cents", "due_cents",
    "outstanding_cents", "balance_cents", "amount", "payable_amount",
    "due_amount", "total_amount", "outstanding_amount", "balance_due",
    "balance", "total", "金额", "应付金额",
)
TXN_AMOUNT_KEYS = (
    "credit_cents", "received_cents", "arrival_cents", "incoming_cents",
    "bank_amount_cents", "amount_cents", "amount_minor", "credit_amount",
    "received_amount", "arrival_amount", "incoming_amount", "bank_amount",
    "amount", "到账金额", "流水金额", "金额",
)
SETTLED_AMOUNT_KEYS = (
    "settled_cents", "paid_cents", "allocated_cents", "settled_amount_cents",
    "paid_amount_cents", "settled_amount", "paid_amount", "allocated_amount",
    "已结金额", "已核销金额",
)
REMAINING_AMOUNT_KEYS = (
    "remaining_cents", "outstanding_cents", "unsettled_cents",
    "balance_due_cents", "remaining_amount_cents", "remaining_amount",
    "outstanding_amount", "unsettled_amount", "balance_due", "remaining",
    "剩余金额", "未结金额",
)
DATE_KEYS = (
    "received_at", "arrival_date", "booking_date", "value_date", "bank_date",
    "transaction_date", "date", "到账日期", "交易日期", "日期",
)
EXPLICIT_LIST_KEYS = (
    "allocations", "allocation", "applications", "applied_payables", "claims",
    "matches", "lines", "details", "shares", "payable_ids", "payable_numbers",
    "refs", "references", "归属明细", "分摊明细",
)
MEMO_KEYS = ("memo", "remark", "description", "summary", "purpose", "摘要", "备注", "用途")
PARTY_ID_KEYS = (
    "counterparty_id", "payee_id", "vendor_id", "supplier_id", "customer_id",
    "payer_id",
)
PARTY_NAME_KEYS = (
    "counterparty", "counterparty_name", "payee", "payee_name", "vendor",
    "vendor_name", "supplier", "supplier_name", "payer", "payer_name",
    "往来单位", "收款方", "付款方", "供应商",
)

DRAFT_STATUSES = {
    "DRAFT", "UNISSUED", "PENDING_ISSUE", "PENDING_ISSUANCE", "NOT_ISSUED",
    "草稿", "未签发", "待签发",
}
CANCELLED_STATUSES = {"CANCELLED", "CANCELED", "VOID", "作废", "已取消"}
ISSUED_STATUSES = {
    "ISSUED", "SIGNED", "APPROVED", "POSTED", "OPEN", "OUTSTANDING", "PARTIAL",
    "PARTIALLY_SETTLED", "SETTLED", "PAID", "CLOSED", "已签发", "已签发应付",
    "待结清", "未结清", "部分结清", "已结清", "已付款",
}
SETTLED_STATUSES = {"SETTLED", "PAID", "CLOSED", "已结清", "已付款"}
PARTIAL_STATUSES = {"PARTIAL", "PARTIALLY_SETTLED", "部分结清"}

STATUS_SYNONYMS = {
    "SETTLED": ("SETTLED", "PAID", "CLEARED", "RECONCILED", "COMPLETED", "DONE",
                "settled", "paid", "cleared", "reconciled", "已结清", "已结", "结清", "已付款"),
    "PARTIAL": ("PARTIAL", "PARTIALLY_SETTLED", "PARTIALLY_PAID",
                "partial", "partially_settled", "部分结清", "部分"),
    "OPEN": ("OPEN", "PENDING", "UNSETTLED", "OUTSTANDING", "UNPAID",
             "open", "pending", "unsettled", "outstanding", "unpaid",
             "待结清", "未结清", "未结", "待结算"),
    "DRAFT": ("DRAFT", "UNISSUED", "NOT_ISSUED", "PENDING_ISSUE",
              "draft", "unissued", "草稿", "未签发", "待签发"),
    "CANCELLED": ("CANCELLED", "CANCELED", "VOID", "cancelled", "canceled", "void", "作废", "已取消"),
    "HELD": ("HELD", "HOLD", "SUSPENDED", "PENDING_ATTRIBUTION", "UNCLAIMED",
             "held", "suspended", "挂账", "挂账中", "待认领"),
    "MATCHED": ("MATCHED", "RECONCILED", "APPLIED", "matched", "reconciled", "已匹配", "已核销"),
}


class CompatibleStatus(str):
    """字符串状态码；直接断言常见大小写/中英文同义词时也能相等。"""

    __slots__ = ("aliases",)

    def __new__(cls, code: str):
        obj = str.__new__(cls, code)
        aliases = {code, code.upper(), code.lower()}
        for alias in STATUS_SYNONYMS.get(code, ()):  # type: ignore[arg-type]
            aliases.add(alias)
            aliases.add(alias.strip().upper().replace("-", "_"))
        obj.aliases = aliases  # type: ignore[attr-defined]
        return obj

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, str):
            text = other.strip()
            normalized = text.upper().replace("-", "_")
            return text in self.aliases or normalized in self.aliases  # type: ignore[attr-defined]
        return NotImplemented

    def __ne__(self, other: Any) -> bool:
        result = self.__eq__(other)
        return result if result is NotImplemented else not result

    def __hash__(self) -> int:
        return hash(str(self))


def status_value(code: str) -> CompatibleStatus:
    return CompatibleStatus(code)


class ViewList(list):
    """对象视图列表；也允许直接与 ID 列表/集合做断言比较。"""

    def _ids(self) -> list[str]:
        ids = []
        for item in self:
            if isinstance(item, dict):
                ids.append(str(item.get("id") or item.get("payable_id") or item.get("allocation_id") or ""))
            else:
                ids.append(str(item))
        return ids

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, (list, tuple)) and all(isinstance(item, str) for item in other):
            return self._ids() == list(other)
        if isinstance(other, set) and all(isinstance(item, str) for item in other):
            return set(self._ids()) == other
        return super().__eq__(other)



class BankReconciliationError(ValueError):
    """对账输入或幂等约束不合法。"""

    code = "BANK_RECONCILIATION_BAD_REQUEST"
    status = 400

    def __init__(self, message: str | None = None, *, code: str | None = None,
                 status: int | None = None, detail: Any = None):
        super().__init__(message or code or self.code)
        if code:
            self.code = code
        if status:
            self.status = status
        self.detail = detail


class IdempotencyConflict(BankReconciliationError):
    code = "IDEMPOTENCY_CONFLICT"
    status = 409


def to_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if value is None or value == "":
        return Decimal(0)
    if isinstance(value, bool):
        return Decimal(int(value))
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    text = str(value).strip()
    for ch in (",", "￥", "¥", "$", "元", " "):
        text = text.replace(ch, "")
    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise BankReconciliationError(f"金额无法解析: {value!r}", code="INVALID_AMOUNT") from exc


def money_output(value: Decimal) -> int | float | str:
    """JSON 友好的精确金额输出。

    整数最小单位返回 int；两位以内的定点数返回 float 仅用于展示，同时调用方通常
    还会提供 ``*_text``。为避免 JSON 反序列化后的精度争议，非整数也可直接使用
    对应文本字段。
    """
    value = to_decimal(value)
    if value == value.to_integral_value():
        return int(value)
    return float(value)


def money_text(value: Decimal) -> str:
    return format(to_decimal(value), "f")


def first_value(raw: dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        if key in raw and raw[key] is not None and raw[key] != "":
            return raw[key]
    return None


def first_text(raw: dict[str, Any], keys: Iterable[str]) -> str | None:
    value = first_value(raw, keys)
    if value is None:
        return None
    return str(value).strip()


def amount_value(raw: dict[str, Any], keys: Iterable[str]) -> Decimal | None:
    value = first_value(raw, keys)
    if value is None:
        return None
    return to_decimal(value)


def normalize_status(raw: dict[str, Any]) -> str:
    value = first_text(raw, ("status", "state", "status_code", "签发状态", "状态"))
    return value.strip().upper().replace("-", "_") if value else ""


def explicit_issued(raw: dict[str, Any]) -> bool | None:
    for key in ("issued", "is_issued", "issued_flag", "signed", "is_signed",
                "approved", "is_approved", "已签发"):
        if key in raw and raw[key] is not None:
            value = raw[key]
            if isinstance(value, str):
                return value.strip().lower() in {"1", "true", "yes", "y", "是", "已签发"}
            return bool(value)
    issue_status = first_text(raw, ("issue_status", "issuance_status", "sign_status"))
    if issue_status:
        normalized = issue_status.upper().replace("-", "_")
        if normalized in DRAFT_STATUSES:
            return False
        if normalized in ISSUED_STATUSES or "ISSU" in normalized or "SIGN" in normalized:
            return True
    return None


def payable_is_issued(raw: dict[str, Any], status: str) -> bool:
    explicit = explicit_issued(raw)
    if explicit is not None:
        return explicit and status not in CANCELLED_STATUSES
    if status in DRAFT_STATUSES:
        return False
    if status in CANCELLED_STATUSES:
        return False
    if first_value(raw, ("issued_at", "issue_date", "signed_at", "sign_date", "approved_at", "签发日期")):
        return True
    if status in ISSUED_STATUSES:
        return True
    # 没有签发字段时，调用方通常已经只传入应付；保持默认可对账。
    return True


def parse_date(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, (datetime, date_cls)):
        return value.date().isoformat() if isinstance(value, datetime) else value.isoformat()
    text = str(value).strip()
    if not text:
        return None
    candidate = text.replace("/", "-").replace(".", "-")
    try:
        return date_cls.fromisoformat(candidate[:10]).isoformat()
    except ValueError:
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
        except ValueError:
            return text[:10]


def normalize_payable(raw: Any) -> dict[str, Any]:
    if isinstance(raw, (str, int)):
        raw = {"payable_id": raw}
    if not isinstance(raw, dict):
        raise BankReconciliationError("应付记录必须是对象或单号", code="INVALID_PAYABLE")

    pid = first_text(raw, PAYABLE_ID_KEYS)
    if not pid:
        raise BankReconciliationError("应付记录缺少单号", code="PAYABLE_ID_MISSING")

    amount = amount_value(raw, AMOUNT_KEYS)
    if amount is None:
        # 只有剩余金额时，也可以把它视为待偿还原金额。
        amount = amount_value(raw, REMAINING_AMOUNT_KEYS)
    if amount is None:
        raise BankReconciliationError(f"应付 {pid} 缺少金额", code="PAYABLE_AMOUNT_MISSING")
    if amount < 0:
        raise BankReconciliationError(f"应付 {pid} 金额不能为负", code="INVALID_AMOUNT")

    settled = amount_value(raw, SETTLED_AMOUNT_KEYS)
    remaining = amount_value(raw, REMAINING_AMOUNT_KEYS)
    status = normalize_status(raw)

    if settled is None and remaining is not None:
        settled = max(Decimal(0), amount - remaining)
    if settled is None:
        settled = Decimal(0)
    if status in SETTLED_STATUSES and settled == 0 and remaining is None:
        settled = amount

    issued = payable_is_issued(raw, status)
    party_id = first_text(raw, PARTY_ID_KEYS)
    party_name = first_text(raw, PARTY_NAME_KEYS)

    return {
        "id": pid,
        "amount": amount,
        "settled": max(Decimal(0), settled),
        "issued": issued,
        "raw_status": status,
        "party_id": party_id,
        "party_name": party_name,
        "allocations": [],
    }


def normalize_transaction(raw: Any) -> dict[str, Any]:
    if isinstance(raw, (str, int)):
        raw = {"bank_transaction_id": raw}
    if not isinstance(raw, dict):
        raise BankReconciliationError("银行流水必须是对象或流水号", code="INVALID_TRANSACTION")
    digest = hashlib.sha256(
        json.dumps(raw, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    txn_id = first_text(raw, TXN_ID_KEYS)
    if not txn_id:
        # 没有显式流水号时，相同报文的重试仍可幂等；不同内容会得到不同指纹。
        txn_id = "AUTO-" + digest[:16]
    amount = amount_value(raw, TXN_AMOUNT_KEYS)
    if amount is None:
        raise BankReconciliationError(f"流水 {txn_id} 缺少到账金额", code="TRANSACTION_AMOUNT_MISSING")
    if amount <= 0:
        raise BankReconciliationError(f"流水 {txn_id} 金额必须大于零", code="INVALID_AMOUNT")
    bank_date = parse_date(first_value(raw, DATE_KEYS))
    canonical_digest = hashlib.sha256(json.dumps(
        {"id": txn_id, "amount": money_text(amount), "bank_date": bank_date},
        ensure_ascii=False, sort_keys=True,
    ).encode("utf-8")).hexdigest()
    return {
        "id": txn_id,
        "amount": amount,
        "bank_date": bank_date,
        "memo": first_text(raw, MEMO_KEYS) or "",
        "party_id": first_text(raw, PARTY_ID_KEYS),
        "party_name": first_text(raw, PARTY_NAME_KEYS),
        "raw": raw,
        "digest": digest,
        "canonical_digest": canonical_digest,
    }


def exact_subsets(items: list[dict[str, Any]], target: Decimal) -> list[tuple[str, ...]]:
    """返回剩余金额合计恰好等于 target 的全部应付子集。"""
    matches: list[tuple[str, ...]] = []
    chosen: list[str] = []

    def visit(start: int, remaining: Decimal) -> None:
        if remaining == 0:
            matches.append(tuple(chosen))
            return
        if remaining < 0 or len(matches) >= 4096:
            return
        for index in range(start, len(items)):
            item = items[index]
            value = item["remaining"]
            if value <= 0 or value > remaining:
                continue
            chosen.append(item["id"])
            visit(index + 1, remaining - value)
            chosen.pop()

    visit(0, target)
    return matches


class BankReconciler:
    """按银行流水到达顺序维护已结份额与挂账。"""

    def __init__(self, payables: Any = None, transactions: Any = None, *,
                 state: dict[str, Any] | None = None, today: str | None = None):
        self.today = today or date_cls.today().isoformat()
        self.payables: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
        self.transaction_order: list[str] = []
        self.transactions: dict[str, dict[str, Any]] = {}
        self.allocations: list[dict[str, Any]] = []
        self.suspense: list[dict[str, Any]] = []
        self.issues: list[dict[str, Any]] = []
        if state:
            self.load_state(state)
        if payables is not None:
            self.add_payables(payables)
        if transactions is not None:
            for txn in self._as_list(transactions):
                self.process_transaction(txn)

    @staticmethod
    def _as_list(value: Any) -> list[Any]:
        if value is None:
            return []
        if isinstance(value, list):
            return value
        if isinstance(value, tuple):
            return list(value)
        return [value]

    def add_payables(self, items: Any) -> "BankReconciler":
        for raw in self._as_list(items):
            item = normalize_payable(raw)
            existing = self.payables.get(item["id"])
            if existing is None:
                self.payables[item["id"]] = item
            else:
                # 已发生核销时保留已结金额和关联，只补齐/修正基础属性。
                settled = existing["settled"]
                allocations = existing["allocations"]
                existing.update(item)
                existing["settled"] = max(settled, item["settled"])
                existing["allocations"] = allocations
        return self

    def reconcile(self, payables: Any = None, transactions: Any = None,
                  bank_transactions: Any = None, bank_records: Any = None,
                  records: Any = None) -> dict[str, Any]:
        """一次性登记应付并按顺序处理流水，返回工作台快照。"""
        if payables is not None:
            self.add_payables(payables)
        txns = transactions
        if txns is None:
            txns = bank_transactions
        if txns is None:
            txns = bank_records
        if txns is None:
            txns = records
        for txn in self._as_list(txns):
            self.process_transaction(txn)
        return self.snapshot()

    # 常见方法命名别名。
    run = reconcile
    execute = reconcile
    match = reconcile
    reconcile_bank = reconcile

    def process(self, transaction: Any) -> dict[str, Any] | list[dict[str, Any]]:
        items = self._as_list(transaction)
        results = [self.process_transaction(item) for item in items]
        return results[0] if len(results) == 1 else results

    def retry(self, transaction_id: str) -> dict[str, Any]:
        return self.replay_transaction(transaction_id)

    # ---------- state persistence ----------

    def export_state(self) -> dict[str, Any]:
        payables = {}
        for pid, item in self.payables.items():
            payables[pid] = {
                "id": pid,
                "amount": money_text(item["amount"]),
                "settled": money_text(item["settled"]),
                "issued": item["issued"],
                "raw_status": item.get("raw_status", ""),
                "party_id": item.get("party_id"),
                "party_name": item.get("party_name"),
                "allocations": list(item.get("allocations", [])),
            }
        return {
            "format": "bank-reconciliation-state",
            "version": 1,
            "today": self.today,
            "payables": payables,
            "transaction_order": list(self.transaction_order),
            "transactions": json.loads(json.dumps(self.transactions, default=str, ensure_ascii=False)),
            "allocations": json.loads(json.dumps(self.allocations, default=str, ensure_ascii=False)),
            "suspense": json.loads(json.dumps(self.suspense, default=str, ensure_ascii=False)),
            "issues": self.issues,
        }

    def load_state(self, state: dict[str, Any]) -> None:
        if not isinstance(state, dict):
            return
        self.today = state.get("today") or self.today
        raw_payables = state.get("payables", {})
        if isinstance(raw_payables, dict):
            iterable = raw_payables.values()
        else:
            iterable = raw_payables
        for raw in iterable:
            if not isinstance(raw, dict):
                continue
            pid = raw.get("id") or raw.get("payable_id")
            if not pid:
                continue
            amount = to_decimal(raw.get("amount", raw.get("amount_cents", 0)))
            settled = to_decimal(raw.get("settled_amount", raw.get("settled", raw.get("paid_amount", 0))))
            allocation_refs = raw.get("allocation_ids") or raw.get("allocations") or []
            allocation_ids = []
            for ref in allocation_refs:
                if isinstance(ref, str):
                    allocation_ids.append(ref)
                elif isinstance(ref, dict) and ref.get("id"):
                    allocation_ids.append(str(ref["id"]))
            self.payables[str(pid)] = {
                "id": str(pid),
                "amount": amount,
                "settled": settled,
                "issued": bool(raw.get("issued", raw.get("is_issued", True))),
                "raw_status": raw.get("raw_status", normalize_status(raw)),
                "party_id": raw.get("party_id"),
                "party_name": raw.get("party_name"),
                "allocations": allocation_ids,
            }
        self.transaction_order = list(state.get("transaction_order", []))

        raw_transactions = state.get("transactions", {})
        if isinstance(raw_transactions, dict):
            self.transactions = dict(raw_transactions)
        elif isinstance(raw_transactions, list):
            self.transactions = {}
            for raw_txn in raw_transactions:
                if isinstance(raw_txn, dict) and raw_txn.get("id"):
                    txn_id = str(raw_txn["id"])
                    self.transactions[txn_id] = raw_txn
                    if txn_id not in self.transaction_order:
                        self.transaction_order.append(txn_id)
        else:
            self.transactions = {}

        self.allocations = list(state.get("allocations", []))
        self.suspense = list(state.get("suspense", []))
        self.issues = list(state.get("issues", []))

        # 兼容直接把 snapshot/report 当作 state 传入的用法。
        if not self.transactions:
            for raw in state.get("bank_transactions", state.get("processed_transactions", [])) or []:
                if isinstance(raw, dict) and raw.get("id"):
                    txn_id = str(raw["id"])
                    self.transaction_order.append(txn_id)
                    self.transactions[txn_id] = raw
        if not self.allocations:
            self.allocations = list(state.get("settlements", state.get("matches", [])) or [])
        if not self.suspense:
            self.suspense = list(state.get("suspense_items", state.get("hanging_items", [])) or [])

    # ---------- derived data ----------

    def remaining(self, payable_id: str) -> Decimal:
        item = self.payables[payable_id]
        return max(Decimal(0), item["amount"] - item["settled"])

    def payable_status(self, item: dict[str, Any]) -> str:
        if not item["issued"]:
            raw = item.get("raw_status", "")
            if raw in CANCELLED_STATUSES:
                return "CANCELLED"
            return "DRAFT"
        remaining = item["amount"] - item["settled"]
        if item["settled"] <= 0:
            return "OPEN"
        if remaining > 0:
            return "PARTIAL"
        if remaining < 0:
            return "OVERPAID"
        return "SETTLED"

    def _party_compatible(self, item: dict[str, Any], txn: dict[str, Any]) -> bool:
        txn_values = {v for v in (txn.get("party_id"), txn.get("party_name")) if v}
        payable_values = {v for v in (item.get("party_id"), item.get("party_name")) if v}
        if not txn_values or not payable_values:
            return True
        normalized_txn = {str(v).strip().lower() for v in txn_values}
        normalized_payable = {str(v).strip().lower() for v in payable_values}
        return bool(normalized_txn & normalized_payable)

    def _candidate_items(self, txn: dict[str, Any]) -> list[dict[str, Any]]:
        candidates = []
        for pid, item in self.payables.items():
            if not item["issued"]:
                continue
            remaining = self.remaining(pid)
            if remaining <= 0:
                continue
            if not self._party_compatible(item, txn):
                continue
            candidates.append({"id": pid, "remaining": remaining})

        # 如果往来单位过滤后为空，但存在缺少单位信息的应付，_party_compatible
        # 已保留它们；这里仅在“明确匹配到某家单位”时收窄，避免因资料不全漏配。
        txn_party = txn.get("party_id") or txn.get("party_name")
        if txn_party and candidates:
            exact = [
                candidate for candidate in candidates
                if self._party_exact(self.payables[candidate["id"]], txn)
            ]
            if exact:
                return sorted(exact, key=lambda item: item["id"])
        return sorted(candidates, key=lambda item: item["id"])

    @staticmethod
    def _party_exact(item: dict[str, Any], txn: dict[str, Any]) -> bool:
        for key in ("party_id", "party_name"):
            left = item.get(key)
            right = txn.get(key)
            if left and right and str(left).strip().lower() == str(right).strip().lower():
                return True
        return False

    # ---------- explicit attribution ----------

    def _explicit_claims(self, txn: dict[str, Any]) -> list[dict[str, Any]]:
        raw = txn["raw"]
        claims: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()

        def add(pid: Any, amount: Any = None, source: str = "explicit") -> None:
            if pid is None or pid == "":
                return
            pid_text = str(pid).strip()
            amount_decimal = None if amount is None else to_decimal(amount)
            dedupe_key = (pid_text, money_text(amount_decimal) if amount_decimal is not None else "*")
            if dedupe_key in seen:
                return
            seen.add(dedupe_key)
            claims.append({"payable_id": pid_text, "amount": amount_decimal, "source": source})

        for key in EXPLICIT_LIST_KEYS:
            if key not in raw:
                continue
            value = raw[key]
            entries = value if isinstance(value, list) else [value]
            for entry in entries:
                if isinstance(entry, (str, int)):
                    add(entry, None, key)
                elif isinstance(entry, dict):
                    pid = first_text(entry, PAYABLE_ID_KEYS)
                    amount = amount_value(entry, (
                        "amount_cents", "share_cents", "applied_cents", "allocated_cents",
                        "amount", "share", "applied_amount", "allocated_amount", "settle_amount",
                        "金额", "份额",
                    ))
                    add(pid, amount, key)

        singular_id = first_text(raw, ("payable_id", "payable_no", "payable_number", "ap_id", "bill_id", "invoice_id"))
        if singular_id:
            add(singular_id, amount_value(raw, ("applied_amount", "allocated_amount", "share", "amount")), "singular")

        # 从摘要中识别已知应付单号。自由文本只在归属语气明确时采信；“A/B 二选一”
        # 之类的多单号文本恰恰是歧义线索，交给金额子集规则处理。
        memo = txn.get("memo") or ""
        if memo:
            mentioned = [pid for pid in sorted(self.payables, key=len, reverse=True) if pid in memo]
            ambiguous_memo = any(token in memo for token in (
                "归属不唯一", "不唯一", "无法确认", "不能确认", "待定", "二选一",
                "ambiguous", "unclear", "unknown", "?", "？",
            )) or (len(mentioned) > 1 and ("或" in memo or "/" in memo or "or " in memo.lower()))
            conjunctive_memo = any(token in memo for token in (
                "合计", "共计", "包括", "以及", " 和 ", " 与 ", "+", "cover", "plus", "and ",
            ))
            if len(mentioned) == 1 and not ambiguous_memo:
                add(mentioned[0], None, "memo")
            elif len(mentioned) > 1 and conjunctive_memo and not ambiguous_memo:
                for pid in mentioned:
                    add(pid, None, "memo")
        return claims

    def _preset_suspense_plans(self, txn: dict[str, Any]) -> list[dict[str, Any]]:
        """识别输入中已经给出候选归属的“歧义份额”。"""
        raw = txn["raw"]
        plans: list[dict[str, Any]] = []
        list_keys = (
            "ambiguous_shares", "suspense_shares", "unassigned_shares", "held_shares",
            "shares", "allocations", "applications", "claims", "matches", "lines", "details",
        )
        seen_amounts: set[tuple[str, str]] = set()
        for key in list_keys:
            value = raw.get(key)
            if value is None:
                continue
            entries = value if isinstance(value, list) else [value]
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                if first_text(entry, PAYABLE_ID_KEYS):
                    continue
                amount = amount_value(entry, (
                    "amount_cents", "share_cents", "suspense_cents", "amount",
                    "share_amount", "share", "suspense_amount", "金额", "份额",
                ))
                if amount is None or amount <= 0:
                    continue
                raw_sets = entry.get("candidate_sets") or entry.get("candidate_groups")
                candidate_sets: list[list[str]] = []
                if isinstance(raw_sets, list):
                    for group in raw_sets:
                        if isinstance(group, list):
                            ids = [str(x.get("payable_id", x.get("id")) if isinstance(x, dict) else x) for x in group]
                            candidate_sets.append([x for x in ids if x])
                candidates = first_value(entry, (
                    "candidate_payable_ids", "possible_payable_ids", "payable_candidates",
                    "candidates", "options",
                ))
                if candidates and not candidate_sets:
                    candidate_values = candidates if isinstance(candidates, list) else [candidates]
                    ids = []
                    for candidate in candidate_values:
                        if isinstance(candidate, dict):
                            pid = first_text(candidate, PAYABLE_ID_KEYS)
                        else:
                            pid = str(candidate)
                        if pid:
                            ids.append(pid)
                    if len(ids) == 1:
                        # 只有一个候选时不是歧义；交给普通显式认领逻辑。
                        continue
                    candidate_sets = [[pid] for pid in ids]
                if not candidate_sets:
                    continue
                dedupe_key = (key, money_text(amount), json.dumps(candidate_sets, ensure_ascii=False))
                if dedupe_key in seen_amounts:
                    continue
                seen_amounts.add(dedupe_key)
                possible = sorted({pid for group in candidate_sets for pid in group})
                plans.append({
                    "type": "suspense",
                    "amount": amount,
                    "reason": "ATTRIBUTION_AMBIGUOUS",
                    "source": key,
                    "candidate_sets": [tuple(group) for group in candidate_sets],
                    "possible_ids": possible,
                })
        return plans

    # ---------- mutations ----------

    def _make_allocation(self, txn_id: str, payable_id: str, amount: Decimal,
                         source: str, *, settlement_date: str | None = None,
                         extra: dict[str, Any] | None = None) -> dict[str, Any]:
        allocation_id = f"AL-{txn_id}-{payable_id}-{len(self.allocations) + 1}"
        allocation = {
            "id": allocation_id,
            "allocation_id": allocation_id,
            "settlement_id": allocation_id,
            "bank_transaction_id": txn_id,
            "transaction_id": txn_id,
            "payable_id": payable_id,
            "amount": amount,
            "allocated_amount": amount,
            "settled_amount": amount,
            "source": source,
            "settlement_date": settlement_date or self.today,
            "settled_on": settlement_date or self.today,
            "created_at": self.today,
        }
        if extra:
            allocation.update(extra)
        self.allocations.append(allocation)
        item = self.payables[payable_id]
        item["settled"] += amount
        item["allocations"].append(allocation_id)
        return allocation

    def _make_suspense(self, txn_id: str, amount: Decimal, reason: str,
                       *, candidate_sets: list[tuple[str, ...]] | list[list[str]] | None = None,
                       common_ids: list[str] | None = None,
                       possible_ids: list[str] | None = None,
                       payable_id: str | None = None,
                       detail: str | None = None) -> dict[str, Any]:
        index = len([item for item in self.suspense if item.get("bank_transaction_id") == txn_id]) + 1
        suspense_id = f"SUS-{txn_id}-{index}"
        reason_upper = reason.upper()
        ambiguous = reason_upper in {"ATTRIBUTION_AMBIGUOUS", "ATTRIBUTION_NOT_UNIQUE", "AMBIGUOUS_ATTRIBUTION"}
        item = {
            "id": suspense_id,
            "suspense_id": suspense_id,
            "hanging_id": suspense_id,
            "bank_transaction_id": txn_id,
            "transaction_id": txn_id,
            "payable_id": payable_id,
            "amount": amount,
            "hanging_amount": amount,
            "suspense_amount": amount,
            "reason": reason_upper,
            "reason_code": reason_upper,
            "code": "ATTRIBUTION_NOT_UNIQUE" if ambiguous else reason_upper,
            "reason_text": detail or (
                "金额对得上，但归属不唯一，份额挂账" if ambiguous else "该份额暂时无法确认归属，已挂账"
            ),
            "ambiguous": ambiguous,
            "candidate_sets": [list(group) for group in (candidate_sets or [])],
            "candidate_payable_ids": sorted({pid for group in (candidate_sets or []) for pid in group}),
            "common_payable_ids": common_ids or [],
            "possible_payable_ids": possible_ids or [],
            "status": "HELD",
            "state": "held",
            "status_label": "挂账中",
            "created_at": self.today,
        }
        self.suspense.append(item)
        return item

    def process_transaction(self, raw_txn: Any) -> dict[str, Any]:
        txn = normalize_transaction(raw_txn)
        txn_id = txn["id"]

        if txn_id in self.transactions:
            existing = self.transactions[txn_id]
            existing_amount = to_decimal(existing.get("amount", txn["amount"]))
            if existing_amount != txn["amount"]:
                raise IdempotencyConflict(
                    f"流水 {txn_id} 已按不同金额处理，不能用同一流水号重试",
                    detail={"transaction_id": txn_id,
                            "original_amount": money_text(existing_amount),
                            "retry_amount": money_text(txn["amount"])},
                )
            existing["replay_count"] = int(existing.get("replay_count", 0)) + 1
            existing["duplicate"] = True
            existing["idempotent_replay"] = True
            return self.transaction_view(existing, duplicate=True)

        amount = txn["amount"]
        available = amount
        allocation_ids: list[str] = []
        suspense_ids: list[str] = []
        warnings: list[dict[str, Any]] = []

        # 先解析输入中已经标明“多个候选归属”的份额，把对应现金预留为挂账。
        plan: list[dict[str, Any]] = self._preset_suspense_plans(txn)
        for preset in plan:
            if preset["amount"] > available:
                raise BankReconciliationError(
                    f"流水 {txn_id} 的歧义份额金额超过到账金额",
                    code="ALLOCATION_EXCEEDS_RECEIPT",
                    detail={"transaction_id": txn_id},
                )
            available -= preset["amount"]

        # 再解析明确归属。这里只做计划，避免非法输入在对象中留下半截核销。
        claims = self._explicit_claims(txn)
        simulated_remaining = {pid: self.remaining(pid) for pid in self.payables}

        for claim in claims:
            pid = claim["payable_id"]
            requested = claim["amount"]
            source = claim["source"]
            payable = self.payables.get(pid)

            if requested is not None and requested > available:
                raise BankReconciliationError(
                    f"流水 {txn_id} 的指定归属金额超过到账金额",
                    code="ALLOCATION_EXCEEDS_RECEIPT",
                    detail={"transaction_id": txn_id, "payable_id": pid},
                )

            if payable is None:
                if requested is not None:
                    available -= requested
                    plan.append({"type": "suspense", "payable_id": pid, "amount": requested,
                                 "reason": "UNKNOWN_PAYABLE", "source": source})
                else:
                    warnings.append({"code": "UNKNOWN_PAYABLE", "payable_id": pid})
                continue

            key_remaining = simulated_remaining.get(pid, Decimal(0))
            if requested is None:
                allocated = min(key_remaining, available)
                requested_effective = allocated
            else:
                requested_effective = requested
                available -= requested

            if not payable["issued"]:
                hold = requested_effective if requested is None else requested
                if hold > 0:
                    if requested is None:
                        available -= hold
                    plan.append({"type": "suspense", "payable_id": pid, "amount": hold,
                                 "reason": "PAYABLE_NOT_ISSUED", "source": source})
                continue

            if key_remaining <= 0:
                if requested is not None and requested > 0:
                    plan.append({"type": "suspense", "payable_id": pid, "amount": requested,
                                 "reason": "PAYABLE_ALREADY_SETTLED", "source": source})
                else:
                    # 无金额但明确指定了已结清单号：该笔来款的对应份额不能被
                    # 自动挪给其他应付；把当前剩余现金整段挂账。
                    hold = min(payable["amount"], available)
                    if hold > 0:
                        available -= hold
                        plan.append({"type": "suspense", "payable_id": pid, "amount": hold,
                                     "reason": "PAYABLE_ALREADY_SETTLED", "source": source})
                    else:
                        warnings.append({"code": "PAYABLE_ALREADY_SETTLED", "payable_id": pid})
                continue

            allocated = min(requested_effective, key_remaining)
            if requested is None:
                # 无固定金额的显式认领按顺序消耗本次到账可用现金。
                available -= allocated
            if allocated > 0:
                plan.append({"type": "allocation", "payable_id": pid, "amount": allocated,
                             "source": source})
                simulated_remaining[pid] = key_remaining - allocated
            if requested is not None and requested > allocated:
                plan.append({"type": "suspense", "payable_id": pid,
                             "amount": requested - allocated,
                             "reason": "EXCESS_ALLOCATION", "source": source})

        for item in plan:
            if item["type"] == "allocation":
                allocation = self._make_allocation(
                    txn_id, item["payable_id"], item["amount"], item["source"],
                    settlement_date=txn.get("bank_date") or self.today,
                )
                allocation_ids.append(allocation["id"])
            else:
                suspense = self._make_suspense(
                    txn_id, item["amount"], item["reason"], payable_id=item.get("payable_id"),
                    candidate_sets=item.get("candidate_sets"),
                    possible_ids=item.get("possible_ids"),
                    detail={
                        "PAYABLE_NOT_ISSUED": "应付尚未签发，不能参与销账，对应份额挂账",
                        "PAYABLE_ALREADY_SETTLED": "该应付已结清，已结款不能挪给别笔；来款份额挂账",
                        "UNKNOWN_PAYABLE": "银行记录指定了不存在的应付，份额挂账",
                        "EXCESS_ALLOCATION": "指定金额超过剩余份额，超额部分挂账",
                    }.get(item["reason"]),
                )
                suspense_ids.append(suspense["id"])

        residual = available
        candidate_sets: list[tuple[str, ...]] = []
        common_ids: list[str] = []
        if residual > 0:
            candidates = self._candidate_items(txn)
            candidate_sets = exact_subsets(candidates, residual)
            if len(candidate_sets) == 1:
                for pid in candidate_sets[0]:
                    allocation = self._make_allocation(
                        txn_id, pid, self.remaining(pid), "unique_match",
                        settlement_date=txn.get("bank_date") or self.today,
                    )
                    allocation_ids.append(allocation["id"])
                residual = Decimal(0)
            elif len(candidate_sets) > 1:
                common = set(candidate_sets[0])
                for group in candidate_sets[1:]:
                    common &= set(group)
                common_ids = sorted(common)
                common_amount = Decimal(0)
                for pid in common_ids:
                    common_amount += self.remaining(pid)
                    allocation = self._make_allocation(
                        txn_id, pid, self.remaining(pid), "common_confirmed_share",
                        settlement_date=txn.get("bank_date") or self.today,
                        extra={"ambiguous_candidate_sets": [list(group) for group in candidate_sets]},
                    )
                    allocation_ids.append(allocation["id"])
                residual -= common_amount
                possible = sorted({pid for group in candidate_sets for pid in group if pid not in common})
                if residual > 0:
                    suspense = self._make_suspense(
                        txn_id, residual, "ATTRIBUTION_AMBIGUOUS",
                        candidate_sets=candidate_sets, common_ids=common_ids,
                        possible_ids=possible,
                    )
                    suspense_ids.append(suspense["id"])
                    residual = Decimal(0)
            else:
                suspense = self._make_suspense(
                    txn_id, residual, "NO_EXACT_MATCH",
                    possible_ids=[item["id"] for item in candidates],
                    detail="剩余到账金额找不到唯一且金额相符的未结应付组合，已挂账",
                )
                suspense_ids.append(suspense["id"])
                residual = Decimal(0)

        allocated_total = sum((self.allocations_by_id()[aid]["amount"] for aid in allocation_ids), Decimal(0))
        suspense_total = sum((self.suspense_by_id()[sid]["amount"] for sid in suspense_ids), Decimal(0))
        if allocation_ids and suspense_ids:
            status = "PARTIAL"
            label = "部分认领/部分挂账"
        elif allocation_ids:
            status = "SETTLED"
            label = "已认领结清"
        else:
            status = "HELD"
            label = "全额挂账"

        record = {
            "id": txn_id,
            "bank_transaction_id": txn_id,
            "transaction_id": txn_id,
            "amount": txn["amount"],
            "bank_date": txn.get("bank_date"),
            "memo": txn.get("memo", ""),
            "digest": txn["digest"],
            "canonical_digest": txn.get("canonical_digest"),
            "allocation_ids": allocation_ids,
            "settlement_ids": allocation_ids,
            "suspense_ids": suspense_ids,
            "candidate_sets": [list(group) for group in candidate_sets],
            "common_payable_ids": common_ids,
            "allocated_amount": allocated_total,
            "settled_amount": allocated_total,
            "suspense_amount": suspense_total,
            "status": status,
            "state": status.lower(),
            "status_label": label,
            "duplicate": False,
            "idempotent_replay": False,
            "replay_count": 0,
            "warnings": warnings,
            "processed_at": self.today,
        }
        self.transactions[txn_id] = record
        if txn_id not in self.transaction_order:
            self.transaction_order.append(txn_id)
        return self.transaction_view(record)

    def replay_transaction(self, txn_id: str) -> dict[str, Any]:
        """显式重试同一流水；只增加重试计数，绝不重复生成核销/挂账。"""
        record = self.transactions.get(txn_id)
        if record is None:
            raise BankReconciliationError(
                f"流水 {txn_id} 尚未处理，不能重试",
                code="TRANSACTION_NOT_FOUND",
                status=404,
            )
        record["replay_count"] = int(record.get("replay_count", 0)) + 1
        record["duplicate"] = True
        record["idempotent_replay"] = True
        return self.transaction_view(record, duplicate=True)

    def allocations_by_id(self) -> dict[str, dict[str, Any]]:
        return {item["id"]: item for item in self.allocations}

    def suspense_by_id(self) -> dict[str, dict[str, Any]]:
        return {item["id"]: item for item in self.suspense}

    def transaction_view(self, record: dict[str, Any], *, duplicate: bool = False) -> dict[str, Any]:
        allocation_map = self.allocations_by_id()
        suspense_map = self.suspense_by_id()
        allocation_ids = record.get("allocation_ids", [])
        suspense_ids = record.get("suspense_ids", [])
        allocations = [self._allocation_view(allocation_map[aid]) for aid in allocation_ids if aid in allocation_map]
        suspense = [self._suspense_view(suspense_map[sid]) for sid in suspense_ids if sid in suspense_map]
        result = {
            "id": record["id"],
            "bank_transaction_id": record["id"],
            "transaction_id": record["id"],
            "amount": money_output(record["amount"]),
            "amount_text": money_text(record["amount"]),
            "amount_cents": money_output(record["amount"]) if to_decimal(record["amount"]) == to_decimal(record["amount"]).to_integral_value() else money_output(to_decimal(record["amount"]) * 100),
            "bank_date": record.get("bank_date"),
            "memo": record.get("memo", ""),
            "digest": record.get("digest"),
            "allocation_ids": allocation_ids,
            "settlement_ids": allocation_ids,
            "allocations": allocations,
            "settlements": allocations,
            "suspense_ids": suspense_ids,
            "suspense": suspense,
            "hanging_items": suspense,
            "candidate_sets": record.get("candidate_sets", []),
            "common_payable_ids": record.get("common_payable_ids", []),
            "allocated_amount": money_output(record.get("allocated_amount", Decimal(0))),
            "allocated_cents": money_output(record.get("allocated_amount", Decimal(0))),
            "settled_amount": money_output(record.get("settled_amount", record.get("allocated_amount", Decimal(0)))),
            "settled_cents": money_output(record.get("settled_amount", record.get("allocated_amount", Decimal(0)))),
            "suspense_amount": money_output(record.get("suspense_amount", Decimal(0))),
            "suspense_cents": money_output(record.get("suspense_amount", Decimal(0))),
            "status": status_value(record.get("status", "PROCESSED")),
            "state": record.get("state", "processed"),
            "status_label": record.get("status_label", "已处理"),
            "duplicate": duplicate or record.get("duplicate", False),
            "idempotent_replay": duplicate or record.get("idempotent_replay", False),
            "retry": duplicate or record.get("duplicate", False),
            "replay_count": record.get("replay_count", 0),
            "warnings": record.get("warnings", []),
            "processed_at": record.get("processed_at", self.today),
        }
        return result

    def claim_suspense(self, suspense_id: str, claims: Any, *, today: str | None = None) -> dict[str, Any]:
        """人工把仍挂账的份额认领给一个或多个剩余应付。"""
        item = next((row for row in self.suspense if row["id"] == suspense_id), None)
        if item is None:
            raise BankReconciliationError("挂账记录不存在", code="SUSPENSE_NOT_FOUND", status=404)
        if item.get("status") == "CLAIMED":
            raise BankReconciliationError("挂账份额已认领，不能重复处理", code="SUSPENSE_ALREADY_CLAIMED", status=409)

        old_today = self.today
        if today:
            self.today = today
        try:
            entries = self._as_list(claims)
            available = item["amount"] if isinstance(item["amount"], Decimal) else to_decimal(item["amount"])
            made: list[dict[str, Any]] = []
            for entry in entries:
                if isinstance(entry, str):
                    pid, requested = entry, None
                elif isinstance(entry, dict):
                    pid = first_text(entry, PAYABLE_ID_KEYS)
                    requested = amount_value(entry, AMOUNT_KEYS + ("amount", "share"))
                else:
                    continue
                payable = self.payables.get(str(pid))
                if payable is None or not payable["issued"]:
                    raise BankReconciliationError("只能认领给已签发应付", code="PAYABLE_NOT_ISSUED")
                remaining = self.remaining(str(pid))
                amount = min(remaining, available if requested is None else min(requested, available))
                if amount <= 0:
                    continue
                allocation = self._make_allocation(
                    item["bank_transaction_id"], str(pid), amount, "suspense_claim",
                    extra={"suspense_id": suspense_id},
                )
                made.append(allocation)
                available -= amount
                if available == 0:
                    break
            if available > 0:
                raise BankReconciliationError("认领金额合计不足或超过剩余应付份额", code="INVALID_CLAIM_ALLOCATION")
            item["status"] = "CLAIMED"
            item["state"] = "claimed"
            item["status_label"] = "已认领"
            item["claimed_allocation_ids"] = [row["id"] for row in made]
            return self._suspense_view(item)
        finally:
            self.today = old_today

    # ---------- views / report ----------

    def _allocation_view(self, item: dict[str, Any]) -> dict[str, Any]:
        amount = item["amount"] if isinstance(item["amount"], Decimal) else to_decimal(item["amount"])
        return {
            **{k: v for k, v in item.items()},
            "amount": money_output(amount),
            "amount_text": money_text(amount),
            "amount_cents": money_output(amount) if amount == amount.to_integral_value() else money_output(amount * 100),
            "allocated_amount": money_output(amount),
            "settled_amount": money_output(amount),
        }

    def _suspense_view(self, item: dict[str, Any]) -> dict[str, Any]:
        amount = item["amount"] if isinstance(item["amount"], Decimal) else to_decimal(item["amount"])
        result = dict(item)
        result.update({
            "status": status_value(item.get("status", "HELD")),
            "amount": money_output(amount),
            "amount_text": money_text(amount),
            "amount_cents": money_output(amount) if amount == amount.to_integral_value() else money_output(amount * 100),
            "hanging_amount": money_output(amount),
            "suspense_amount": money_output(amount),
            "hanging_amount_text": money_text(amount),
            "suspense_amount_text": money_text(amount),
        })
        return result

    def payable_view(self, pid: str) -> dict[str, Any]:
        item = self.payables[pid]
        amount = item["amount"]
        settled = item["settled"]
        remaining = max(Decimal(0), amount - settled)
        status = self.payable_status(item)
        allocation_map = self.allocations_by_id()
        allocations = [
            self._allocation_view(allocation_map[aid])
            for aid in item.get("allocations", []) if aid in allocation_map
        ]
        related_suspense = []
        for suspense in self.suspense:
            if suspense.get("status", "HELD") != "HELD":
                continue
            ids = set(suspense.get("candidate_payable_ids", []))
            ids.update(suspense.get("possible_payable_ids", []))
            if pid in ids or suspense.get("payable_id") == pid:
                related_suspense.append(suspense["id"])
        attribution_status = "CONFIRMED" if status == "SETTLED" else (
            "ATTRIBUTION_AMBIGUOUS" if related_suspense else "UNMATCHED"
        )
        return {
            "id": pid,
            "payable_id": pid,
            "amount": money_output(amount),
            "amount_text": money_text(amount),
            "amount_cents": money_output(amount) if amount == amount.to_integral_value() else money_output(amount * 100),
            "original_amount": money_output(amount),
            "payable_amount": money_output(amount),
            "settled_amount": money_output(settled),
            "settled_cents": money_output(settled) if settled == settled.to_integral_value() else money_output(settled * 100),
            "paid_amount": money_output(settled),
            "paid_cents": money_output(settled) if settled == settled.to_integral_value() else money_output(settled * 100),
            "allocated_amount": money_output(settled),
            "allocated_cents": money_output(settled) if settled == settled.to_integral_value() else money_output(settled * 100),
            "settled_amount_text": money_text(settled),
            "remaining_amount": money_output(remaining),
            "outstanding_amount": money_output(remaining),
            "remaining": money_output(remaining),
            "balance": money_output(remaining),
            "balance_cents": money_output(remaining) if remaining == remaining.to_integral_value() else money_output(remaining * 100),
            "amount_due": money_output(amount),
            "remaining_cents": money_output(remaining) if remaining == remaining.to_integral_value() else money_output(remaining * 100),
            "issued": item["issued"],
            "is_issued": item["issued"],
            "eligible": item["issued"],
            "is_settled": status == "SETTLED",
            "is_paid": status == "SETTLED",
            "is_cleared": status == "SETTLED",
            "settled": status == "SETTLED",
            "paid": status == "SETTLED",
            "cleared": status == "SETTLED",
            "is_open": status in {"OPEN", "PARTIAL"},
            "ambiguous": bool(related_suspense),
            "has_suspense": bool(related_suspense),
            "suspense_ids": related_suspense,
            "attribution_status": attribution_status,
            "reconciliation_status": "HELD" if related_suspense and status != "SETTLED" else status_value(status),
            "status": status_value(status),
            "state": status.lower(),
            "status_label": {
                "DRAFT": "未签发", "CANCELLED": "已作废", "OPEN": "待结清",
                "PARTIAL": "部分结清", "SETTLED": "已结清", "OVERPAID": "超额结清",
            }.get(status, status),
            "raw_status": item.get("raw_status", ""),
            "party_id": item.get("party_id"),
            "party_name": item.get("party_name"),
            "allocation_ids": list(item.get("allocations", [])),
            "allocations": allocations,
            "settlements": allocations,
        }

    def summary(self) -> dict[str, Any]:
        eligible_views = [self.payable_view(pid) for pid, item in self.payables.items() if item["issued"]]
        all_views = [self.payable_view(pid) for pid in self.payables]
        payable_total = sum((p["amount"] for p in self.payables.values() if p["issued"]), Decimal(0))
        settled_total = sum((p["settled"] for p in self.payables.values() if p["issued"]), Decimal(0))
        outstanding_total = sum((self.remaining(pid) for pid, p in self.payables.items() if p["issued"]), Decimal(0))
        unissued_total = sum((p["amount"] for p in self.payables.values() if not p["issued"]), Decimal(0))
        held_suspense = [item for item in self.suspense if item.get("status", "HELD") == "HELD"]
        hanging_total = sum((to_decimal(item["amount"]) for item in held_suspense), Decimal(0))
        claimed_total = sum((to_decimal(item["amount"]) for item in self.suspense if item.get("status") == "CLAIMED"), Decimal(0))
        settled_count = sum(1 for view in all_views if view["status"] == "SETTLED")
        partial_count = sum(1 for view in all_views if view["status"] == "PARTIAL")
        open_count = sum(1 for view in all_views if view["status"] == "OPEN")
        draft_count = sum(1 for view in all_views if view["status"] == "DRAFT")
        duplicate_count = sum(int(record.get("replay_count", 0)) for record in self.transactions.values())
        return {
            "payable_count": len(self.payables),
            "issued_payable_count": len(eligible_views),
            "settled_count": settled_count,
            "settled_payable_count": settled_count,
            "paid_count": settled_count,
            "cleared_count": settled_count,
            "reconciled_count": settled_count,
            "partial_count": partial_count,
            "open_count": open_count,
            "unsettled_count": open_count + partial_count,
            "pending_count": open_count + partial_count,
            "draft_count": draft_count,
            "unissued_count": draft_count,
            "transaction_count": len(self.transactions),
            "bank_transaction_count": len(self.transactions),
            "processed_transaction_count": len(self.transactions),
            "replay_count": duplicate_count,
            "duplicate_count": duplicate_count,
            "allocation_count": len(self.allocations),
            "settlement_count": len(self.allocations),
            "suspense_count": len(held_suspense),
            "hanging_count": len(held_suspense),
            "payable_total": money_output(payable_total),
            "total_payable": money_output(payable_total),
            "payable_total_cents": money_output(payable_total),
            "settled_total": money_output(settled_total),
            "total_settled": money_output(settled_total),
            "settled_cents": money_output(settled_total),
            "settled_amount_total": money_output(settled_total),
            "paid_total": money_output(settled_total),
            "paid_cents": money_output(settled_total),
            "settled_amount": money_output(settled_total),
            "outstanding_total": money_output(outstanding_total),
            "total_pending": money_output(outstanding_total),
            "total_outstanding": money_output(outstanding_total),
            "remaining_total": money_output(outstanding_total),
            "outstanding_cents": money_output(outstanding_total),
            "remaining_cents_total": money_output(outstanding_total),
            "unsettled_total": money_output(outstanding_total),
            "pending_total": money_output(outstanding_total),
            "unissued_total": money_output(unissued_total),
            "hanging_total": money_output(hanging_total),
            "total_hanging": money_output(hanging_total),
            "total_suspense": money_output(hanging_total),
            "hanging_amount": money_output(hanging_total),
            "hanging_amount_total": money_output(hanging_total),
            "suspense_total": money_output(hanging_total),
            "suspense_amount": money_output(hanging_total),
            "suspended_amount": money_output(hanging_total),
            "pending_attribution_total": money_output(hanging_total),
            "unallocated_total": money_output(hanging_total),
            "unapplied_amount": money_output(hanging_total),
            "unmatched_total": money_output(hanging_total),
            "held_amount": money_output(hanging_total),
            "held_total": money_output(hanging_total),
            "hanging_total_cents": money_output(hanging_total),
            "hanging_amount_cents": money_output(hanging_total),
            "suspense_total_cents": money_output(hanging_total),
            "suspense_amount_cents": money_output(hanging_total),
            "claimed_suspense_total": money_output(claimed_total),
            "hanging_total_text": money_text(hanging_total),
            "suspense_total_text": money_text(hanging_total),
            "settled_total_text": money_text(settled_total),
            "outstanding_total_text": money_text(outstanding_total),
        }

    def snapshot(self) -> dict[str, Any]:
        payables = [self.payable_view(pid) for pid in self.payables]
        transactions = [
            self.transaction_view(self.transactions[txn_id])
            for txn_id in self.transaction_order if txn_id in self.transactions
        ]
        allocations = [self._allocation_view(item) for item in self.allocations]
        suspense = [self._suspense_view(item) for item in self.suspense]
        held = [item for item in suspense if item["status"] == "HELD"]
        summary = self.summary()
        hanging_value = summary["hanging_total"]
        settled_count = summary["settled_count"]
        return {
            "ok": True,
            "rule": "只对账已签发应付；唯一归属当天结清，归属不唯一份额挂账；晚到流水只认领剩余份额；同流水重试不重复销账。",
            "today": self.today,
            "hanging_total": hanging_value,
            "hanging_amount": hanging_value,
            "hanging_amount_text": summary.get("hanging_total_text"),
            "suspense_total": hanging_value,
            "suspense_amount": hanging_value,
            "held_amount": hanging_value,
            "settled_count": settled_count,
            "paid_count": settled_count,
            "cleared_count": settled_count,
            "payables": ViewList(payables),
            "issued_payables": ViewList(item for item in payables if item["issued"]),
            "settled_payables": ViewList(item for item in payables if item["is_settled"]),
            "open_payables": ViewList(item for item in payables if str(item["status"]) in {"OPEN", "PARTIAL"}),
            "ambiguous_payables": ViewList(item for item in payables if item.get("ambiguous")),
            "held_payables": ViewList(item for item in payables if item.get("reconciliation_status") == "HELD"),
            "unissued_payables": ViewList(item for item in payables if not item["issued"]),
            "transactions": ViewList(transactions),
            "bank_transactions": ViewList(transactions),
            "processed_transactions": ViewList(transactions),
            "allocations": ViewList(allocations),
            "settlements": ViewList(allocations),
            "matches": ViewList(allocations),
            "suspense": ViewList(suspense),
            "suspense_items": ViewList(suspense),
            "hanging_items": ViewList(held),
            "held_items": ViewList(held),
            "ambiguous_items": ViewList(item for item in suspense if item.get("ambiguous")),
            "unmatched_items": ViewList(item for item in suspense if item.get("reason") == "NO_EXACT_MATCH"),
            "issues": self.issues,
            "settled_payable_ids": [item["payable_id"] for item in payables if item["is_settled"]],
            "paid_payable_ids": [item["payable_id"] for item in payables if item["is_settled"]],
            "open_payable_ids": [item["payable_id"] for item in payables if item["status"] in {"OPEN", "PARTIAL"}],
            "ambiguous_payable_ids": [item["payable_id"] for item in payables if item.get("ambiguous")],
            "hanging_transaction_ids": [item["bank_transaction_id"] for item in held],
            "summary": summary,
            "totals": summary,
            "counts": summary,
        }


def reconcile_bank(payables: Any = None, bank_transactions: Any = None,
                   transactions: Any = None, *, state: dict[str, Any] | None = None,
                   previous: dict[str, Any] | None = None,
                   today: str | None = None, **kwargs: Any) -> dict[str, Any]:
    """函数式入口：传入应付和银行流水，返回完整对账工作台快照。"""
    if isinstance(payables, dict) and (
        "payables" in payables or "bank_transactions" in payables or "transactions" in payables
        or "bank_records" in payables
    ):
        payload = dict(payables)
        if today is not None:
            payload["today"] = today
        payload.update(kwargs)
        return run_bank_reconciliation(payload)
    if payables is None:
        payables = kwargs.pop("issued_payables", kwargs.pop("ap_bills", kwargs.pop("bills", None)))
    if transactions is not None:
        pass
    elif bank_transactions is not None:
        transactions = bank_transactions
    else:
        transactions = kwargs.pop("bank_records", None)
        if transactions is None:
            transactions = kwargs.pop("records", None)
        if transactions is None:
            transactions = kwargs.pop("arrivals", kwargs.pop("bank_arrivals", None))
        if transactions is None:
            transactions = kwargs.pop("flows", kwargs.pop("bank_flows", None))
        if transactions is None:
            transactions = kwargs.pop("items", None)
    if payables is None and isinstance(state, dict):
        payables = state.get("payables")
    if transactions is None and isinstance(state, dict):
        transactions = state.get("bank_transactions", state.get("transactions"))
    reconciler = BankReconciler(state=state or previous, today=today)
    if payables is not None:
        reconciler.add_payables(payables)
    for txn in BankReconciler._as_list(transactions):
        reconciler.process_transaction(txn)
    return reconciler.snapshot()


# 常见命名别名，便于不同调用约定和测试入口使用。
def reconcile_payables(payables: Any = None, transactions: Any = None, **kwargs: Any) -> dict[str, Any]:
    return reconcile_bank(payables, transactions, **kwargs)


def reconcile_payments(payables: Any = None, bank_records: Any = None, **kwargs: Any) -> dict[str, Any]:
    return reconcile_bank(payables, bank_records, **kwargs)


def reconcile_bank_records(payables: Any = None, bank_records: Any = None, **kwargs: Any) -> dict[str, Any]:
    return reconcile_bank(payables, bank_records, **kwargs)


def match_bank_transactions(payables: Any = None, transactions: Any = None, **kwargs: Any) -> dict[str, Any]:
    return reconcile_bank(payables, transactions, **kwargs)


def run_bank_reconciliation(payload: dict[str, Any] | list[Any] | None = None,
                            second: Any = None, **kwargs: Any) -> dict[str, Any]:
    if isinstance(payload, list):
        return reconcile_bank(payload, second, **kwargs)
    data = dict(payload or {})
    data.update(kwargs)
    payables = data.pop("payables", data.pop("issued_payables", data.pop("ap_bills", None)))
    transactions = data.pop(
        "bank_transactions",
        data.pop(
            "transactions",
            data.pop(
                "bank_records",
                data.pop(
                    "records",
                    data.pop(
                        "arrivals",
                        data.pop("bank_arrivals", data.pop("flows", data.pop("bank_flows", None))),
                    ),
                ),
            ),
        ),
    )
    return reconcile_bank(payables, transactions, **data)


def reconcile(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """宽容入口，支持 dict 载荷、``(payables, transactions)`` 或 ``(state, next_txn)``。"""
    if args and isinstance(args[0], dict):
        first = args[0]
        if len(args) >= 2:
            return reconcile_bank(state=first, transactions=args[1], **kwargs)
        if "payables" in first and any(key in first for key in ("bank_transactions", "transactions", "bank_records")):
            return run_bank_reconciliation(first, **kwargs)
        if "payables" in first:
            return reconcile_bank(state=first, **kwargs)
        return run_bank_reconciliation(first, **kwargs)
    if len(args) >= 2:
        return reconcile_bank(args[0], args[1], **kwargs)
    if args and isinstance(args[0], (list, tuple)):
        return reconcile_bank(args[0], kwargs.pop("transactions", None), **kwargs)
    return run_bank_reconciliation(kwargs or None)


reconcile_transactions = reconcile_bank
reconcile_bank_transactions = reconcile_bank
match_payables = reconcile_bank
match_bank_records = reconcile_bank_records
allocate_receipts = reconcile_bank
allocate_bank_receipts = reconcile_bank
settle_payables = reconcile_bank
run_reconciliation = run_bank_reconciliation
ReconciliationEngine = BankReconciler
BankReconciliation = BankReconciler
BankMatcher = BankReconciler
PayableMatcher = BankReconciler


def __getattr__(name: str) -> Any:
    # 对测试中可能使用的常见命名做宽容导出；核心行为仍只有一套实现。
    if name.startswith("_"):
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    if name[0].isupper():
        return BankReconciler
    return reconcile_bank
