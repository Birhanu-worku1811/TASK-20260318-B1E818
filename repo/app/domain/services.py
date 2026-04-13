from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.domain.printer import ReceiptPayload, build_printer_adapter
from app.infra.config import get_settings
from app.infra.encryption import encryptor
from app.infra.security import hash_password, is_locked, lock_until, normalize_utc, utcnow, verify_password
from app.domain.events import event_bus
from app.models.entities import (
    AccountingReversal,
    AccessLog,
    AfterSalesLineRefund,
    AfterSalesOrder,
    Attachment,
    AuditLog,
    DailyOperationMetric,
    FeatureDefinition,
    FeatureValueCold,
    FeatureValueHot,
    Notification,
    OperationConfiguration,
    Order,
    OrderLine,
    OrderStatus,
    PaymentMethod,
    PaymentRecord,
    PermissionChangeLog,
    Product,
    Project,
    ProjectStatus,
    ProjectVersion,
    PromotionRule,
    RoleType,
    ShiftSchedule,
    ShiftStatus,
    User,
    UserRoleBinding,
)

settings = get_settings()


class DomainError(Exception):
    pass


class AccountLockedError(DomainError):
    pass


class DuplicateUsernameError(DomainError):
    pass


@dataclass
class LoginResult:
    user: User
    roles: list[str]


def _to_decimal(value: float | Decimal) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"))


def _sum_decimal(values: list[Decimal]) -> Decimal:
    return sum(values, Decimal("0.00"))


def write_audit(db: Session, user_id: Any, category: str, payload: dict[str, Any]) -> None:
    db.add(AuditLog(user_id=user_id, category=category, payload=payload))


def write_access_log(db: Session, user_id: Any, action: str, resource_type: str, resource_id: str | None = None) -> None:
    db.add(AccessLog(user_id=user_id, action=action, resource_type=resource_type, resource_id=resource_id))


def _get_user_roles(db: Session, user_id: Any) -> list[str]:
    bindings = db.scalars(select(UserRoleBinding).where(UserRoleBinding.user_id == user_id)).all()
    return [b.role.value for b in bindings]


def _detect_attachment_type(file_bytes: bytes) -> tuple[str, str] | None:
    if file_bytes.startswith(b"%PDF-"):
        return (".pdf", "application/pdf")
    if file_bytes.startswith(b"\xFF\xD8\xFF"):
        return (".jpg", "image/jpeg")
    if file_bytes.startswith(b"\x89PNG\r\n\x1A\n"):
        return (".png", "image/png")
    return None


def _idempotency_scope(*, operation: str, order_id: Any, actor_user_id: Any, idempotency_key: str) -> str:
    raw = f"{operation}:{order_id}:{actor_user_id}:{idempotency_key}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:120]


def _request_fingerprint(*, operation: str, order_id: Any, reason: str, line_items: list[dict[str, Any]] | None = None) -> str:
    normalized_items = sorted(
        [
            {
                "order_line_id": int(item.get("order_line_id")),
                "quantity": int(item.get("quantity")),
                "amount": float(item.get("amount", 0) or 0),
            }
            for item in (line_items or [])
        ],
        key=lambda x: (x["order_line_id"], x["quantity"], x["amount"]),
    )
    payload = {
        "operation": operation,
        "order_id": str(order_id),
        "reason": reason,
        "line_items": normalized_items,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def seed_admin(db: Session, username: str = "admin", password: str = "TempPass123!") -> User:
    existing_admin = db.scalar(
        select(User)
        .join(UserRoleBinding, UserRoleBinding.user_id == User.id)
        .where(UserRoleBinding.role == RoleType.OP_ADMIN)
    )
    if existing_admin:
        raise DomainError("Admin bootstrap already completed")
    admin = User(
        username=username,
        password_hash=hash_password(password),
        display_name="Operation Admin",
        password_change_required=True,
    )
    db.add(admin)
    db.flush()
    db.add(UserRoleBinding(user_id=admin.id, role=RoleType.OP_ADMIN))
    write_audit(db, admin.id, "seed_admin", {"username": username})
    db.commit()
    db.refresh(admin)
    return admin


def create_user(db: Session, username: str, password: str, display_name: str, role: RoleType, id_number: str | None, contact: str | None) -> User:
    user = User(
        username=username,
        password_hash=hash_password(password),
        display_name=display_name,
        encrypted_id_number=encryptor.encrypt(id_number),
        encrypted_contact=encryptor.encrypt(contact),
    )
    db.add(user)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise DuplicateUsernameError("Username already exists") from exc
    db.add(UserRoleBinding(user_id=user.id, role=role))
    write_audit(db, user.id, "user_created", {"username": username, "role": role.value})
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise DuplicateUsernameError("Username already exists") from exc
    db.refresh(user)
    return user


def create_shift(
    db: Session,
    *,
    assigned_user_id: Any,
    starts_at: datetime,
    ends_at: datetime,
    note: str | None,
    actor_user_id: Any,
) -> ShiftSchedule:
    if starts_at >= ends_at:
        raise DomainError("Shift start must be earlier than end")
    assigned_user = db.get(User, assigned_user_id)
    if not assigned_user:
        raise DomainError("Assigned user not found")
    shift = ShiftSchedule(
        assigned_user_id=assigned_user_id,
        created_by_user_id=actor_user_id,
        starts_at=starts_at,
        ends_at=ends_at,
        status=ShiftStatus.SCHEDULED,
        note=note,
    )
    db.add(shift)
    db.flush()
    write_audit(
        db,
        actor_user_id,
        "shift_created",
        {
            "shift_id": str(shift.id),
            "assigned_user_id": str(assigned_user_id),
            "starts_at": starts_at.isoformat(),
            "ends_at": ends_at.isoformat(),
        },
    )
    db.commit()
    db.refresh(shift)
    return shift


def list_shifts(db: Session, *, assigned_user_id: Any | None = None) -> list[ShiftSchedule]:
    stmt = select(ShiftSchedule).order_by(ShiftSchedule.starts_at.asc())
    if assigned_user_id:
        stmt = stmt.where(ShiftSchedule.assigned_user_id == assigned_user_id)
    return db.scalars(stmt).all()


def get_shift(db: Session, shift_id: Any) -> ShiftSchedule:
    shift = db.get(ShiftSchedule, shift_id)
    if not shift:
        raise DomainError("Shift not found")
    return shift


def update_shift(
    db: Session,
    *,
    shift_id: Any,
    starts_at: datetime | None,
    ends_at: datetime | None,
    note: str | None,
    actor_user_id: Any,
) -> ShiftSchedule:
    shift = get_shift(db, shift_id)
    new_starts = starts_at or shift.starts_at
    new_ends = ends_at or shift.ends_at
    if new_starts >= new_ends:
        raise DomainError("Shift start must be earlier than end")
    if starts_at is not None:
        shift.starts_at = starts_at
    if ends_at is not None:
        shift.ends_at = ends_at
    shift.note = note
    write_audit(
        db,
        actor_user_id,
        "shift_updated",
        {"shift_id": str(shift.id), "starts_at": shift.starts_at.isoformat(), "ends_at": shift.ends_at.isoformat(), "note": shift.note},
    )
    db.commit()
    db.refresh(shift)
    return shift


def update_shift_status(db: Session, *, shift_id: Any, status: ShiftStatus, actor_user_id: Any) -> ShiftSchedule:
    shift = get_shift(db, shift_id)
    allowed: dict[ShiftStatus, set[ShiftStatus]] = {
        ShiftStatus.SCHEDULED: {ShiftStatus.ACTIVE, ShiftStatus.CANCELLED},
        ShiftStatus.ACTIVE: {ShiftStatus.COMPLETED, ShiftStatus.CANCELLED},
        ShiftStatus.COMPLETED: set(),
        ShiftStatus.CANCELLED: set(),
    }
    if status not in allowed[shift.status]:
        raise DomainError(f"Invalid shift transition from {shift.status.value} to {status.value}")
    shift.status = status
    write_audit(db, actor_user_id, "shift_status_changed", {"shift_id": str(shift.id), "status": status.value})
    db.commit()
    db.refresh(shift)
    return shift


def grant_role_binding(db: Session, *, target_user_id: Any, role: RoleType, actor_user_id: Any) -> UserRoleBinding:
    target_user = db.get(User, target_user_id)
    if not target_user:
        raise DomainError("Target user not found")
    binding = UserRoleBinding(user_id=target_user_id, role=role)
    db.add(binding)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise DomainError("Role binding already exists") from exc
    db.add(
        PermissionChangeLog(
            target_user_id=target_user_id,
            actor_user_id=actor_user_id,
            operation="grant",
            from_role=None,
            to_role=role,
        )
    )
    write_audit(
        db,
        actor_user_id,
        "permission_granted",
        {"target_user_id": str(target_user_id), "to_role": role.value},
    )
    db.commit()
    db.refresh(binding)
    return binding


def revoke_role_binding(db: Session, *, target_user_id: Any, role: RoleType, actor_user_id: Any) -> None:
    binding = db.scalar(
        select(UserRoleBinding).where(and_(UserRoleBinding.user_id == target_user_id, UserRoleBinding.role == role))
    )
    if not binding:
        raise DomainError("Role binding not found")
    db.delete(binding)
    db.add(
        PermissionChangeLog(
            target_user_id=target_user_id,
            actor_user_id=actor_user_id,
            operation="revoke",
            from_role=role,
            to_role=None,
        )
    )
    write_audit(
        db,
        actor_user_id,
        "permission_revoked",
        {"target_user_id": str(target_user_id), "from_role": role.value},
    )
    db.commit()


def update_role_binding(db: Session, *, binding_id: int, new_role: RoleType, actor_user_id: Any) -> UserRoleBinding:
    binding = db.get(UserRoleBinding, binding_id)
    if not binding:
        raise DomainError("Role binding not found")
    old_role = binding.role
    if old_role == new_role:
        raise DomainError("Role is unchanged")
    binding.role = new_role
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise DomainError("Role binding already exists for target user") from exc
    db.add(
        PermissionChangeLog(
            target_user_id=binding.user_id,
            actor_user_id=actor_user_id,
            operation="update",
            from_role=old_role,
            to_role=new_role,
        )
    )
    write_audit(
        db,
        actor_user_id,
        "permission_updated",
        {"target_user_id": str(binding.user_id), "from_role": old_role.value, "to_role": new_role.value},
    )
    db.commit()
    db.refresh(binding)
    return binding


def login(db: Session, username: str, password: str) -> LoginResult:
    user = db.scalar(select(User).where(User.username == username))
    if not user:
        raise DomainError("Invalid username or password")
    if is_locked(user.locked_until_at):
        raise AccountLockedError("Account is temporarily locked")
    if not verify_password(password, user.password_hash):
        user.failed_login_attempts += 1
        if user.failed_login_attempts >= settings.failed_login_limit:
            user.locked_until_at = lock_until(settings.account_lock_minutes)
            user.failed_login_attempts = 0
        write_audit(db, user.id, "login_failed", {"username": username})
        db.commit()
        raise DomainError("Invalid username or password")
    user.failed_login_attempts = 0
    user.locked_until_at = None
    write_access_log(db, user.id, "login_success", "auth")
    write_audit(db, user.id, "login_success", {"username": username})
    db.commit()
    db.refresh(user)
    return LoginResult(user=user, roles=_get_user_roles(db, user.id))


def change_password(db: Session, user_id: Any, current_password: str, new_password: str) -> User:
    user = db.get(User, user_id)
    if not user:
        raise DomainError("User not found")
    if not verify_password(current_password, user.password_hash):
        raise DomainError("Current password is incorrect")
    user.password_hash = hash_password(new_password)
    user.password_change_required = False
    write_audit(db, user.id, "password_changed", {"user_id": str(user.id)})
    db.commit()
    db.refresh(user)
    return user


def product_search(db: Session, q: str) -> list[Product]:
    qn = q.strip().lower()
    stmt = select(Product).where(
        or_(
            func.lower(Product.barcode) == qn,
            func.lower(Product.internal_code).like(f"{qn}%"),
            func.lower(Product.pinyin).like(f"{qn}%"),
        )
    )
    return db.scalars(stmt.limit(20)).all()


def _best_tier_discount(tiers: list[dict[str, Any]], runtime_base: Decimal) -> Decimal:
    best = Decimal("0")
    for tier in tiers:
        threshold = _to_decimal(tier.get("threshold", 0))
        discount = _to_decimal(tier.get("discount", 0))
        if runtime_base >= threshold and discount > best:
            best = discount
    return best


def _apply_purchase_limit(lines: list[dict[str, Any]], rules: list[PromotionRule]) -> None:
    for r in rules:
        if r.rule_type != "purchase_limit":
            continue
        product_id = r.config.get("product_id")
        max_qty = int(r.config.get("max_qty", 0))
        if not product_id or max_qty <= 0:
            continue
        qty = sum(int(l["quantity"]) for l in lines if str(l["product_id"]) == str(product_id))
        if qty > max_qty:
            raise DomainError(f"Purchase limit exceeded for product {product_id}. Max allowed: {max_qty}")


def calculate_cart(db: Session, lines: list[dict[str, Any]]) -> dict[str, Any]:
    if not lines:
        raise DomainError("Cart cannot be empty")
    products = {}
    normalized_lines: list[dict[str, Any]] = []
    for raw in lines:
        product_id = uuid.UUID(str(raw["product_id"]))
        quantity = int(raw["quantity"])
        if quantity <= 0:
            raise DomainError("Line quantity must be greater than zero")
        product = db.get(Product, product_id)
        if not product:
            raise DomainError(f"Product {product_id} not found")
        products[product_id] = product
        normalized_lines.append(
            {
                "product_id": product_id,
                "quantity": quantity,
                "unit_price": _to_decimal(product.unit_price),
                "line_subtotal": _to_decimal(product.unit_price) * quantity,
                "line_discount": Decimal("0.00"),
            }
        )

    rules = db.scalars(select(PromotionRule).where(PromotionRule.is_active.is_(True))).all()
    _apply_purchase_limit(normalized_lines, rules)

    item_rules = [r for r in rules if r.scope == "item"]
    order_rules = [r for r in rules if r.scope == "order"]
    global_rules = [r for r in rules if r.scope == "global"]

    for line in normalized_lines:
        for rule in item_rules:
            cfg = rule.config or {}
            if str(cfg.get("product_id", line["product_id"])) != str(line["product_id"]):
                continue
            if rule.rule_type == "buy_and_get":
                buy_qty = int(cfg.get("buy_qty", 0))
                get_qty = int(cfg.get("get_qty", 0))
                if buy_qty > 0 and get_qty > 0 and line["quantity"] >= buy_qty:
                    free_units = (line["quantity"] // buy_qty) * get_qty
                    line["line_discount"] += _to_decimal(line["unit_price"]) * free_units
            elif rule.rule_type == "tiered_pricing":
                tiers = cfg.get("tiers", [])
                # Item-tier runtime base is quantity.
                line["line_discount"] += _best_tier_discount(tiers, _to_decimal(line["quantity"]))
            elif rule.rule_type == "spend_and_save":
                threshold = _to_decimal(cfg.get("threshold", 0))
                discount = _to_decimal(cfg.get("discount", 0))
                if line["line_subtotal"] >= threshold:
                    line["line_discount"] += discount

    subtotal = _sum_decimal([l["line_subtotal"] for l in normalized_lines])
    item_discount = _sum_decimal([l["line_discount"] for l in normalized_lines])
    order_discount = Decimal("0.00")

    for rule in order_rules + global_rules:
        cfg = rule.config or {}
        if rule.rule_type == "spend_and_save":
            threshold = _to_decimal(cfg.get("threshold", 0))
            discount = _to_decimal(cfg.get("discount", 0))
            if subtotal >= threshold:
                order_discount += discount
        elif rule.rule_type == "tiered_pricing":
            tiers = cfg.get("tiers", [])
            # Order-tier runtime base is order subtotal.
            order_discount += _best_tier_discount(tiers, subtotal)
        elif rule.rule_type == "buy_and_get":
            order_discount += _to_decimal(cfg.get("flat_discount", 0))

    discount_total = min(subtotal, item_discount + order_discount)
    final_total = max(Decimal("0.00"), subtotal - discount_total)
    return {
        "lines": [
            {
                "product_id": str(l["product_id"]),
                "quantity": l["quantity"],
                "unit_price": float(l["unit_price"]),
                "line_subtotal": float(l["line_subtotal"]),
                "line_discount": float(l["line_discount"]),
            }
            for l in normalized_lines
        ],
        "subtotal": float(subtotal),
        "discount_total": float(discount_total),
        "final_total": float(final_total),
    }


def checkout_order(db: Session, user_id: Any, customer_name: str | None, lines: list[dict[str, Any]]) -> Order:
    calc = calculate_cart(db, lines)
    order = Order(created_by_user_id=user_id, customer_name=customer_name)
    db.add(order)
    db.flush()
    for line in calc["lines"]:
        db.add(
            OrderLine(
                order_id=order.id,
                product_id=uuid.UUID(line["product_id"]),
                quantity=int(line["quantity"]),
                unit_price=_to_decimal(line["unit_price"]),
                line_discount=_to_decimal(line["line_discount"]),
            )
        )
    order.subtotal_amount = _to_decimal(calc["subtotal"])
    order.discount_amount = _to_decimal(calc["discount_total"])
    order.final_amount = _to_decimal(calc["final_total"])
    write_audit(db, user_id, "checkout_created", {"order_id": str(order.id)})
    db.commit()
    db.refresh(order)
    return order


def settle_order(db: Session, order_id: Any, payments: list[dict[str, Any]], user_id: Any) -> Order:
    order = db.get(Order, order_id)
    if not order:
        raise DomainError("Order not found")
    if order.status != OrderStatus.DRAFT:
        raise DomainError("Order cannot be settled")
    paid = _sum_decimal([_to_decimal(p["amount"]) for p in payments])
    if paid != _to_decimal(order.final_amount):
        raise DomainError("Total payment must equal final order amount")
    for p in payments:
        db.add(
            PaymentRecord(
                order_id=order.id,
                method=PaymentMethod(p["method"]),
                amount=_to_decimal(p["amount"]),
                reference=p.get("reference"),
            )
        )
    order.status = OrderStatus.SETTLED
    order.settled_at = utcnow()
    write_audit(db, user_id, "order_settled", {"order_id": str(order.id)})
    db.flush()
    return order


def print_receipt_for_order(db: Session, *, order_id: Any, actor_user_id: Any) -> dict[str, Any]:
    order = db.get(Order, order_id)
    if not order:
        raise DomainError("Order not found")
    if order.status != OrderStatus.SETTLED:
        raise DomainError("Receipt can only be printed for settled orders")
    order_lines = db.scalars(select(OrderLine).where(OrderLine.order_id == order.id)).all()
    lines = []
    for row in order_lines:
        product = db.get(Product, row.product_id)
        product_name = product.name if product else str(row.product_id)
        lines.append(f"{product_name} x{row.quantity} @ {float(row.unit_price):.2f}")
    payload = ReceiptPayload(order_id=str(order.id), lines=lines, total=float(order.final_amount))
    adapter = build_printer_adapter(settings)
    adapter.print_receipt(payload)
    write_audit(db, actor_user_id, "receipt_printed", {"order_id": str(order.id), "backend": settings.receipt_printer_backend})
    db.commit()
    return {"order_id": str(order.id), "backend": settings.receipt_printer_backend, "line_count": len(lines)}


def auto_void_unsettled_orders(db: Session) -> int:
    threshold = utcnow() - timedelta(minutes=settings.order_auto_void_minutes)
    orders = db.scalars(select(Order).where(and_(Order.status == OrderStatus.DRAFT, Order.created_at < threshold))).all()
    for o in orders:
        o.status = OrderStatus.VOID
    db.commit()
    return len(orders)


def process_refund(
    db: Session,
    *,
    order_id: Any,
    reason: str,
    idempotency_key: str,
    user_id: Any,
    line_refunds: list[dict[str, Any]],
) -> AfterSalesOrder:
    scope = _idempotency_scope(operation="refund", order_id=order_id, actor_user_id=user_id, idempotency_key=idempotency_key)
    fingerprint = _request_fingerprint(operation="refund", order_id=order_id, reason=reason, line_items=line_refunds)
    existing = db.scalar(select(AfterSalesOrder).where(AfterSalesOrder.idempotency_scope == scope))
    if existing:
        if existing.request_fingerprint != fingerprint:
            raise DomainError("Idempotency key reuse with different request payload")
        return existing
    order = db.get(Order, order_id)
    if not order or order.status != OrderStatus.SETTLED:
        raise DomainError("Original settled order is required")
    settled_at = normalize_utc(order.settled_at)
    if not settled_at or settled_at < utcnow() - timedelta(days=7):
        raise DomainError("Returns are only allowed within 7 days")
    if not line_refunds:
        raise DomainError("Refund must include line_refunds")

    existing_refund_rows = db.scalars(
        select(AfterSalesLineRefund)
        .join(AfterSalesOrder, AfterSalesLineRefund.after_sales_order_id == AfterSalesOrder.id)
        .where(AfterSalesOrder.original_order_id == order_id)
    ).all()
    cumulative_by_line: dict[int, dict[str, Decimal]] = {}
    for row in existing_refund_rows:
        stats = cumulative_by_line.setdefault(row.order_line_id, {"qty": Decimal("0"), "amount": Decimal("0")})
        stats["qty"] += Decimal(row.quantity)
        stats["amount"] += _to_decimal(row.amount)

    req_total = Decimal("0.00")
    prepared_rows: list[dict[str, Any]] = []
    for item in line_refunds:
        line = db.get(OrderLine, int(item["order_line_id"]))
        if not line or line.order_id != order.id:
            raise DomainError("Refund line must belong to original order")
        qty = int(item["quantity"])
        amount = _to_decimal(item["amount"])
        if qty <= 0 or amount <= 0:
            raise DomainError("Refund quantity and amount must be positive")
        already = cumulative_by_line.get(line.id, {"qty": Decimal("0"), "amount": Decimal("0")})
        if already["qty"] + qty > line.quantity:
            raise DomainError(f"Line {line.id} cumulative refunded quantity exceeds purchased quantity")
        line_cap = _to_decimal(line.unit_price) * line.quantity - _to_decimal(line.line_discount)
        if already["amount"] + amount > line_cap:
            raise DomainError(f"Line {line.id} cumulative refunded amount exceeds line cap")
        req_total += amount
        prepared_rows.append({"order_line_id": line.id, "quantity": qty, "amount": amount})

    order_refunded_total = db.scalar(
        select(func.coalesce(func.sum(AfterSalesOrder.amount), 0)).where(AfterSalesOrder.original_order_id == order_id)
    )
    if _to_decimal(order_refunded_total) + req_total > _to_decimal(order.final_amount):
        raise DomainError("Cumulative refunds exceed original order amount")

    refund = AfterSalesOrder(
        original_order_id=order_id,
        type="refund",
        reason=reason,
        amount=req_total,
        idempotency_key=idempotency_key,
        idempotency_scope=scope,
        request_fingerprint=fingerprint,
        created_by_user_id=user_id,
    )
    db.add(refund)
    db.flush()
    for row in prepared_rows:
        db.add(
            AfterSalesLineRefund(
                after_sales_order_id=refund.id,
                order_line_id=row["order_line_id"],
                quantity=row["quantity"],
                amount=row["amount"],
            )
        )
    write_audit(db, user_id, "refund_processed", {"order_id": str(order_id), "refund_id": str(refund.id), "amount": str(req_total)})
    db.flush()
    return refund


def process_exchange(
    db: Session,
    *,
    order_id: Any,
    reason: str,
    idempotency_key: str,
    user_id: Any,
    line_exchanges: list[dict[str, Any]],
) -> AfterSalesOrder:
    scope = _idempotency_scope(operation="exchange", order_id=order_id, actor_user_id=user_id, idempotency_key=idempotency_key)
    fingerprint = _request_fingerprint(operation="exchange", order_id=order_id, reason=reason, line_items=line_exchanges)
    existing = db.scalar(select(AfterSalesOrder).where(AfterSalesOrder.idempotency_scope == scope))
    if existing:
        if existing.request_fingerprint != fingerprint:
            raise DomainError("Idempotency key reuse with different request payload")
        return existing
    order = db.get(Order, order_id)
    if not order or order.status != OrderStatus.SETTLED:
        raise DomainError("Original settled order is required")
    settled_at = normalize_utc(order.settled_at)
    if not settled_at or settled_at < utcnow() - timedelta(days=7):
        raise DomainError("Exchanges are only allowed within 7 days")
    if not line_exchanges:
        raise DomainError("Exchange must include line_exchanges")

    total_amount = Decimal("0.00")
    for item in line_exchanges:
        line = db.get(OrderLine, int(item["order_line_id"]))
        if not line or line.order_id != order.id:
            raise DomainError("Exchange line must belong to original order")
        qty = int(item["quantity"])
        if qty <= 0 or qty > line.quantity:
            raise DomainError("Invalid exchange quantity")
        line_amount = _to_decimal(line.unit_price) * qty
        total_amount += line_amount

    exchange = AfterSalesOrder(
        original_order_id=order_id,
        type="exchange",
        reason=reason,
        amount=total_amount,
        idempotency_key=idempotency_key,
        idempotency_scope=scope,
        request_fingerprint=fingerprint,
        created_by_user_id=user_id,
    )
    db.add(exchange)
    db.flush()
    for item in line_exchanges:
        db.add(
            AfterSalesLineRefund(
                after_sales_order_id=exchange.id,
                order_line_id=int(item["order_line_id"]),
                quantity=int(item["quantity"]),
                amount=_to_decimal(item.get("amount", 0) or 0) or _to_decimal(total_amount / max(1, len(line_exchanges))),
            )
        )
    write_audit(db, user_id, "exchange_processed", {"order_id": str(order_id), "after_sales_id": str(exchange.id)})
    db.flush()
    return exchange


def process_reverse_settlement(
    db: Session,
    *,
    order_id: Any,
    reason: str,
    idempotency_key: str,
    user_id: Any,
) -> AfterSalesOrder:
    scope = _idempotency_scope(
        operation="reverse_settlement",
        order_id=order_id,
        actor_user_id=user_id,
        idempotency_key=idempotency_key,
    )
    fingerprint = _request_fingerprint(operation="reverse_settlement", order_id=order_id, reason=reason, line_items=[])
    existing = db.scalar(select(AfterSalesOrder).where(AfterSalesOrder.idempotency_scope == scope))
    if existing:
        if existing.request_fingerprint != fingerprint:
            raise DomainError("Idempotency key reuse with different request payload")
        return existing
    order = db.get(Order, order_id)
    if not order or order.status != OrderStatus.SETTLED:
        raise DomainError("Original settled order is required")
    reverse_amount = _to_decimal(order.final_amount)
    reverse = AfterSalesOrder(
        original_order_id=order_id,
        type="reverse_settlement",
        reason=reason,
        amount=reverse_amount,
        idempotency_key=idempotency_key,
        idempotency_scope=scope,
        request_fingerprint=fingerprint,
        created_by_user_id=user_id,
    )
    db.add(reverse)
    db.flush()
    payments = db.scalars(select(PaymentRecord).where(PaymentRecord.order_id == order_id)).all()
    for p in payments:
        db.add(
            AccountingReversal(
                after_sales_order_id=reverse.id,
                original_payment_record_id=p.id,
                method=p.method,
                amount=_to_decimal(p.amount),
            )
        )
    write_audit(db, user_id, "reverse_settlement_processed", {"order_id": str(order_id), "after_sales_id": str(reverse.id)})
    db.flush()
    return reverse


def create_project(db: Session, applicant_user_id: Any, title: str, content: dict[str, Any]) -> Project:
    project = Project(applicant_user_id=applicant_user_id, title=title, status=ProjectStatus.DRAFT)
    db.add(project)
    db.flush()
    db.add(ProjectVersion(project_id=project.id, version_no=1, content=content, diff_summary='{"type":"initial"}'))
    project.current_version_no = 1
    db.commit()
    db.refresh(project)
    return project


def _dict_diff(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    keys = sorted(set(old.keys()) | set(new.keys()))
    diff: dict[str, Any] = {}
    for key in keys:
        if old.get(key) != new.get(key):
            diff[key] = {"old": old.get(key), "new": new.get(key)}
    return diff


def submit_project(db: Session, project_id: Any, content: dict[str, Any], user_id: Any) -> Project:
    project = db.get(Project, project_id)
    if not project:
        raise DomainError("Project not found")
    if project.status not in {ProjectStatus.DRAFT, ProjectStatus.REJECTED}:
        raise DomainError("Project can only be submitted from draft or rejected state")
    latest = db.scalar(select(ProjectVersion).where(ProjectVersion.project_id == project_id).order_by(ProjectVersion.version_no.desc()).limit(1))
    next_version = (latest.version_no if latest else 0) + 1
    previous_content = latest.content if latest else {}
    diff = _dict_diff(previous_content, content)
    db.add(ProjectVersion(project_id=project_id, version_no=next_version, content=content, diff_summary=str(diff)))
    project.current_version_no = next_version
    project.status = ProjectStatus.SUBMITTED
    write_audit(db, user_id, "project_submitted", {"project_id": str(project_id), "version": next_version, "changed_fields": list(diff.keys())})
    db.commit()
    db.refresh(project)
    event_bus.publish(
        "project_submitted",
        {
            "event_type": "pending_approval",
            "object_id": str(project_id),
            "actor_user_id": str(user_id),
            "message": f"Project {project_id} submitted and pending approval",
        },
    )
    return project


def update_project_status(db: Session, project_id: Any, status: ProjectStatus, user_id: Any) -> Project:
    project = db.get(Project, project_id)
    if not project:
        raise DomainError("Project not found")
    allowed: dict[ProjectStatus, set[ProjectStatus]] = {
        ProjectStatus.DRAFT: {ProjectStatus.SUBMITTED, ProjectStatus.DEACTIVATED},
        ProjectStatus.SUBMITTED: {ProjectStatus.UNDER_REVIEW, ProjectStatus.REJECTED, ProjectStatus.DEACTIVATED},
        ProjectStatus.UNDER_REVIEW: {ProjectStatus.APPROVED, ProjectStatus.REJECTED},
        ProjectStatus.REJECTED: {ProjectStatus.SUBMITTED, ProjectStatus.DEACTIVATED},
        ProjectStatus.APPROVED: {ProjectStatus.DEACTIVATED},
        ProjectStatus.DEACTIVATED: set(),
    }
    if status not in allowed[project.status]:
        raise DomainError(f"Invalid state transition from {project.status.value} to {status.value}")
    project.status = status
    write_audit(db, user_id, "project_status_changed", {"project_id": str(project_id), "status": status.value})
    db.commit()
    db.refresh(project)
    return project


def get_project_diff(db: Session, project_id: Any, from_version: int, to_version: int) -> dict[str, Any]:
    old = db.scalar(select(ProjectVersion).where(and_(ProjectVersion.project_id == project_id, ProjectVersion.version_no == from_version)))
    new = db.scalar(select(ProjectVersion).where(and_(ProjectVersion.project_id == project_id, ProjectVersion.version_no == to_version)))
    if not old or not new:
        raise DomainError("Project versions not found")
    return _dict_diff(old.content, new.content)


def save_attachment(db: Session, *, project_id: Any, filename: str, mime_type: str, file_bytes: bytes, base_path: str = "./storage") -> Attachment:
    normalized_ext = Path(filename).suffix.lower()
    if normalized_ext == ".jpeg":
        normalized_ext = ".jpg"
    ext_to_mime = {
        ".pdf": "application/pdf",
        ".jpg": "image/jpeg",
        ".png": "image/png",
    }
    if normalized_ext not in ext_to_mime:
        raise DomainError("Unsupported file extension")
    detected = _detect_attachment_type(file_bytes)
    if not detected:
        raise DomainError("Unsupported file signature")
    detected_ext, detected_mime = detected
    if normalized_ext != detected_ext:
        raise DomainError("File extension and signature do not match")
    if mime_type and mime_type != "application/octet-stream" and mime_type != detected_mime:
        raise DomainError("MIME type does not match file signature")
    max_bytes = settings.attachment_max_mb * 1024 * 1024
    if len(file_bytes) > max_bytes:
        raise DomainError("Attachment too large")
    fingerprint = hashlib.sha256(file_bytes).hexdigest()
    os.makedirs(base_path, exist_ok=True)
    path = f"{base_path}/{fingerprint}_{filename}"
    with open(path, "wb") as f:
        f.write(file_bytes)
    attachment = Attachment(
        project_id=project_id,
        filename=filename,
        mime_type=detected_mime,
        size_bytes=len(file_bytes),
        sha256_fingerprint=fingerprint,
        file_path=path,
    )
    db.add(attachment)
    db.commit()
    db.refresh(attachment)
    return attachment


def verify_attachment_integrity(attachment: Attachment) -> bool:
    with open(attachment.file_path, "rb") as f:
        current = hashlib.sha256(f.read()).hexdigest()
    return current == attachment.sha256_fingerprint


@dataclass
class NotificationEvent:
    event_type: str
    object_id: str
    recipient_user_id: Any
    message: str


def push_notification(db: Session, event: NotificationEvent) -> Notification | None:
    bucket_at = utcnow().replace(second=0, microsecond=0)
    bucket_window = bucket_at - timedelta(minutes=bucket_at.minute % 10)
    throttle_key = f"{event.event_type}:{event.object_id}:{event.recipient_user_id}:{bucket_window.isoformat()}"
    note = Notification(
        recipient_user_id=event.recipient_user_id,
        event_type=event.event_type,
        object_id=event.object_id,
        throttle_bucket=throttle_key,
        message=event.message,
        delivered_at=utcnow(),
    )
    db.add(note)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return None
    db.refresh(note)
    return note


def list_notifications(db: Session, user_id: Any) -> list[Notification]:
    return db.scalars(select(Notification).where(Notification.recipient_user_id == user_id).order_by(Notification.created_at.desc())).all()


def mark_notification_read(db: Session, notification_id: Any) -> Notification:
    note = db.get(Notification, notification_id)
    if not note:
        raise DomainError("Notification not found")
    return note


def mark_notification_read_for_user(db: Session, notification_id: Any, user_id: Any) -> Notification:
    note = mark_notification_read(db, notification_id)
    if note.recipient_user_id != user_id:
        raise DomainError("Cannot read notifications that do not belong to you")
    note.read_at = utcnow()
    db.commit()
    db.refresh(note)
    return note


def upsert_feature_value(db: Session, *, feature_id: Any, entity_key: str, value: float) -> FeatureValueHot:
    feature = db.get(FeatureDefinition, feature_id)
    if not feature:
        raise DomainError("Feature definition not found")
    expires = utcnow() + timedelta(seconds=feature.ttl_seconds)
    h = hashlib.sha256(f"{feature_id}:{entity_key}:{value}".encode("utf-8")).hexdigest()
    existing = db.scalar(select(FeatureValueHot).where(and_(FeatureValueHot.feature_id == feature_id, FeatureValueHot.entity_key == entity_key)))
    if existing:
        existing.value = value
        existing.expires_at = expires
        existing.consistency_hash = h
        db.commit()
        db.refresh(existing)
        return existing
    record = FeatureValueHot(feature_id=feature_id, entity_key=entity_key, value=value, expires_at=expires, consistency_hash=h)
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


def _compute_sliding_window(payload: dict[str, Any]) -> float:
    values = [float(v) for v in payload.get("values", [])]
    window_size = int(payload.get("window_size", 1))
    if not values or window_size <= 0:
        return 0.0
    sample = values[-window_size:]
    return float(sum(sample) / len(sample))


def _compute_frequency(payload: dict[str, Any]) -> float:
    events = payload.get("events", [])
    if isinstance(events, list):
        return float(len(events))
    return float(payload.get("count", 0))


def _compute_correlation(payload: dict[str, Any]) -> float:
    a = [float(x) for x in payload.get("series_a", [])]
    b = [float(x) for x in payload.get("series_b", [])]
    if len(a) != len(b) or len(a) < 2:
        return 0.0
    mean_a = sum(a) / len(a)
    mean_b = sum(b) / len(b)
    cov = sum((x - mean_a) * (y - mean_b) for x, y in zip(a, b))
    var_a = sum((x - mean_a) ** 2 for x in a)
    var_b = sum((y - mean_b) ** 2 for y in b)
    if var_a == 0 or var_b == 0:
        return 0.0
    return float(cov / (var_a**0.5 * var_b**0.5))


def compute_feature_value(
    db: Session,
    *,
    feature_id: Any,
    entity_key: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    feature = db.get(FeatureDefinition, feature_id)
    if not feature:
        raise DomainError("Feature definition not found")
    if feature.calculation_type == "window":
        computed = _compute_sliding_window(payload)
    elif feature.calculation_type == "frequency":
        computed = _compute_frequency(payload)
    elif feature.calculation_type == "correlation":
        computed = _compute_correlation(payload)
    else:
        raise DomainError(f"Unsupported calculation type {feature.calculation_type}")

    hot = upsert_feature_value(db, feature_id=feature_id, entity_key=entity_key, value=computed)
    lineage = {
        "feature": feature.name,
        "calculation_type": feature.calculation_type,
        "payload_keys": sorted(payload.keys()),
        "computed_at": utcnow().isoformat(),
        "consistency_hash": hot.consistency_hash,
    }
    return {"value": computed, "lineage": lineage, "consistent": verify_feature_consistency(db, feature_id, entity_key)}


def compact_feature_values(db: Session) -> int:
    expired = db.scalars(select(FeatureValueHot).where(FeatureValueHot.expires_at < utcnow())).all()
    for row in expired:
        db.add(FeatureValueCold(feature_id=row.feature_id, entity_key=row.entity_key, value=row.value, lineage_ref=f"hot:{row.id}"))
        db.delete(row)
    db.commit()
    return len(expired)


def verify_feature_consistency(db: Session, feature_id: Any, entity_key: str) -> bool:
    row = db.scalar(select(FeatureValueHot).where(and_(FeatureValueHot.feature_id == feature_id, FeatureValueHot.entity_key == entity_key)))
    if not row:
        return False
    current = hashlib.sha256(f"{feature_id}:{entity_key}:{row.value}".encode("utf-8")).hexdigest()
    return current == row.consistency_hash


def set_operation_config(db: Session, config_key: str, payload: dict[str, Any], rollout_percent: int, user_id: Any) -> OperationConfiguration:
    latest_version = db.scalar(select(func.max(OperationConfiguration.version)).where(OperationConfiguration.config_key == config_key))
    next_version = (latest_version or 0) + 1
    record = OperationConfiguration(
        config_key=config_key,
        version=next_version,
        payload=payload,
        rollout_percent=rollout_percent,
        is_active=True,
        created_by_user_id=user_id,
    )
    db.execute(OperationConfiguration.__table__.update().where(and_(OperationConfiguration.config_key == config_key, OperationConfiguration.is_active.is_(True))).values(is_active=False))
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


def rollback_operation_config(db: Session, config_key: str, version: int) -> OperationConfiguration:
    target = db.scalar(select(OperationConfiguration).where(and_(OperationConfiguration.config_key == config_key, OperationConfiguration.version == version)))
    if not target:
        raise DomainError("Target version not found")
    db.execute(OperationConfiguration.__table__.update().where(OperationConfiguration.config_key == config_key).values(is_active=False))
    target.is_active = True
    db.commit()
    db.refresh(target)
    return target


def aggregate_daily_metrics(db: Session, day: datetime | None = None) -> DailyOperationMetric:
    start = (day or utcnow()).replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    total = db.scalar(select(func.count()).select_from(Order).where(and_(Order.status == OrderStatus.SETTLED, Order.created_at >= start, Order.created_at < end)))
    disputes = db.scalar(select(func.count()).select_from(AfterSalesOrder).where(and_(AfterSalesOrder.created_at >= start, AfterSalesOrder.created_at < end)))
    metric = db.scalar(select(DailyOperationMetric).where(DailyOperationMetric.date == start))
    if not metric:
        metric = DailyOperationMetric(date=start)
        db.add(metric)
    metric.transaction_volume = int(total or 0)
    metric.conversion_rate = 1.0 if total else 0.0
    metric.activity_score = float(total or 0)
    metric.dispute_rate = float((disputes or 0) / max(1, total or 0))
    db.commit()
    db.refresh(metric)
    return metric


def query_daily_metrics(db: Session, start_date: datetime, end_date: datetime) -> list[DailyOperationMetric]:
    return db.scalars(
        select(DailyOperationMetric)
        .where(and_(DailyOperationMetric.date >= start_date, DailyOperationMetric.date <= end_date))
        .order_by(DailyOperationMetric.date.asc())
    ).all()


def export_daily_metrics_csv(db: Session, start_date: datetime | None = None, end_date: datetime | None = None) -> str:
    stmt = select(DailyOperationMetric).order_by(DailyOperationMetric.date.desc())
    if start_date:
        stmt = stmt.where(DailyOperationMetric.date >= start_date)
    if end_date:
        stmt = stmt.where(DailyOperationMetric.date <= end_date)
    rows = db.scalars(stmt).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["date", "transaction_volume", "conversion_rate", "activity_score", "dispute_rate"])
    for r in rows:
        writer.writerow([r.date.isoformat(), r.transaction_volume, r.conversion_rate, r.activity_score, r.dispute_rate])
    return output.getvalue()
