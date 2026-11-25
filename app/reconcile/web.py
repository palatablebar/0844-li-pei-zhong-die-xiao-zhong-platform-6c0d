from __future__ import annotations

from typing import Any

from flask import Blueprint, current_app, jsonify, request

from app.reconcile.bank import (
    BankReconciliationError,
    run_bank_reconciliation,
)


bank_api = Blueprint("bank_api", __name__)


def service():
    return current_app.extensions["bank"]


def json_body() -> Any:
    body = request.get_json(silent=True)
    if isinstance(body, dict):
        key = request.headers.get("Idempotency-Key") or request.headers.get("X-Idempotency-Key")
        if key and not any(body.get(name) for name in ("bank_transaction_id", "transaction_id", "id", "flow_no")):
            body["bank_transaction_id"] = key
    return body


def extract_pair(body: dict[str, Any]):
    payables = body.get("payables")
    if payables is None:
        payables = body.get("issued_payables") or body.get("ap_bills")
    transactions = None
    for key in (
        "bank_transactions", "transactions", "bank_records", "records",
        "arrivals", "bank_arrivals", "flows", "bank_flows", "statements",
    ):
        if body.get(key) is not None:
            transactions = body[key]
            break
    return payables, transactions


@bank_api.get("/api/bank/state")
@bank_api.get("/api/bank/reconciliation")
@bank_api.get("/api/bank/reconcile")
@bank_api.get("/api/reconcile")
@bank_api.get("/api/reconciliation")
@bank_api.get("/api/reconciliations")
@bank_api.get("/api/reconcile/bank")
@bank_api.get("/api/bank-reconciliation")
@bank_api.get("/api/bank-reconciliations")
@bank_api.get("/api/bank-reconcile")
def get_state():
    return jsonify(service().snapshot())


@bank_api.post("/api/bank/reconcile")
@bank_api.post("/api/reconcile")
@bank_api.post("/api/bank/reconciliation")
@bank_api.post("/api/reconcile/bank")
@bank_api.post("/api/reconciliation")
@bank_api.post("/api/reconciliations")
@bank_api.post("/api/bank-reconciliation")
@bank_api.post("/api/bank-reconciliations")
@bank_api.post("/api/bank-reconcile")
@bank_api.post("/api/reconcile/run")
@bank_api.post("/api/bank/reconcile/run")
@bank_api.post("/api/bank/run")
@bank_api.post("/api/bank-reconciliation/run")
@bank_api.post("/api/reconciliation/run")
def reconcile():
    body = json_body()
    if body is None:
        body = {}
    if isinstance(body, list):
        # 直接提交流水列表时，使用当前工作台中的应付。
        return jsonify(service().process_many(body))

    payables, transactions = extract_pair(body)
    stateless = bool(body.get("stateless") or body.get("dry_run") or body.get("dryRun"))
    if stateless:
        return jsonify(run_bank_reconciliation(body))

    persist = body.get("persist", True)
    if not persist:
        return jsonify(run_bank_reconciliation(body))

    if payables is not None:
        service().add_payables(payables if isinstance(payables, list) else [payables])

    if transactions is None:
        # 允许把单条流水直接 POST 到 /api/bank/reconcile。
        if any(key in body for key in ("bank_transaction_id", "transaction_id", "id", "amount", "amount_cents")):
            transactions = body
        else:
            return jsonify(service().snapshot())

    if isinstance(transactions, list):
        return jsonify(service().process_many(transactions))
    return jsonify(service().process_transaction(transactions))


@bank_api.post("/api/payables")
@bank_api.post("/api/bank/payables")
@bank_api.post("/api/reconciliation/payables")
@bank_api.post("/api/bank-reconciliation/payables")
def add_payables():
    body = json_body()
    if isinstance(body, list):
        items = body
    elif isinstance(body, dict) and isinstance(body.get("payables"), list):
        items = body["payables"]
    elif body is not None:
        items = [body]
    else:
        raise BankReconciliationError("缺少应付数据")
    return jsonify(service().add_payables(items)), 201


@bank_api.get("/api/payables")
@bank_api.get("/api/bank/payables")
@bank_api.get("/api/reconciliation/payables")
@bank_api.get("/api/bank-reconciliation/payables")
def list_payables():
    snapshot = service().snapshot()
    return jsonify(snapshot["payables"])


@bank_api.post("/api/bank-transactions")
@bank_api.post("/api/transactions")
@bank_api.post("/api/bank/transactions")
@bank_api.post("/api/reconciliation/transactions")
@bank_api.post("/api/reconciliation/bank-transactions")
@bank_api.post("/api/bank-reconciliation/transactions")
def add_transaction():
    body = json_body()
    if body is None:
        raise BankReconciliationError("缺少银行流水")
    if isinstance(body, list):
        return jsonify(service().process_many(body)), 201
    if isinstance(body, dict) and isinstance(body.get("transactions"), list):
        return jsonify(service().process_many(body["transactions"])), 201
    return jsonify(service().process_transaction(body)), 201


@bank_api.get("/api/bank-transactions")
@bank_api.get("/api/transactions")
@bank_api.get("/api/bank/transactions")
@bank_api.get("/api/reconciliation/transactions")
@bank_api.get("/api/reconciliation/bank-transactions")
@bank_api.get("/api/bank-reconciliation/transactions")
def list_transactions():
    snapshot = service().snapshot()
    return jsonify(snapshot["transactions"])


@bank_api.post("/api/bank/transactions/<transaction_id>/retry")
@bank_api.post("/api/bank-transactions/<transaction_id>/retry")
@bank_api.post("/api/transactions/<transaction_id>/retry")
def retry_transaction(transaction_id: str):
    return jsonify(service().retry_transaction(transaction_id))


@bank_api.post("/api/bank/suspense/<suspense_id>/claim")
@bank_api.post("/api/suspense/<suspense_id>/claim")
def claim_suspense(suspense_id: str):
    body = json_body() or {}
    claims = body.get("allocations") or body.get("claims") or body.get("payable_ids")
    if claims is None and body.get("payable_id"):
        claims = [{"payable_id": body["payable_id"], "amount": body.get("amount")}]
    if claims is None:
        raise BankReconciliationError("缺少认领对象")
    return jsonify(service().claim_suspense(suspense_id, claims))


@bank_api.post("/api/bank/demo")
def load_demo():
    body = json_body() or {}
    through = body.get("through", request.args.get("through", "first"))
    return jsonify(service().load_demo(through=through))


@bank_api.post("/api/bank/reset")
def reset():
    return jsonify(service().reset())


@bank_api.get("/bank")
def bank_page():
    from flask import render_template

    return render_template("index.html", p=None, bank_only=False)
