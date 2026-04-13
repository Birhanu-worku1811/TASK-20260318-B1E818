from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from sqlalchemy import select

os.environ.setdefault("APP_ENV", "dev")
os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///./offline_platform.db")
os.environ.setdefault("ALLOW_DEV_SQLITE_OVERRIDE", "true")

from app.domain.services import (
    NotificationEvent,
    auto_void_unsettled_orders,
    calculate_cart,
    compute_feature_value,
    create_project,
    create_user,
    process_exchange,
    process_refund,
    process_reverse_settlement,
    push_notification,
)
from app.infra.config import Settings, get_settings, validate_runtime_settings
from app.infra.db import Base, SessionLocal, engine
from app.infra.security import create_access_token, is_locked, utcnow
from app.main import app
from app.models.entities import (
    AccountingReversal,
    AccessLog,
    AfterSalesOrder,
    AuditLog,
    FeatureDefinition,
    Notification,
    Order,
    OrderLine,
    OrderStatus,
    PaymentRecord,
    PaymentMethod,
    PermissionChangeLog,
    Project,
    Product,
    ProjectStatus,
    PromotionRule,
    RoleType,
    ShiftSchedule,
    UserRoleBinding,
)


@pytest.fixture(autouse=True)
def reset_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


def _assert_success_envelope(body: dict) -> None:
    assert body["status"] == "success"
    assert "data" in body


def _assert_error_envelope(body: dict) -> None:
    assert body["status"] == "error"
    assert {"code", "message", "details"}.issubset(body.keys())


def _auth_headers(username: str, password: str) -> dict[str, str]:
    client = TestClient(app)
    resp = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    token = resp.json()["data"]["access_token"]
    return {"Authorization": f"Bearer {token}"}


def test_strong_jwt_secret_required_outside_dev():
    settings = Settings(app_env="prod", jwt_algorithm="HS256", jwt_secret="too-short")
    with pytest.raises(RuntimeError):
        validate_runtime_settings(settings)


def test_runtime_requires_postgresql_without_explicit_dev_override():
    with pytest.raises(RuntimeError):
        validate_runtime_settings(Settings(app_env="dev", database_url="sqlite+pysqlite:///./offline_platform.db", allow_dev_sqlite_override=False))

    validate_runtime_settings(Settings(app_env="dev", database_url="sqlite+pysqlite:///./offline_platform.db", allow_dev_sqlite_override=True))
    validate_runtime_settings(
        Settings(
            app_env="prod",
            database_url="postgresql+psycopg://postgres:postgres@localhost:5432/offline_platform",
            jwt_secret="a" * 32,
        )
    )


def test_aware_utc_lock_comparison_handles_naive_datetime():
    naive_future = datetime.utcnow() + timedelta(minutes=2)
    naive_past = datetime.utcnow() - timedelta(minutes=2)
    assert is_locked(naive_future) is True
    assert is_locked(naive_past) is False


def test_lockout_returns_423_with_error_envelope():
    with SessionLocal() as db:
        create_user(db, "cashier1", "StrongPass12!", "Cashier", RoleType.CASHIER, None, None)
    client = TestClient(app)
    for _ in range(5):
        client.post("/api/v1/auth/login", json={"username": "cashier1", "password": "bad"})
    locked = client.post("/api/v1/auth/login", json={"username": "cashier1", "password": "StrongPass12!"})
    assert locked.status_code == 423
    _assert_error_envelope(locked.json())
    assert locked.json()["code"] == "account_locked"


def test_login_uses_configured_expiry():
    with SessionLocal() as db:
        create_user(db, "cashier_exp", "StrongPass12!", "Cashier Exp", RoleType.CASHIER, None, None)
    client = TestClient(app)
    resp = client.post("/api/v1/auth/login", json={"username": "cashier_exp", "password": "StrongPass12!"})
    assert resp.status_code == 200
    _assert_success_envelope(resp.json())
    assert resp.json()["data"]["expires_in"] == get_settings().access_token_expire_minutes * 60


def test_canonical_order_routes_envelope_and_refund_idempotency():
    with SessionLocal() as db:
        create_user(db, "cashier2", "StrongPass12!", "Cashier 2", RoleType.CASHIER, None, None)
        db.add(Product(name="Milk", barcode="123", internal_code="MILK-1", pinyin="niu nai", unit_price=10.0))
        db.add(PromotionRule(name="Spend10Save1", scope="order", rule_type="spend_and_save", config={"threshold": 10, "discount": 1}))
        db.commit()
        product = db.scalar(select(Product).where(Product.barcode == "123"))
        assert product is not None

    client = TestClient(app)
    headers = _auth_headers("cashier2", "StrongPass12!")

    calc = client.post("/api/v1/orders/calculate", json={"lines": [{"product_id": str(product.id), "quantity": 1}]}, headers=headers)
    assert calc.status_code == 200
    _assert_success_envelope(calc.json())
    assert calc.json()["data"]["final_total"] == 9.0

    checkout = client.post("/api/v1/orders/checkout", json={"customer_name": "Alice", "lines": [{"product_id": str(product.id), "quantity": 1}]}, headers=headers)
    assert checkout.status_code == 200
    _assert_success_envelope(checkout.json())
    order_id = checkout.json()["data"]["order_id"]

    settle = client.post(f"/api/v1/orders/{order_id}/settle", json={"payments": [{"method": "cash", "amount": 9.0}]}, headers=headers)
    assert settle.status_code == 200
    _assert_success_envelope(settle.json())

    with SessionLocal() as db:
        order_line_id = db.scalar(select(OrderLine.id).where(OrderLine.order_id == uuid.UUID(order_id)))

    refund_req = {
        "order_id": order_id,
        "reason": "damaged",
        "line_refunds": [{"order_line_id": order_line_id, "quantity": 1, "amount": 9.0}],
    }
    r1 = client.post("/api/v1/after-sales/refund", json=refund_req, headers={**headers, "Idempotency-Key": "k1"})
    r2 = client.post("/api/v1/after-sales/refund", json=refund_req, headers={**headers, "Idempotency-Key": "k1"})
    assert r1.status_code == 200 and r2.status_code == 200
    _assert_success_envelope(r1.json())
    _assert_success_envelope(r2.json())
    assert r1.json()["data"]["refund_id"] == r2.json()["data"]["refund_id"]


def test_project_submit_respects_throttle_and_diff_v1_v2():
    with SessionLocal() as db:
        applicant = create_user(db, "applicant1", "StrongPass12!", "Applicant", RoleType.PROJECT_APPLICANT, None, None)
        project = create_project(db, applicant.id, "P1", {"goal": "x"})
        project_id = project.id
        project.status = ProjectStatus.REJECTED
        db.commit()
        # pre-existing throttled event should not block submit notification flow
        push_notification(
            db,
            NotificationEvent(
                event_type="pending_approval",
                object_id=str(project_id),
                recipient_user_id=applicant.id,
                message="Pre-existing",
            ),
        )

    client = TestClient(app)
    headers = _auth_headers("applicant1", "StrongPass12!")
    submit = client.post(f"/api/v1/projects/{project_id}/submit", json={"content": {"goal": "x", "budget": 100}}, headers=headers)
    assert submit.status_code == 200
    _assert_success_envelope(submit.json())
    assert submit.json()["data"]["notification_id"] is None

    diff = client.get(f"/api/v1/projects/{project_id}/diff?v1=1&v2=2", headers=headers)
    assert diff.status_code == 200
    _assert_success_envelope(diff.json())
    assert "budget" in diff.json()["data"]["diff"]

    with SessionLocal() as db:
        rows = db.scalars(select(Notification).where(Notification.object_id == str(project_id))).all()
        assert len(rows) == 1


def test_project_status_action_contract_and_state_machine():
    with SessionLocal() as db:
        applicant = create_user(db, "applicant_action", "StrongPass12!", "Applicant", RoleType.PROJECT_APPLICANT, None, None)
        reviewer = create_user(db, "reviewer1", "StrongPass12!", "Reviewer", RoleType.REVIEWER, None, None)
        project = create_project(db, applicant.id, "P2", {"goal": "y"})
        project_id = project.id
        project.status = ProjectStatus.REJECTED
        db.commit()

    client = TestClient(app)
    app_headers = _auth_headers("applicant_action", "StrongPass12!")
    rev_headers = _auth_headers("reviewer1", "StrongPass12!")
    submit = client.post(f"/api/v1/projects/{project_id}/submit", json={"content": {"goal": "y", "budget": 200}}, headers=app_headers)
    assert submit.status_code == 200
    start_review = client.patch(f"/api/v1/projects/{project_id}/status", json={"action": "start_review"}, headers=rev_headers)
    assert start_review.status_code == 200
    _assert_success_envelope(start_review.json())
    assert start_review.json()["data"]["action"] == "start_review"
    assert start_review.json()["data"]["status"] == "under_review"

    invalid = client.patch(f"/api/v1/projects/{project_id}/status", json={"action": "resubmit"}, headers=rev_headers)
    assert invalid.status_code == 400
    _assert_error_envelope(invalid.json())
    assert invalid.json()["code"] == "resubmit_requires_submit_flow"
    with SessionLocal() as db:
        current = db.get(Project, project_id)
        assert current.current_version_no == 2


def test_metrics_date_start_date_end_and_downloadable_export():
    with SessionLocal() as db:
        create_user(db, "admin1", "StrongPass12!", "Admin", RoleType.OP_ADMIN, None, None)
    client = TestClient(app)
    headers = _auth_headers("admin1", "StrongPass12!")

    agg = client.post("/api/v1/analytics/aggregate", headers=headers)
    assert agg.status_code == 200
    _assert_success_envelope(agg.json())

    date_start = (utcnow() - timedelta(days=1)).isoformat()
    date_end = (utcnow() + timedelta(days=1)).isoformat()
    metrics = client.get("/api/v1/analytics/daily-metrics", params={"date_start": date_start, "date_end": date_end}, headers=headers)
    assert metrics.status_code == 200
    _assert_success_envelope(metrics.json())

    export = client.get("/api/v1/analytics/export", params={"date_start": date_start, "date_end": date_end}, headers=headers)
    assert export.status_code == 200
    assert "text/csv" in export.headers["content-type"]


def test_negative_auth_and_cross_user_denials():
    with SessionLocal() as db:
        owner = create_user(db, "owner1", "StrongPass12!", "Owner", RoleType.PROJECT_APPLICANT, None, None)
        other = create_user(db, "other1", "StrongPass12!", "Other", RoleType.PROJECT_APPLICANT, None, None)
        project = create_project(db, owner.id, "Secure Project", {"goal": "secure"})
        project_id = project.id
        project.status = ProjectStatus.REJECTED
        db.commit()
        note = push_notification(
            db,
            NotificationEvent(
                event_type="pending_approval",
                object_id=str(project_id),
                recipient_user_id=owner.id,
                message="for owner only",
            ),
        )
        assert note is not None
        notification_id = note.id

    client = TestClient(app)
    owner_headers = _auth_headers("owner1", "StrongPass12!")
    other_headers = _auth_headers("other1", "StrongPass12!")

    # 401 without bearer token on protected route.
    no_auth = client.post("/api/v1/orders/calculate", json={"lines": []})
    assert no_auth.status_code == 401
    _assert_error_envelope(no_auth.json())

    # Owner upload works.
    upload = client.post(
        f"/api/v1/projects/{project_id}/attachments",
        files={"file": ("proposal.pdf", b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n", "application/pdf")},
        headers=owner_headers,
    )
    assert upload.status_code == 200
    attachment_id = upload.json()["data"]["id"]

    # Cross-user project/attachment/notification access is denied.
    submit_denied = client.post(f"/api/v1/projects/{project_id}/submit", json={"content": {"goal": "x2"}}, headers=other_headers)
    assert submit_denied.status_code == 403
    _assert_error_envelope(submit_denied.json())
    assert submit_denied.json()["code"] == "forbidden_project_access"

    diff_denied = client.get(f"/api/v1/projects/{project_id}/diff?v1=1&v2=1", headers=other_headers)
    assert diff_denied.status_code == 403
    assert diff_denied.json()["code"] == "forbidden_project_access"

    verify_denied = client.get(f"/api/v1/attachments/{attachment_id}/verify", headers=other_headers)
    assert verify_denied.status_code == 403
    assert verify_denied.json()["code"] == "forbidden_project_access"

    notification_denied = client.patch(f"/api/v1/notifications/{notification_id}/read", headers=other_headers)
    assert notification_denied.status_code == 403
    assert notification_denied.json()["code"] == "forbidden_notification_access"


def test_internal_error_is_client_safe(monkeypatch):
    with SessionLocal() as db:
        create_user(db, "boom_user", "StrongPass12!", "Boom", RoleType.CASHIER, None, None)
    client = TestClient(app, raise_server_exceptions=False)
    headers = _auth_headers("boom_user", "StrongPass12!")

    def _boom(*_args, **_kwargs):
        raise RuntimeError("sensitive stack detail")

    monkeypatch.setattr("app.api.v1.routes.product_search", _boom)
    resp = client.get("/api/v1/products/search?q=test", headers=headers)
    assert resp.status_code == 500
    _assert_error_envelope(resp.json())
    assert resp.json()["message"] == "Internal server error"


def test_promotion_variants_buy_get_tiered_and_purchase_limit_failure():
    with SessionLocal() as db:
        create_user(db, "promo_user", "StrongPass12!", "Promo", RoleType.CASHIER, None, None)
        p1 = Product(name="Soda", barcode="s1", internal_code="SODA", pinyin="soda", unit_price=10.0)
        p2 = Product(name="Snack", barcode="sn1", internal_code="SNACK", pinyin="snack", unit_price=12.0)
        db.add_all([p1, p2])
        db.flush()
        db.add(
            PromotionRule(
                name="buy2get1",
                scope="item",
                rule_type="buy_and_get",
                config={"product_id": str(p1.id), "buy_qty": 2, "get_qty": 1},
            )
        )
        db.add(
            PromotionRule(
                name="tiered_order",
                scope="order",
                rule_type="tiered_pricing",
                config={"tiers": [{"threshold": 20, "discount": 2}, {"threshold": 30, "discount": 5}]},
            )
        )
        db.add(
            PromotionRule(
                name="limit_soda",
                scope="item",
                rule_type="purchase_limit",
                config={"product_id": str(p1.id), "max_qty": 3},
            )
        )
        db.commit()

        # Item-tier boundaries: below/equal/above threshold.
        below = calculate_cart(db, [{"product_id": str(p1.id), "quantity": 1}])
        equal = calculate_cart(db, [{"product_id": str(p1.id), "quantity": 2}])
        above = calculate_cart(db, [{"product_id": str(p1.id), "quantity": 3}])
        assert below["discount_total"] == 0.0
        assert equal["discount_total"] >= 10.0
        assert above["discount_total"] >= 10.0

        # Order-tier boundaries on subtotal: below/equal/above threshold.
        order_below = calculate_cart(db, [{"product_id": str(p2.id), "quantity": 1}])  # subtotal 12
        order_equal = calculate_cart(db, [{"product_id": str(p2.id), "quantity": 2}])  # subtotal 24
        order_above = calculate_cart(db, [{"product_id": str(p2.id), "quantity": 3}])  # subtotal 36
        assert order_below["discount_total"] < order_equal["discount_total"]
        assert order_above["discount_total"] >= order_equal["discount_total"]

        with pytest.raises(Exception):
            calculate_cart(db, [{"product_id": str(p1.id), "quantity": 4}])


def test_auto_void_timing():
    with SessionLocal() as db:
        user = create_user(db, "void_user", "StrongPass12!", "Void", RoleType.CASHIER, None, None)
        order = Order(created_by_user_id=user.id, customer_name="VoidMe")
        db.add(order)
        db.commit()
        db.refresh(order)
        order.created_at = utcnow() - timedelta(minutes=get_settings().order_auto_void_minutes + 1)
        db.commit()
        voided = auto_void_unsettled_orders(db)
        db.refresh(order)
        assert voided == 1
        assert order.status.value == "void"


def test_refund_rejections_seven_day_and_cumulative_cap():
    with SessionLocal() as db:
        user = create_user(db, "refund_user", "StrongPass12!", "Refund", RoleType.CASHIER, None, None)
        product = Product(name="Item", barcode="it1", internal_code="IT1", pinyin="it", unit_price=20)
        db.add(product)
        db.flush()
        order = Order(created_by_user_id=user.id, status=OrderStatus.SETTLED, subtotal_amount=20, final_amount=20, discount_amount=0, settled_at=utcnow())
        db.add(order)
        db.flush()
        line = OrderLine(order_id=order.id, product_id=product.id, quantity=1, unit_price=20, line_discount=0)
        payment = PaymentRecord(order_id=order.id, method=PaymentMethod.CASH, amount=20)
        db.add_all([line, payment])
        db.commit()
        db.refresh(order)
        db.refresh(line)

        # cumulative cap
        process_refund(
            db,
            order_id=order.id,
            reason="first",
            idempotency_key="r1",
            user_id=user.id,
            line_refunds=[{"order_line_id": line.id, "quantity": 1, "amount": 10}],
        )
        with pytest.raises(Exception):
            process_refund(
                db,
                order_id=order.id,
                reason="second too much",
                idempotency_key="r2",
                user_id=user.id,
                line_refunds=[{"order_line_id": line.id, "quantity": 1, "amount": 11}],
            )

        # >7 days
        order.settled_at = utcnow() - timedelta(days=8)
        db.commit()
        with pytest.raises(Exception):
            process_refund(
                db,
                order_id=order.id,
                reason="late",
                idempotency_key="r3",
                user_id=user.id,
                line_refunds=[{"order_line_id": line.id, "quantity": 1, "amount": 1}],
            )


def test_aftersales_window_comparison_handles_naive_db_datetimes():
    with SessionLocal() as db:
        user = create_user(db, "tz_refund_user", "StrongPass12!", "TZ Refund", RoleType.CASHIER, None, None)
        product = Product(name="TZ Item", barcode="tz1", internal_code="TZ1", pinyin="tz", unit_price=20)
        db.add(product)
        db.flush()
        order = Order(
            created_by_user_id=user.id,
            status=OrderStatus.SETTLED,
            subtotal_amount=20,
            final_amount=20,
            discount_amount=0,
            settled_at=datetime.now(timezone.utc).replace(tzinfo=None),  # simulate naive DB value
        )
        db.add(order)
        db.flush()
        line = OrderLine(order_id=order.id, product_id=product.id, quantity=1, unit_price=20, line_discount=0)
        payment = PaymentRecord(order_id=order.id, method=PaymentMethod.CASH, amount=20)
        db.add_all([line, payment])
        db.commit()

        row = process_refund(
            db,
            order_id=order.id,
            reason="timezone-safe",
            idempotency_key="tz-refund-1",
            user_id=user.id,
            line_refunds=[{"order_line_id": line.id, "quantity": 1, "amount": 10}],
        )
        db.commit()
        assert row.type == "refund"


def test_notification_throttle_bucket_boundary(monkeypatch):
    with SessionLocal() as db:
        user = create_user(db, "note_user", "StrongPass12!", "Note", RoleType.CASHIER, None, None)
        event = NotificationEvent(event_type="budget_alert", object_id="obj-1", recipient_user_id=user.id, message="m1")
        first = push_notification(db, event)
        assert first is not None
        second = push_notification(db, event)
        assert second is None

        base = utcnow()
        monkeypatch.setattr("app.domain.services.utcnow", lambda: base + timedelta(minutes=10, seconds=1))
        third = push_notification(db, event)
        assert third is not None


def test_after_sales_exchange_and_reverse_settlement_paths():
    with SessionLocal() as db:
        user = create_user(db, "aftersales_user", "StrongPass12!", "AS", RoleType.CASHIER, None, None)
        p = Product(name="AS-Item", barcode="as1", internal_code="AS1", pinyin="as", unit_price=15)
        db.add(p)
        db.flush()
        order = Order(created_by_user_id=user.id, status=OrderStatus.SETTLED, subtotal_amount=30, final_amount=30, discount_amount=0, settled_at=utcnow())
        db.add(order)
        db.flush()
        line = OrderLine(order_id=order.id, product_id=p.id, quantity=2, unit_price=15, line_discount=0)
        pay1 = PaymentRecord(order_id=order.id, method=PaymentMethod.CASH, amount=10)
        pay2 = PaymentRecord(order_id=order.id, method=PaymentMethod.BANK_CARD, amount=20)
        db.add_all([line, pay1, pay2])
        db.commit()
        db.refresh(line)

        ex = process_exchange(
            db,
            order_id=order.id,
            reason="size issue",
            idempotency_key="ex-1",
            user_id=user.id,
            line_exchanges=[{"order_line_id": line.id, "quantity": 1, "amount": 15}],
        )
        assert ex.type == "exchange"

        rs = process_reverse_settlement(
            db,
            order_id=order.id,
            reason="cancel all",
            idempotency_key="rs-1",
            user_id=user.id,
        )
        assert rs.type == "reverse_settlement"
        reversals = db.scalars(select(AccountingReversal).where(AccountingReversal.after_sales_order_id == rs.id)).all()
        assert len(reversals) == 2


def test_feature_library_computation_window_frequency_correlation():
    with SessionLocal() as db:
        admin = create_user(db, "feature_admin", "StrongPass12!", "FAdmin", RoleType.OP_ADMIN, None, None)
        fd_window = FeatureDefinition(name="f_window", calculation_type="window", ttl_seconds=60, lineage_note="ln")
        fd_freq = FeatureDefinition(name="f_freq", calculation_type="frequency", ttl_seconds=60, lineage_note="ln")
        fd_corr = FeatureDefinition(name="f_corr", calculation_type="correlation", ttl_seconds=60, lineage_note="ln")
        db.add_all([fd_window, fd_freq, fd_corr])
        db.commit()
        db.refresh(fd_window)
        db.refresh(fd_freq)
        db.refresh(fd_corr)

        w = compute_feature_value(db, feature_id=fd_window.id, entity_key="u1", payload={"values": [1, 2, 3, 4], "window_size": 2})
        f = compute_feature_value(db, feature_id=fd_freq.id, entity_key="u1", payload={"events": [1, 2, 3, 4, 5]})
        c = compute_feature_value(db, feature_id=fd_corr.id, entity_key="u1", payload={"series_a": [1, 2, 3], "series_b": [2, 4, 6]})
        assert w["value"] == 3.5
        assert f["value"] == 5.0
        assert c["value"] > 0.99
        assert w["lineage"]["consistency_hash"]


def test_pending_approval_notifies_reviewer_and_op_admin_not_submitter():
    with SessionLocal() as db:
        applicant = create_user(db, "notify_app", "StrongPass12!", "Applicant", RoleType.PROJECT_APPLICANT, None, None)
        create_user(db, "notify_rev", "StrongPass12!", "Reviewer", RoleType.REVIEWER, None, None)
        create_user(db, "notify_admin", "StrongPass12!", "Admin", RoleType.OP_ADMIN, None, None)
        project = create_project(db, applicant.id, "Notify Project", {"goal": "n"})
        project.status = ProjectStatus.REJECTED
        db.commit()
        project_id = project.id

    client = TestClient(app)
    app_headers = _auth_headers("notify_app", "StrongPass12!")
    rev_headers = _auth_headers("notify_rev", "StrongPass12!")
    admin_headers = _auth_headers("notify_admin", "StrongPass12!")

    submit = client.post(f"/api/v1/projects/{project_id}/submit", json={"content": {"goal": "n", "budget": 1}}, headers=app_headers)
    assert submit.status_code == 200

    app_notes = client.get("/api/v1/notifications", headers=app_headers).json()["data"]
    rev_notes = client.get("/api/v1/notifications", headers=rev_headers).json()["data"]
    admin_notes = client.get("/api/v1/notifications", headers=admin_headers).json()["data"]
    assert not any(n["event_type"] == "pending_approval" and n["object_id"] == str(project_id) for n in app_notes)
    assert any(n["event_type"] == "pending_approval" and n["object_id"] == str(project_id) for n in rev_notes)
    assert any(n["event_type"] == "pending_approval" and n["object_id"] == str(project_id) for n in admin_notes)


def test_contract_and_budget_trigger_producers_with_throttling():
    with SessionLocal() as db:
        create_user(db, "ops_mgr", "StrongPass12!", "OpsMgr", RoleType.STORE_MANAGER, None, None)
        create_user(db, "ops_admin", "StrongPass12!", "OpsAdmin", RoleType.OP_ADMIN, None, None)
    client = TestClient(app)
    mgr_headers = _auth_headers("ops_mgr", "StrongPass12!")
    admin_headers = _auth_headers("ops_admin", "StrongPass12!")

    c1 = client.post(
        "/api/v1/notifications/triggers/contract-expiration",
        json={"object_id": "contract-1", "message": "contract expiring"},
        headers=mgr_headers,
    )
    c2 = client.post(
        "/api/v1/notifications/triggers/contract-expiration",
        json={"object_id": "contract-1", "message": "contract expiring"},
        headers=mgr_headers,
    )
    assert c1.status_code == 200 and c2.status_code == 200

    b1 = client.post(
        "/api/v1/notifications/triggers/budget-alert",
        json={"object_id": "budget-1", "message": "budget alert"},
        headers=mgr_headers,
    )
    b2 = client.post(
        "/api/v1/notifications/triggers/budget-alert",
        json={"object_id": "budget-1", "message": "budget alert"},
        headers=mgr_headers,
    )
    assert b1.status_code == 200 and b2.status_code == 200

    notes = client.get("/api/v1/notifications", headers=admin_headers).json()["data"]
    contract_count = len([n for n in notes if n["event_type"] == "contract_expiration" and n["object_id"] == "contract-1"])
    budget_count = len([n for n in notes if n["event_type"] == "budget_alert" and n["object_id"] == "budget-1"])
    assert contract_count == 1
    assert budget_count == 1


def test_attachment_validation_checks_extension_and_signature():
    with SessionLocal() as db:
        owner = create_user(db, "attach_owner", "StrongPass12!", "Attach Owner", RoleType.PROJECT_APPLICANT, None, None)
        project = create_project(db, owner.id, "Attach Project", {"goal": "attach"})
        project_id = project.id

    client = TestClient(app)
    headers = _auth_headers("attach_owner", "StrongPass12!")

    mismatch = client.post(
        f"/api/v1/projects/{project_id}/attachments",
        files={"file": ("doc.pdf", b"\x89PNG\r\n\x1A\n\x00\x00", "application/pdf")},
        headers=headers,
    )
    assert mismatch.status_code == 400
    _assert_error_envelope(mismatch.json())
    assert mismatch.json()["code"] == "attachment_invalid"

    valid = client.post(
        f"/api/v1/projects/{project_id}/attachments",
        files={"file": ("image.png", b"\x89PNG\r\n\x1A\n\x00\x00\x00\rIHDR", "image/png")},
        headers=headers,
    )
    assert valid.status_code == 200
    _assert_success_envelope(valid.json())


def test_role_based_403_for_protected_endpoint_groups():
    with SessionLocal() as db:
        create_user(db, "limited_user", "StrongPass12!", "Limited", RoleType.PROJECT_APPLICANT, None, None)
    client = TestClient(app)
    headers = _auth_headers("limited_user", "StrongPass12!")
    now = utcnow().isoformat()

    promotion = client.post(
        "/api/v1/promotions",
        json={"name": "Nope", "scope": "order", "rule_type": "spend_and_save", "config": {"threshold": 10, "discount": 1}},
        headers=headers,
    )
    assert promotion.status_code == 403

    config = client.post(
        "/api/v1/configs",
        json={"config_key": "ops.test", "payload": {"enabled": True}, "rollout_percent": 100},
        headers=headers,
    )
    assert config.status_code == 403

    analytics = client.get(f"/api/v1/analytics/daily-metrics?date_start={now}&date_end={now}", headers=headers)
    assert analytics.status_code == 403

    feature = client.post(
        "/api/v1/features/definitions?name=f1&calculation_type=window&ttl_seconds=60&lineage_note=test",
        headers=headers,
    )
    assert feature.status_code == 403

    for resp in (promotion, config, analytics, feature):
        _assert_error_envelope(resp.json())
        assert resp.json()["code"] == "forbidden"


def test_invalid_and_expired_tokens_rejected_for_http_and_websocket():
    with SessionLocal() as db:
        user = create_user(db, "token_user", "StrongPass12!", "Token User", RoleType.CASHIER, None, None)
        expired_token, _ = create_access_token(subject=str(user.id), roles=[RoleType.CASHIER.value], expires_minutes=-1)
    client = TestClient(app)

    invalid = client.get("/api/v1/products/search?q=milk", headers={"Authorization": "Bearer bad.token.value"})
    assert invalid.status_code == 401
    _assert_error_envelope(invalid.json())
    assert invalid.json()["code"] == "invalid_token"

    expired = client.get("/api/v1/products/search?q=milk", headers={"Authorization": f"Bearer {expired_token}"})
    assert expired.status_code == 401
    _assert_error_envelope(expired.json())
    assert expired.json()["code"] == "invalid_token"

    with pytest.raises(WebSocketDisconnect) as invalid_ws:
        with client.websocket_connect("/api/v1/notifications/stream?token=bad.token.value") as ws:
            ws.receive_text()
    assert invalid_ws.value.code == 1008

    with pytest.raises(WebSocketDisconnect) as expired_ws:
        with client.websocket_connect(f"/api/v1/notifications/stream?token={expired_token}") as ws:
            ws.receive_text()
    assert expired_ws.value.code == 1008


def test_websocket_auth_enforces_password_change_and_active_user_checks():
    with SessionLocal() as db:
        user = create_user(db, "ws_guard_user", "StrongPass12!", "WS Guard", RoleType.CASHIER, None, None)
        user.password_change_required = True
        db.commit()
        password_change_token, _ = create_access_token(subject=str(user.id), roles=[RoleType.CASHIER.value], expires_minutes=5)

        user.password_change_required = False
        user.is_active = False
        db.commit()
        inactive_token, _ = create_access_token(subject=str(user.id), roles=[RoleType.CASHIER.value], expires_minutes=5)
    client = TestClient(app)

    with pytest.raises(WebSocketDisconnect) as pwd_required_ws:
        with client.websocket_connect(f"/api/v1/notifications/stream?token={password_change_token}") as ws:
            ws.receive_text()
    assert pwd_required_ws.value.code == 1008

    with pytest.raises(WebSocketDisconnect) as inactive_ws:
        with client.websocket_connect(f"/api/v1/notifications/stream?token={inactive_token}") as ws:
            ws.receive_text()
    assert inactive_ws.value.code == 1008


def test_sensitive_endpoints_write_access_logs():
    with SessionLocal() as db:
        applicant = create_user(db, "audit_app", "StrongPass12!", "Audit Applicant", RoleType.PROJECT_APPLICANT, None, None)
        reviewer = create_user(db, "audit_rev", "StrongPass12!", "Audit Reviewer", RoleType.REVIEWER, None, None)
        cashier = create_user(db, "audit_cash", "StrongPass12!", "Audit Cashier", RoleType.CASHIER, None, None)
        product = Product(name="Audit Item", barcode="audit-1", internal_code="AUD1", pinyin="audit", unit_price=12.0)
        db.add(product)
        db.flush()
        project = create_project(db, applicant.id, "Audit Project", {"goal": "audit"})
        note = push_notification(
            db,
            NotificationEvent(
                event_type="pending_approval",
                object_id=str(project.id),
                recipient_user_id=applicant.id,
                message="to read",
            ),
        )
        assert note is not None
        project_id = project.id
        notification_id = note.id
        product_id = product.id

    client = TestClient(app)
    app_headers = _auth_headers("audit_app", "StrongPass12!")
    rev_headers = _auth_headers("audit_rev", "StrongPass12!")
    cash_headers = _auth_headers("audit_cash", "StrongPass12!")

    upload = client.post(
        f"/api/v1/projects/{project_id}/attachments",
        files={"file": ("audit.pdf", b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n", "application/pdf")},
        headers=app_headers,
    )
    assert upload.status_code == 200
    attachment_id = upload.json()["data"]["id"]

    verify = client.get(f"/api/v1/attachments/{attachment_id}/verify", headers=app_headers)
    assert verify.status_code == 200

    submit = client.post(f"/api/v1/projects/{project_id}/submit", json={"content": {"goal": "audit", "v": 2}}, headers=app_headers)
    assert submit.status_code == 200

    review = client.patch(f"/api/v1/projects/{project_id}/status", json={"action": "start_review"}, headers=rev_headers)
    assert review.status_code == 200

    checkout = client.post(
        "/api/v1/orders/checkout",
        json={"customer_name": "Audit", "lines": [{"product_id": str(product_id), "quantity": 1}]},
        headers=cash_headers,
    )
    assert checkout.status_code == 200
    order_id = checkout.json()["data"]["order_id"]
    settle = client.post(
        f"/api/v1/orders/{order_id}/settle",
        json={"payments": [{"method": "cash", "amount": 12.0}]},
        headers=cash_headers,
    )
    assert settle.status_code == 200
    with SessionLocal() as db:
        order_line_id = db.scalar(select(OrderLine.id).where(OrderLine.order_id == uuid.UUID(order_id)))
    refund = client.post(
        "/api/v1/after-sales/refund",
        json={
            "order_id": order_id,
            "reason": "audit-refund",
            "line_refunds": [{"order_line_id": order_line_id, "quantity": 1, "amount": 12.0}],
        },
        headers={**cash_headers, "Idempotency-Key": "audit-refund-key"},
    )
    assert refund.status_code == 200

    read_note = client.patch(f"/api/v1/notifications/{notification_id}/read", headers=app_headers)
    assert read_note.status_code == 200

    with SessionLocal() as db:
        actions = [row.action for row in db.scalars(select(AccessLog)).all()]
    assert "attachment_upload" in actions
    assert "attachment_verify" in actions
    assert "project_review_status_change" in actions
    assert "after_sales_refund" in actions
    assert "notification_mark_read" in actions


def test_shift_scheduling_happy_path_unauthorized_and_audit():
    with SessionLocal() as db:
        manager = create_user(db, "shift_mgr", "StrongPass12!", "Shift Manager", RoleType.OP_ADMIN, None, None)
        cashier = create_user(db, "shift_cashier", "StrongPass12!", "Shift Cashier", RoleType.CASHIER, None, None)
        create_user(db, "shift_app", "StrongPass12!", "Shift Applicant", RoleType.PROJECT_APPLICANT, None, None)
        manager_id = manager.id
        cashier_id = cashier.id

    client = TestClient(app)
    manager_headers = _auth_headers("shift_mgr", "StrongPass12!")
    cashier_headers = _auth_headers("shift_cashier", "StrongPass12!")
    applicant_headers = _auth_headers("shift_app", "StrongPass12!")
    start = (utcnow() + timedelta(hours=1)).isoformat()
    end = (utcnow() + timedelta(hours=9)).isoformat()

    create_resp = client.post(
        "/api/v1/shifts",
        json={"assigned_user_id": str(cashier_id), "starts_at": start, "ends_at": end, "note": "morning"},
        headers=manager_headers,
    )
    assert create_resp.status_code == 200
    shift_id = create_resp.json()["data"]["id"]

    update_resp = client.patch(
        f"/api/v1/shifts/{shift_id}",
        json={"note": "updated note"},
        headers=manager_headers,
    )
    assert update_resp.status_code == 200

    status_resp = client.patch(
        f"/api/v1/shifts/{shift_id}/status",
        json={"status": "active"},
        headers=manager_headers,
    )
    assert status_resp.status_code == 200
    assert status_resp.json()["data"]["status"] == "active"

    my_resp = client.get("/api/v1/shifts/me", headers=cashier_headers)
    assert my_resp.status_code == 200
    assert any(row["id"] == shift_id for row in my_resp.json()["data"])

    forbidden = client.post(
        "/api/v1/shifts",
        json={"assigned_user_id": str(cashier_id), "starts_at": start, "ends_at": end, "note": "bad"},
        headers=applicant_headers,
    )
    assert forbidden.status_code == 403
    _assert_error_envelope(forbidden.json())
    assert forbidden.json()["code"] == "forbidden"

    with SessionLocal() as db:
        shift = db.get(ShiftSchedule, uuid.UUID(shift_id))
        assert shift is not None
        shift_audits = db.scalars(select(AuditLog).where(AuditLog.category.in_(["shift_created", "shift_updated", "shift_status_changed"]))).all()
        assert len(shift_audits) >= 3
        shift_access = db.scalars(select(AccessLog).where(AccessLog.action.in_(["shift_create", "shift_update", "shift_status_update"]))).all()
        assert len(shift_access) == 3
        assert str(shift.created_by_user_id) == str(manager_id)
        assert str(shift.assigned_user_id) == str(cashier_id)


def test_permission_change_lifecycle_happy_path_unauthorized_and_audit():
    with SessionLocal() as db:
        admin = create_user(db, "perm_admin", "StrongPass12!", "Perm Admin", RoleType.OP_ADMIN, None, None)
        target = create_user(db, "perm_target", "StrongPass12!", "Perm Target", RoleType.CASHIER, None, None)
        manager = create_user(db, "perm_mgr", "StrongPass12!", "Perm Manager", RoleType.STORE_MANAGER, None, None)
        target_id = target.id

    client = TestClient(app)
    admin_headers = _auth_headers("perm_admin", "StrongPass12!")
    manager_headers = _auth_headers("perm_mgr", "StrongPass12!")

    grant = client.post(
        "/api/v1/permissions/grant",
        json={"target_user_id": str(target_id), "role": "reviewer"},
        headers=admin_headers,
    )
    assert grant.status_code == 200
    binding_id = grant.json()["data"]["binding_id"]

    update = client.patch(
        f"/api/v1/permissions/bindings/{binding_id}",
        json={"role": "store_manager"},
        headers=admin_headers,
    )
    assert update.status_code == 200
    assert update.json()["data"]["role"] == "store_manager"

    revoke = client.post(
        "/api/v1/permissions/revoke",
        json={"target_user_id": str(target_id), "role": "store_manager"},
        headers=admin_headers,
    )
    assert revoke.status_code == 200
    assert revoke.json()["data"]["revoked"] is True

    forbidden = client.post(
        "/api/v1/permissions/grant",
        json={"target_user_id": str(target_id), "role": "reviewer"},
        headers=manager_headers,
    )
    assert forbidden.status_code == 403
    _assert_error_envelope(forbidden.json())
    assert forbidden.json()["code"] == "forbidden"

    with SessionLocal() as db:
        lifecycle = db.scalars(select(PermissionChangeLog).where(PermissionChangeLog.target_user_id == target_id)).all()
        assert len(lifecycle) == 3
        ops = [row.operation for row in lifecycle]
        assert ops == ["grant", "update", "revoke"]
        remaining_bindings = db.scalars(select(UserRoleBinding).where(UserRoleBinding.user_id == target_id, UserRoleBinding.role == RoleType.STORE_MANAGER)).all()
        assert len(remaining_bindings) == 0
        audit_rows = db.scalars(
            select(AuditLog).where(AuditLog.category.in_(["permission_granted", "permission_updated", "permission_revoked"]))
        ).all()
        assert len(audit_rows) == 3


def test_receipt_print_happy_path_unauthorized_and_audit():
    with SessionLocal() as db:
        cashier = create_user(db, "print_cashier", "StrongPass12!", "Print Cashier", RoleType.CASHIER, None, None)
        applicant = create_user(db, "print_applicant", "StrongPass12!", "Print Applicant", RoleType.PROJECT_APPLICANT, None, None)
        product = Product(name="Print Item", barcode="p-print", internal_code="PPRINT", pinyin="print", unit_price=7.5)
        db.add(product)
        db.flush()
        order = Order(
            created_by_user_id=cashier.id,
            status=OrderStatus.SETTLED,
            subtotal_amount=7.5,
            discount_amount=0,
            final_amount=7.5,
            settled_at=utcnow(),
        )
        db.add(order)
        db.flush()
        db.add(OrderLine(order_id=order.id, product_id=product.id, quantity=1, unit_price=7.5, line_discount=0))
        db.commit()
        order_id = order.id

    client = TestClient(app)
    cashier_headers = _auth_headers("print_cashier", "StrongPass12!")
    applicant_headers = _auth_headers("print_applicant", "StrongPass12!")

    print_resp = client.post(f"/api/v1/orders/{order_id}/receipt/print", headers=cashier_headers)
    assert print_resp.status_code == 200
    assert print_resp.json()["data"]["order_id"] == str(order_id)
    assert print_resp.json()["data"]["backend"] == get_settings().receipt_printer_backend

    forbidden = client.post(f"/api/v1/orders/{order_id}/receipt/print", headers=applicant_headers)
    assert forbidden.status_code == 403
    _assert_error_envelope(forbidden.json())
    assert forbidden.json()["code"] == "forbidden"

    with SessionLocal() as db:
        receipt_audits = db.scalars(select(AuditLog).where(AuditLog.category == "receipt_printed")).all()
        assert len(receipt_audits) == 1
        receipt_access = db.scalars(select(AccessLog).where(AccessLog.action == "receipt_print")).all()
        assert len(receipt_access) == 1


def test_cross_user_financial_operations_are_forbidden():
    with SessionLocal() as db:
        owner = create_user(db, "fin_owner", "StrongPass12!", "Fin Owner", RoleType.CASHIER, None, None)
        other = create_user(db, "fin_other", "StrongPass12!", "Fin Other", RoleType.CASHIER, None, None)
        product = Product(name="Fin Item", barcode="fin-1", internal_code="FIN1", pinyin="fin", unit_price=20.0)
        db.add(product)
        db.commit()
        product_id = product.id

    client = TestClient(app)
    owner_headers = _auth_headers("fin_owner", "StrongPass12!")
    other_headers = _auth_headers("fin_other", "StrongPass12!")

    checkout = client.post(
        "/api/v1/orders/checkout",
        json={"customer_name": "Fin", "lines": [{"product_id": str(product_id), "quantity": 1}]},
        headers=owner_headers,
    )
    assert checkout.status_code == 200
    order_id = checkout.json()["data"]["order_id"]

    denied_settle = client.post(
        f"/api/v1/orders/{order_id}/settle",
        json={"payments": [{"method": "cash", "amount": 20.0}]},
        headers=other_headers,
    )
    assert denied_settle.status_code == 403
    assert denied_settle.json()["code"] == "forbidden_order_access"

    settle = client.post(
        f"/api/v1/orders/{order_id}/settle",
        json={"payments": [{"method": "cash", "amount": 20.0}]},
        headers=owner_headers,
    )
    assert settle.status_code == 200

    with SessionLocal() as db:
        order_line_id = db.scalar(select(OrderLine.id).where(OrderLine.order_id == uuid.UUID(order_id)))

    denied_refund = client.post(
        "/api/v1/after-sales/refund",
        json={"order_id": order_id, "reason": "nope", "line_refunds": [{"order_line_id": order_line_id, "quantity": 1, "amount": 5.0}]},
        headers={**other_headers, "Idempotency-Key": "deny-refund"},
    )
    assert denied_refund.status_code == 403
    assert denied_refund.json()["code"] == "forbidden_order_access"

    denied_exchange = client.post(
        "/api/v1/after-sales/exchange",
        json={"order_id": order_id, "reason": "nope", "line_exchanges": [{"order_line_id": order_line_id, "quantity": 1, "amount": 5.0}]},
        headers={**other_headers, "Idempotency-Key": "deny-exchange"},
    )
    assert denied_exchange.status_code == 403
    assert denied_exchange.json()["code"] == "forbidden_order_access"

    denied_reverse = client.post(
        "/api/v1/after-sales/reverse-settlement",
        json={"order_id": order_id, "reason": "nope"},
        headers={**other_headers, "Idempotency-Key": "deny-reverse"},
    )
    assert denied_reverse.status_code == 403
    assert denied_reverse.json()["code"] == "forbidden_order_access"


def test_aftersales_idempotency_scoped_and_fingerprint_validated():
    with SessionLocal() as db:
        c1 = create_user(db, "idem_user_1", "StrongPass12!", "Idem1", RoleType.CASHIER, None, None)
        c2 = create_user(db, "idem_user_2", "StrongPass12!", "Idem2", RoleType.CASHIER, None, None)
        p = Product(name="Idem Item", barcode="idem-1", internal_code="IDEM1", pinyin="idem", unit_price=20.0)
        db.add(p)
        db.flush()

        o1 = Order(created_by_user_id=c1.id, status=OrderStatus.SETTLED, subtotal_amount=20, final_amount=20, discount_amount=0, settled_at=utcnow())
        db.add(o1)
        db.flush()
        l1 = OrderLine(order_id=o1.id, product_id=p.id, quantity=1, unit_price=20, line_discount=0)
        pay1 = PaymentRecord(order_id=o1.id, method=PaymentMethod.CASH, amount=20)
        db.add_all([l1, pay1])

        o2 = Order(created_by_user_id=c2.id, status=OrderStatus.SETTLED, subtotal_amount=20, final_amount=20, discount_amount=0, settled_at=utcnow())
        db.add(o2)
        db.flush()
        l2 = OrderLine(order_id=o2.id, product_id=p.id, quantity=1, unit_price=20, line_discount=0)
        pay2 = PaymentRecord(order_id=o2.id, method=PaymentMethod.CASH, amount=20)
        db.add_all([l2, pay2])
        db.commit()
        order1_id = str(o1.id)
        order2_id = str(o2.id)
        line1_id = l1.id
        line2_id = l2.id

    client = TestClient(app)
    h1 = _auth_headers("idem_user_1", "StrongPass12!")
    h2 = _auth_headers("idem_user_2", "StrongPass12!")

    first = client.post(
        "/api/v1/after-sales/refund",
        json={"order_id": order1_id, "reason": "idem", "line_refunds": [{"order_line_id": line1_id, "quantity": 1, "amount": 5.0}]},
        headers={**h1, "Idempotency-Key": "shared-idem-key"},
    )
    assert first.status_code == 200
    refund_id = first.json()["data"]["refund_id"]

    replay_same = client.post(
        "/api/v1/after-sales/refund",
        json={"order_id": order1_id, "reason": "idem", "line_refunds": [{"order_line_id": line1_id, "quantity": 1, "amount": 5.0}]},
        headers={**h1, "Idempotency-Key": "shared-idem-key"},
    )
    assert replay_same.status_code == 200
    assert replay_same.json()["data"]["refund_id"] == refund_id

    replay_changed = client.post(
        "/api/v1/after-sales/refund",
        json={"order_id": order1_id, "reason": "idem", "line_refunds": [{"order_line_id": line1_id, "quantity": 1, "amount": 6.0}]},
        headers={**h1, "Idempotency-Key": "shared-idem-key"},
    )
    assert replay_changed.status_code == 400
    assert replay_changed.json()["code"] == "refund_failed"
    assert "Idempotency key reuse with different request payload" in replay_changed.json()["message"]

    other_user_same_key = client.post(
        "/api/v1/after-sales/refund",
        json={"order_id": order2_id, "reason": "idem", "line_refunds": [{"order_line_id": line2_id, "quantity": 1, "amount": 5.0}]},
        headers={**h2, "Idempotency-Key": "shared-idem-key"},
    )
    assert other_user_same_key.status_code == 200
    assert other_user_same_key.json()["data"]["refund_id"] != refund_id


def test_refund_and_logs_commit_atomically_on_log_failure(monkeypatch):
    with SessionLocal() as db:
        cashier = create_user(db, "atomic_cashier", "StrongPass12!", "Atomic", RoleType.CASHIER, None, None)
        p = Product(name="Atomic Item", barcode="atomic-1", internal_code="AT1", pinyin="atomic", unit_price=10.0)
        db.add(p)
        db.flush()
        order = Order(created_by_user_id=cashier.id, status=OrderStatus.SETTLED, subtotal_amount=10, final_amount=10, discount_amount=0, settled_at=utcnow())
        db.add(order)
        db.flush()
        line = OrderLine(order_id=order.id, product_id=p.id, quantity=1, unit_price=10, line_discount=0)
        payment = PaymentRecord(order_id=order.id, method=PaymentMethod.CASH, amount=10)
        db.add_all([line, payment])
        db.commit()
        order_id = str(order.id)
        line_id = line.id

    def fail_access_log(*_args, **_kwargs):
        raise RuntimeError("access log write failed")

    monkeypatch.setattr("app.api.v1.routes.write_access_log", fail_access_log)
    client = TestClient(app, raise_server_exceptions=False)
    headers = _auth_headers("atomic_cashier", "StrongPass12!")
    resp = client.post(
        "/api/v1/after-sales/refund",
        json={"order_id": order_id, "reason": "atomic", "line_refunds": [{"order_line_id": line_id, "quantity": 1, "amount": 5.0}]},
        headers={**headers, "Idempotency-Key": "atomic-log-fail"},
    )
    assert resp.status_code == 500

    with SessionLocal() as db:
        refunds = db.scalars(select(AfterSalesOrder).where(AfterSalesOrder.original_order_id == uuid.UUID(order_id))).all()
        assert len(refunds) == 0
        refund_audit = db.scalars(select(AuditLog).where(AuditLog.category == "refund_processed")).all()
        assert len(refund_audit) == 0


def test_duplicate_username_returns_409_username_exists():
    with SessionLocal() as db:
        create_user(db, "admin_dup", "StrongPass12!", "Admin Dup", RoleType.OP_ADMIN, None, None)
    client = TestClient(app)
    headers = _auth_headers("admin_dup", "StrongPass12!")

    first = client.post(
        "/api/v1/auth/users",
        json={"username": "dup_target", "password": "StrongPass12!", "display_name": "Dup Target", "role": "cashier"},
        headers=headers,
    )
    assert first.status_code == 200

    second = client.post(
        "/api/v1/auth/users",
        json={"username": "dup_target", "password": "StrongPass12!", "display_name": "Dup Target 2", "role": "cashier"},
        headers=headers,
    )
    assert second.status_code == 409
    _assert_error_envelope(second.json())
    assert second.json()["code"] == "username_exists"


def test_validation_errors_redact_sensitive_inputs():
    client = TestClient(app)
    resp = client.post("/api/v1/auth/login", json={"username": "any", "password": 12345})
    assert resp.status_code == 422
    _assert_error_envelope(resp.json())
    errors = resp.json()["details"]["errors"]
    password_errors = [e for e in errors if "password" in [str(p).lower() for p in e.get("loc", [])]]
    assert password_errors
    assert all(e.get("input") == "***REDACTED***" for e in password_errors if "input" in e)


def test_login_always_writes_audit_records():
    with SessionLocal() as db:
        user = create_user(db, "audit_login_user", "StrongPass12!", "Audit Login", RoleType.CASHIER, None, None)
        user_id = user.id
    client = TestClient(app)

    failed = client.post("/api/v1/auth/login", json={"username": "audit_login_user", "password": "bad-pass"})
    assert failed.status_code == 401
    ok = client.post("/api/v1/auth/login", json={"username": "audit_login_user", "password": "StrongPass12!"})
    assert ok.status_code == 200

    with SessionLocal() as db:
        categories = [
            row.category
            for row in db.scalars(select(AuditLog).where(AuditLog.user_id == user_id)).all()
            if row.category in {"login_failed", "login_success"}
        ]
    assert "login_failed" in categories
    assert "login_success" in categories


def test_shift_detail_enforces_object_scope_for_non_assignee():
    with SessionLocal() as db:
        mgr = create_user(db, "scope_shift_mgr", "StrongPass12!", "Scope Mgr", RoleType.OP_ADMIN, None, None)
        assignee = create_user(db, "scope_shift_assignee", "StrongPass12!", "Scope A", RoleType.CASHIER, None, None)
        other = create_user(db, "scope_shift_other", "StrongPass12!", "Scope O", RoleType.CASHIER, None, None)
        mgr_id = mgr.id
        assignee_id = assignee.id
        _ = other.id

    client = TestClient(app)
    mgr_headers = _auth_headers("scope_shift_mgr", "StrongPass12!")
    other_headers = _auth_headers("scope_shift_other", "StrongPass12!")
    start = (utcnow() + timedelta(hours=2)).isoformat()
    end = (utcnow() + timedelta(hours=10)).isoformat()
    created = client.post(
        "/api/v1/shifts",
        json={"assigned_user_id": str(assignee_id), "starts_at": start, "ends_at": end, "note": "scope"},
        headers=mgr_headers,
    )
    assert created.status_code == 200
    shift_id = created.json()["data"]["id"]

    denied = client.get(f"/api/v1/shifts/{shift_id}", headers=other_headers)
    assert denied.status_code == 403
    _assert_error_envelope(denied.json())
    assert denied.json()["code"] == "forbidden"

    with SessionLocal() as db:
        row = db.get(ShiftSchedule, uuid.UUID(shift_id))
        assert row is not None
        assert str(row.created_by_user_id) == str(mgr_id)


def test_permission_endpoints_strictly_require_admin():
    with SessionLocal() as db:
        admin = create_user(db, "strict_perm_admin", "StrongPass12!", "Strict Admin", RoleType.OP_ADMIN, None, None)
        manager = create_user(db, "strict_perm_mgr", "StrongPass12!", "Strict Mgr", RoleType.STORE_MANAGER, None, None)
        target = create_user(db, "strict_perm_target", "StrongPass12!", "Strict Target", RoleType.CASHIER, None, None)
        target_id = target.id
        _ = admin.id

    client = TestClient(app)
    admin_headers = _auth_headers("strict_perm_admin", "StrongPass12!")
    mgr_headers = _auth_headers("strict_perm_mgr", "StrongPass12!")

    granted = client.post(
        "/api/v1/permissions/grant",
        json={"target_user_id": str(target_id), "role": "reviewer"},
        headers=admin_headers,
    )
    assert granted.status_code == 200
    binding_id = granted.json()["data"]["binding_id"]

    denied_grant = client.post(
        "/api/v1/permissions/grant",
        json={"target_user_id": str(target_id), "role": "store_manager"},
        headers=mgr_headers,
    )
    denied_revoke = client.post(
        "/api/v1/permissions/revoke",
        json={"target_user_id": str(target_id), "role": "reviewer"},
        headers=mgr_headers,
    )
    denied_update = client.patch(
        f"/api/v1/permissions/bindings/{binding_id}",
        json={"role": "store_manager"},
        headers=mgr_headers,
    )
    for resp in (denied_grant, denied_revoke, denied_update):
        assert resp.status_code == 403
        _assert_error_envelope(resp.json())
        assert resp.json()["code"] == "forbidden"


def test_short_password_rejected_for_create_user():
    with SessionLocal() as db:
        create_user(db, "admin_pw", "StrongPass12!", "Admin PW", RoleType.OP_ADMIN, None, None)
    client = TestClient(app)
    headers = _auth_headers("admin_pw", "StrongPass12!")

    resp = client.post(
        "/api/v1/auth/users",
        json={"username": "too_short_pw", "password": "short", "display_name": "Too Short", "role": "cashier"},
        headers=headers,
    )
    assert resp.status_code == 400
    _assert_error_envelope(resp.json())
    assert resp.json()["code"] == "invalid_password"


def test_split_payment_mismatch_rejected():
    with SessionLocal() as db:
        create_user(db, "split_cashier", "StrongPass12!", "Split Cashier", RoleType.CASHIER, None, None)
        product = Product(name="Split Item", barcode="sp1", internal_code="SP1", pinyin="split", unit_price=10.0)
        db.add(product)
        db.commit()
        product_id = product.id

    client = TestClient(app)
    headers = _auth_headers("split_cashier", "StrongPass12!")
    checkout = client.post(
        "/api/v1/orders/checkout",
        json={"customer_name": "Split", "lines": [{"product_id": str(product_id), "quantity": 1}]},
        headers=headers,
    )
    assert checkout.status_code == 200
    order_id = checkout.json()["data"]["order_id"]

    mismatch = client.post(
        f"/api/v1/orders/{order_id}/settle",
        json={"payments": [{"method": "cash", "amount": 7.0}]},
        headers=headers,
    )
    assert mismatch.status_code == 400
    _assert_error_envelope(mismatch.json())
    assert mismatch.json()["code"] == "settlement_failed"


def test_attachment_max_size_and_tamper_detection(monkeypatch):
    with SessionLocal() as db:
        owner = create_user(db, "attach_guard_owner", "StrongPass12!", "Attach Guard", RoleType.PROJECT_APPLICANT, None, None)
        project = create_project(db, owner.id, "Attach Guard Project", {"goal": "guard"})
        project_id = project.id

    client = TestClient(app)
    headers = _auth_headers("attach_guard_owner", "StrongPass12!")
    monkeypatch.setattr("app.domain.services.settings.attachment_max_mb", 0)

    too_large = client.post(
        f"/api/v1/projects/{project_id}/attachments",
        files={"file": ("large.pdf", b"%PDF-1.7\nx", "application/pdf")},
        headers=headers,
    )
    assert too_large.status_code == 400
    _assert_error_envelope(too_large.json())
    assert too_large.json()["code"] == "attachment_invalid"

    monkeypatch.setattr("app.domain.services.settings.attachment_max_mb", 20)
    uploaded = client.post(
        f"/api/v1/projects/{project_id}/attachments",
        files={"file": ("ok.pdf", b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n", "application/pdf")},
        headers=headers,
    )
    assert uploaded.status_code == 200
    attachment_id = uploaded.json()["data"]["id"]

    from app.models.entities import Attachment

    with SessionLocal() as db:
        row = db.get(Attachment, uuid.UUID(attachment_id))
        assert row is not None
        with open(row.file_path, "wb") as fh:
            fh.write(b"%PDF-1.7\nTAMPERED\n")

    verify = client.get(f"/api/v1/attachments/{attachment_id}/verify", headers=headers)
    assert verify.status_code == 200
    assert verify.json()["data"]["valid"] is False


def test_bootstrap_admin_security_path():
    client = TestClient(app)
    # Disabled bootstrap by default.
    disabled = client.post("/api/v1/auth/seed-admin", json={"username": "seed1", "password": "StrongPass12!"})
    assert disabled.status_code == 403
    _assert_error_envelope(disabled.json())
    assert disabled.json()["code"] == "bootstrap_disabled"

    env_keys = ("BOOTSTRAP_MODE", "INSTALL_BOOTSTRAP_TOKEN")
    old_env = {k: os.environ.get(k) for k in env_keys}
    os.environ["BOOTSTRAP_MODE"] = "true"
    os.environ["INSTALL_BOOTSTRAP_TOKEN"] = "seed-token"
    get_settings.cache_clear()
    try:
        bad_token = client.post(
            "/api/v1/auth/seed-admin",
            json={"username": "seed2", "password": "StrongPass12!"},
            headers={"X-Install-Token": "wrong-token"},
        )
        assert bad_token.status_code == 401
        assert bad_token.json()["code"] == "invalid_install_token"

        ok = client.post(
            "/api/v1/auth/seed-admin",
            json={"username": "seed2", "password": "StrongPass12!"},
            headers={"X-Install-Token": "seed-token"},
        )
        assert ok.status_code == 200
        assert ok.json()["data"]["password_change_required"] is True

        replay = client.post(
            "/api/v1/auth/seed-admin",
            json={"username": "seed3", "password": "StrongPass12!"},
            headers={"X-Install-Token": "seed-token"},
        )
        assert replay.status_code == 400
        assert replay.json()["code"] == "seed_admin_failed"
    finally:
        for key, val in old_env.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val
        get_settings.cache_clear()


def test_exchange_and_reverse_settlement_emit_audit_records():
    with SessionLocal() as db:
        cashier = create_user(db, "audit_as_cashier", "StrongPass12!", "AS Cashier", RoleType.CASHIER, None, None)
        p = Product(name="AS Audit Item", barcode="as-audit-1", internal_code="ASA1", pinyin="asa", unit_price=30.0)
        db.add(p)
        db.flush()
        order = Order(
            created_by_user_id=cashier.id,
            status=OrderStatus.SETTLED,
            subtotal_amount=30,
            final_amount=30,
            discount_amount=0,
            settled_at=utcnow(),
        )
        db.add(order)
        db.flush()
        line = OrderLine(order_id=order.id, product_id=p.id, quantity=1, unit_price=30, line_discount=0)
        payment = PaymentRecord(order_id=order.id, method=PaymentMethod.CASH, amount=30)
        db.add_all([line, payment])
        db.commit()
        order_id = str(order.id)
        line_id = line.id

    client = TestClient(app)
    headers = _auth_headers("audit_as_cashier", "StrongPass12!")
    exchange = client.post(
        "/api/v1/after-sales/exchange",
        json={"order_id": order_id, "reason": "exchange-audit", "line_exchanges": [{"order_line_id": line_id, "quantity": 1, "amount": 30.0}]},
        headers={**headers, "Idempotency-Key": "as-audit-exchange"},
    )
    assert exchange.status_code == 200

    reverse = client.post(
        "/api/v1/after-sales/reverse-settlement",
        json={"order_id": order_id, "reason": "reverse-audit"},
        headers={**headers, "Idempotency-Key": "as-audit-reverse"},
    )
    assert reverse.status_code == 200

    with SessionLocal() as db:
        categories = [row.category for row in db.scalars(select(AuditLog)).all()]
        actions = [row.action for row in db.scalars(select(AccessLog)).all()]
    assert "exchange_processed" in categories
    assert "reverse_settlement_processed" in categories
    assert "after_sales_exchange" in actions
    assert "after_sales_reverse_settlement" in actions
