import pytest

from app.reconcile.bank import BankReconciler, reconcile_bank


def payables():
    return [
        {"payable_id": "P1", "amount_cents": 100, "status": "ISSUED"},
        {"payable_id": "P2", "amount_cents": 200, "status": "ISSUED"},
        {"payable_id": "P3", "amount_cents": 200, "status": "ISSUED"},
        {"payable_id": "DRAFT", "amount_cents": 999, "status": "DRAFT"},
    ]


def test_only_issued_payables_are_eligible():
    result = reconcile_bank(payables(), [{"id": "B0", "amount_cents": 999}])
    draft = next(item for item in result["payables"] if item["id"] == "DRAFT")
    assert draft["status"] == "DRAFT"
    assert draft["is_settled"] is False
    assert result["summary"]["settled_count"] == 0
    assert result["summary"]["hanging_total"] == 999


def test_one_receipt_can_settle_multiple_payables():
    rows = [
        {"id": "P1", "amount_cents": 100, "status": "ISSUED"},
        {"id": "P2", "amount_cents": 200, "status": "ISSUED"},
    ]
    result = reconcile_bank(rows, [{"id": "B1", "amount_cents": 300}])
    assert {item["id"]: item["status"] for item in result["payables"]} == {
        "P1": "SETTLED", "P2": "SETTLED"
    }
    assert result["summary"]["hanging_total"] == 0
    assert result["summary"]["allocation_count"] == 2


def test_non_unique_full_match_is_held():
    rows = [
        {"id": "P1", "amount_cents": 100},
        {"id": "P2", "amount_cents": 200},
        {"id": "P3", "amount_cents": 300},
    ]
    result = reconcile_bank(rows, [{"id": "B1", "amount_cents": 300}])
    assert result["summary"]["settled_count"] == 0
    assert result["summary"]["hanging_total"] == 300
    held = result["hanging_items"][0]
    assert held["reason"] == "ATTRIBUTION_AMBIGUOUS"
    assert ["P3"] in held["candidate_sets"]
    assert ["P1", "P2"] in held["candidate_sets"]


def test_confirmed_common_share_settles_and_ambiguous_share_holds_same_day():
    reconciler = BankReconciler(payables(), today="2026-11-25")
    txn = reconciler.process_transaction({"id": "B1", "amount_cents": 300})
    assert [item["payable_id"] for item in txn["allocations"]] == ["P1"]
    assert txn["allocations"][0]["settlement_date"] == "2026-11-25"
    assert txn["suspense"][0]["amount"] == 200
    assert txn["suspense"][0]["common_payable_ids"] == ["P1"]
    p1 = reconciler.payable_view("P1")
    p2 = reconciler.payable_view("P2")
    assert p1["status"] == "SETTLED"
    assert p2["ambiguous"] is True
    assert reconciler.summary()["hanging_total"] == 200


def test_late_receipt_only_claims_remaining_share():
    reconciler = BankReconciler(payables(), today="2026-11-25")
    reconciler.process_transaction({"id": "B1", "amount_cents": 300})
    late = reconciler.process_transaction({
        "id": "B2", "amount_cents": 200, "payable_ids": ["P2"],
    })
    assert [item["payable_id"] for item in late["allocations"]] == ["P2"]
    assert reconciler.payable_view("P2")["status"] == "SETTLED"
    assert reconciler.payable_view("P3")["status"] == "OPEN"
    # 第一笔的歧义挂账不被晚到流水自动抹平。
    assert reconciler.summary()["hanging_total"] == 200


def test_settled_money_is_not_reallocated_to_another_payable():
    rows = [
        {"id": "P1", "amount_cents": 100},
        {"id": "P2", "amount_cents": 200},
        {"id": "P3", "amount_cents": 300},
    ]
    reconciler = BankReconciler(rows)
    first = reconciler.process_transaction({
        "id": "B1", "amount_cents": 300, "payable_ids": ["P1", "P2"],
    })
    assert len(first["allocations"]) == 2
    # 明确指向已结清 P1 的晚到流水不能把钱挪给 P3。
    retry_like_late = reconciler.process_transaction({
        "id": "B2", "amount_cents": 100, "payable_id": "P1",
    })
    assert retry_like_late["allocations"] == []
    assert retry_like_late["suspense"][0]["reason"] == "PAYABLE_ALREADY_SETTLED"
    assert reconciler.payable_view("P3")["status"] == "OPEN"


def test_same_transaction_retry_does_not_duplicate_settlement():
    reconciler = BankReconciler([
        {"id": "P1", "amount_cents": 100},
        {"id": "P2", "amount_cents": 200},
    ])
    original = {"id": "B1", "amount_cents": 300}
    first = reconciler.process_transaction(original)
    second = reconciler.process_transaction(dict(original))
    assert first["allocation_ids"] == second["allocation_ids"]
    assert second["duplicate"] is True
    assert len(reconciler.allocations) == 2
    assert reconciler.summary()["allocation_count"] == 2
    assert reconciler.summary()["replay_count"] == 1


def test_persistent_http_workbench_is_idempotent_and_renders(client):
    payload = {
        "payables": [
            {"payable_id": "HP1", "amount_cents": 10000, "status": "ISSUED"},
            {"payable_id": "HP2", "amount_cents": 10000, "status": "ISSUED"},
        ],
        "bank_transactions": [
            {"bank_transaction_id": "HB1", "amount_cents": 10000},
        ],
    }
    first = client.post("/api/bank/reconcile", json=payload)
    assert first.status_code == 200
    body = first.get_json()
    assert body["summary"]["hanging_total"] == 10000
    assert body["summary"]["settled_count"] == 0

    retry = client.post("/api/bank/transactions", json=payload["bank_transactions"][0])
    assert retry.status_code in (200, 201)
    assert retry.get_json()["duplicate"] is True
    state = client.get("/api/bank/state").get_json()
    assert state["summary"]["transaction_count"] == 1
    assert state["summary"]["allocation_count"] == 0
    assert state["summary"]["hanging_total"] == 10000

    page = client.get("/")
    assert page.status_code == 200
    assert "银行到账对账".encode() in page.data
    assert "data-testid=\"suspense-total\"".encode() in page.data


def test_demo_late_sequence_http(client):
    response = client.post("/api/bank/demo", json={"through": "all"})
    assert response.status_code == 200
    body = response.get_json()
    assert body["summary"]["settled_count"] == 3
    assert body["summary"]["hanging_total"] == 20000
    response = client.post("/api/bank/transactions/BK-20261125-001/retry")
    assert response.status_code == 200
    body = response.get_json()
    assert body["duplicate"] is True
    assert body["summary"]["allocation_count"] == 3
    assert body["summary"]["replay_count"] >= 1
