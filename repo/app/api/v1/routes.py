from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Header, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.services import (
    change_password,
    AccountLockedError,
    DuplicateUsernameError,
    DomainError,
    NotificationEvent,
    aggregate_daily_metrics,
    auto_void_unsettled_orders,
    calculate_cart,
    checkout_order,
    compact_feature_values,
    create_project,
    create_shift,
    create_user,
    export_daily_metrics_csv,
    get_shift,
    get_project_diff,
    grant_role_binding,
    list_notifications,
    list_shifts,
    login,
    mark_notification_read_for_user,
    process_exchange,
    process_refund,
    process_reverse_settlement,
    compute_feature_value,
    product_search,
    push_notification,
    query_daily_metrics,
    revoke_role_binding,
    rollback_operation_config,
    save_attachment,
    seed_admin,
    set_operation_config,
    settle_order,
    submit_project,
    print_receipt_for_order,
    update_project_status,
    update_role_binding,
    update_shift,
    update_shift_status,
    upsert_feature_value,
    verify_attachment_integrity,
    verify_feature_consistency,
    write_access_log,
)
from app.domain.events import event_bus
from app.infra.auth import get_current_user, get_current_user_allow_password_change, require_roles, resolve_user_from_token
from app.infra.config import get_settings
from app.infra.db import SessionLocal, get_db
from app.infra.response import APIError, success
from app.infra.security import create_access_token
from app.infra.ws import ws_hub
from app.models.entities import (
    Attachment,
    FeatureDefinition,
    Order,
    Project,
    Product,
    ProjectStatus,
    PromotionRule,
    RoleType,
    ShiftStatus,
    User,
    UserRoleBinding,
)

router = APIRouter(prefix="/api/v1")


def _current_user_roles(db: Session, user_id: uuid.UUID) -> set[str]:
    return {r.role.value for r in db.scalars(select(UserRoleBinding).where(UserRoleBinding.user_id == user_id)).all()}


def _assert_project_access(db: Session, current_user: User, project_id: uuid.UUID) -> Project:
    project = db.get(Project, project_id)
    if not project:
        raise APIError(status_code=404, code="project_not_found", message="Project not found")
    if project.applicant_user_id == current_user.id:
        return project
    roles = _current_user_roles(db, current_user.id)
    settings = get_settings()
    if settings.allow_admin_reviewer_project_override and roles.intersection({RoleType.OP_ADMIN.value, RoleType.REVIEWER.value}):
        return project
    raise APIError(status_code=403, code="forbidden_project_access", message="Project access denied")


def _assert_order_financial_access(db: Session, current_user: User, order_id: uuid.UUID) -> Order:
    order = db.get(Order, order_id)
    if not order:
        raise APIError(status_code=404, code="order_not_found", message="Order not found")
    roles = _current_user_roles(db, current_user.id)
    if order.created_by_user_id == current_user.id:
        return order
    if RoleType.OP_ADMIN.value in roles:
        return order
    raise APIError(status_code=403, code="forbidden_order_access", message="Order financial access denied")


class UserCreateReq(BaseModel):
    username: str
    password: str
    display_name: str
    role: RoleType
    id_number: str | None = None
    contact: str | None = None


class LoginReq(BaseModel):
    username: str
    password: str


class SeedAdminReq(BaseModel):
    username: str = "admin"
    password: str


class ChangePasswordReq(BaseModel):
    current_password: str
    new_password: str


class ProductCreateReq(BaseModel):
    name: str
    barcode: str
    internal_code: str
    pinyin: str
    unit_price: float


class PromotionRuleReq(BaseModel):
    name: str
    scope: str = Field(pattern="^(item|order|global)$")
    rule_type: str
    config: dict[str, Any] = Field(default_factory=dict)


class CartLineReq(BaseModel):
    product_id: uuid.UUID
    quantity: int = Field(ge=1)


class CartCalculateReq(BaseModel):
    lines: list[CartLineReq]


class CheckoutReq(BaseModel):
    customer_name: str | None = None
    lines: list[CartLineReq]


class SettlementReq(BaseModel):
    payments: list[dict[str, Any]]


class RefundLineReq(BaseModel):
    order_line_id: int
    quantity: int = Field(ge=1)
    amount: float = Field(gt=0)


class RefundReq(BaseModel):
    order_id: uuid.UUID
    reason: str
    line_refunds: list[RefundLineReq]


class ExchangeLineReq(BaseModel):
    order_line_id: int
    quantity: int = Field(ge=1)
    amount: float | None = None


class ExchangeReq(BaseModel):
    order_id: uuid.UUID
    reason: str
    line_exchanges: list[ExchangeLineReq]


class ReverseSettlementReq(BaseModel):
    order_id: uuid.UUID
    reason: str


class ProjectCreateReq(BaseModel):
    title: str
    content: dict[str, Any]


class ProjectSubmitReq(BaseModel):
    content: dict[str, Any]


class ProjectStatusReq(BaseModel):
    action: str


class NotificationReq(BaseModel):
    recipient_user_id: uuid.UUID
    event_type: str
    object_id: str
    message: str


class TriggerEventReq(BaseModel):
    object_id: str
    message: str


class FeatureValueReq(BaseModel):
    feature_id: uuid.UUID
    entity_key: str
    value: float


class FeatureComputeReq(BaseModel):
    feature_id: uuid.UUID
    entity_key: str
    payload: dict[str, Any]


class ConfigReq(BaseModel):
    config_key: str
    payload: dict[str, Any]
    rollout_percent: int = Field(ge=1, le=100)


class ShiftCreateReq(BaseModel):
    assigned_user_id: uuid.UUID
    starts_at: datetime
    ends_at: datetime
    note: str | None = None


class ShiftUpdateReq(BaseModel):
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    note: str | None = None


class ShiftStatusReq(BaseModel):
    status: ShiftStatus


class PermissionGrantReq(BaseModel):
    target_user_id: uuid.UUID
    role: RoleType


class PermissionRevokeReq(BaseModel):
    target_user_id: uuid.UUID
    role: RoleType


class PermissionUpdateReq(BaseModel):
    role: RoleType


def _notification_event_handler(payload: dict[str, Any]) -> None:
    target_roles_by_event = {
        "pending_approval": {RoleType.REVIEWER.value, RoleType.OP_ADMIN.value},
        "contract_expiration": {RoleType.STORE_MANAGER.value, RoleType.OP_ADMIN.value},
        "budget_alert": {RoleType.STORE_MANAGER.value, RoleType.OP_ADMIN.value},
    }
    target_roles = target_roles_by_event.get(payload["event_type"], {RoleType.OP_ADMIN.value})
    actor_user_id = payload.get("actor_user_id")
    with SessionLocal() as db:
        recipients = db.scalars(select(UserRoleBinding).where(UserRoleBinding.role.in_(target_roles))).all()
        for recipient in recipients:
            if actor_user_id and str(recipient.user_id) == str(actor_user_id):
                continue
            note = push_notification(
                db,
                NotificationEvent(
                    event_type=payload["event_type"],
                    object_id=payload["object_id"],
                    recipient_user_id=recipient.user_id,
                    message=payload["message"],
                ),
            )
            if note:
                import asyncio

                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(ws_hub.send_user(str(recipient.user_id), note.message))
                except RuntimeError:
                    # No running loop in sync context; rely on inbox persistence.
                    pass


@router.post("/notifications/triggers/contract-expiration")
def trigger_contract_expiration(
    req: TriggerEventReq,
    _: User = Depends(require_roles(RoleType.OP_ADMIN, RoleType.STORE_MANAGER)),
) -> dict[str, Any]:
    event_bus.publish(
        "project_submitted",
        {
            "event_type": "contract_expiration",
            "object_id": req.object_id,
            "message": req.message,
        },
    )
    return success({"queued": True, "event_type": "contract_expiration", "object_id": req.object_id})


@router.post("/notifications/triggers/budget-alert")
def trigger_budget_alert(
    req: TriggerEventReq,
    _: User = Depends(require_roles(RoleType.OP_ADMIN, RoleType.STORE_MANAGER)),
) -> dict[str, Any]:
    event_bus.publish(
        "project_submitted",
        {
            "event_type": "budget_alert",
            "object_id": req.object_id,
            "message": req.message,
        },
    )
    return success({"queued": True, "event_type": "budget_alert", "object_id": req.object_id})


event_bus.subscribe("project_submitted", _notification_event_handler)


@router.post("/auth/seed-admin")
def seed_admin_handler(
    req: SeedAdminReq,
    db: Session = Depends(get_db),
    install_token: Annotated[str | None, Header(alias="X-Install-Token")] = None,
) -> dict[str, Any]:
    settings = get_settings()
    if not settings.bootstrap_mode:
        raise APIError(status_code=403, code="bootstrap_disabled", message="Seed admin is disabled in normal runtime")
    if not settings.install_bootstrap_token:
        raise APIError(status_code=500, code="bootstrap_misconfigured", message="Bootstrap token is not configured")
    if install_token != settings.install_bootstrap_token:
        raise APIError(status_code=401, code="invalid_install_token", message="Invalid install token")
    try:
        admin = seed_admin(db, username=req.username, password=req.password)
    except DomainError as exc:
        raise APIError(status_code=400, code="seed_admin_failed", message=str(exc)) from exc
    return success({"id": str(admin.id), "username": admin.username, "password_change_required": admin.password_change_required})


@router.post("/auth/users")
def create_user_handler(
    req: UserCreateReq,
    db: Session = Depends(get_db),
    _: User = Depends(require_roles(RoleType.OP_ADMIN)),
) -> dict[str, Any]:
    try:
        user = create_user(db, req.username, req.password, req.display_name, req.role, req.id_number, req.contact)
        return success({"id": str(user.id), "username": user.username, "role": req.role.value})
    except DuplicateUsernameError as exc:
        raise APIError(status_code=409, code="username_exists", message=str(exc)) from exc
    except ValueError as exc:
        raise APIError(status_code=400, code="invalid_password", message=str(exc)) from exc


@router.post("/auth/login")
def login_handler(req: LoginReq, db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        result = login(db, req.username, req.password)
    except AccountLockedError as exc:
        raise APIError(status_code=423, code="account_locked", message=str(exc)) from exc
    except DomainError as exc:
        raise APIError(status_code=401, code="invalid_credentials", message=str(exc)) from exc
    expires_minutes = get_settings().access_token_expire_minutes
    token, expires_in = create_access_token(subject=str(result.user.id), roles=result.roles, expires_minutes=expires_minutes)
    return success(
        {
            "access_token": token,
            "token_type": "bearer",
            "expires_in": expires_in,
            "user": {
                "id": str(result.user.id),
                "username": result.user.username,
                "roles": result.roles,
                "password_change_required": result.user.password_change_required,
            },
        }
    )


@router.post("/auth/change-password")
def change_password_handler(
    req: ChangePasswordReq,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_allow_password_change),
) -> dict[str, Any]:
    try:
        user = change_password(db, current_user.id, req.current_password, req.new_password)
        return success({"id": str(user.id), "password_change_required": user.password_change_required})
    except DomainError as exc:
        raise APIError(status_code=400, code="password_change_failed", message=str(exc)) from exc
    except ValueError as exc:
        raise APIError(status_code=400, code="invalid_password", message=str(exc)) from exc


@router.post("/products")
def create_product(
    req: ProductCreateReq,
    db: Session = Depends(get_db),
    _: User = Depends(require_roles(RoleType.STORE_MANAGER, RoleType.OP_ADMIN)),
) -> dict[str, Any]:
    p = Product(**req.model_dump())
    db.add(p)
    db.commit()
    db.refresh(p)
    return success({"id": str(p.id)})


@router.get("/products/search")
def search_products(q: str, db: Session = Depends(get_db), _: User = Depends(get_current_user)) -> dict[str, Any]:
    rows = product_search(db, q)
    return success([{"id": str(p.id), "name": p.name, "barcode": p.barcode, "price": float(p.unit_price)} for p in rows])


@router.post("/promotions")
def create_promotion(
    req: PromotionRuleReq,
    db: Session = Depends(get_db),
    _: User = Depends(require_roles(RoleType.STORE_MANAGER, RoleType.OP_ADMIN)),
) -> dict[str, Any]:
    row = PromotionRule(**req.model_dump())
    db.add(row)
    db.commit()
    db.refresh(row)
    return success({"id": str(row.id)})


@router.post("/orders/calculate")
def calculate_cart_handler(req: CartCalculateReq, db: Session = Depends(get_db), _: User = Depends(get_current_user)) -> dict[str, Any]:
    try:
        return success(calculate_cart(db, [l.model_dump() for l in req.lines]))
    except DomainError as exc:
        raise APIError(status_code=400, code="cart_validation_failed", message=str(exc)) from exc


@router.post("/orders/checkout")
def checkout_handler(
    req: CheckoutReq,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(RoleType.CASHIER, RoleType.STORE_MANAGER, RoleType.OP_ADMIN)),
) -> dict[str, Any]:
    try:
        order = checkout_order(db, current_user.id, req.customer_name, [l.model_dump() for l in req.lines])
        return success({"order_id": str(order.id), "status": order.status.value, "final_amount": float(order.final_amount)})
    except DomainError as exc:
        raise APIError(status_code=400, code="checkout_failed", message=str(exc)) from exc


@router.post("/orders/{order_id}/settle")
def settlement_handler(
    order_id: uuid.UUID,
    req: SettlementReq,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(RoleType.CASHIER, RoleType.STORE_MANAGER, RoleType.OP_ADMIN)),
) -> dict[str, Any]:
    try:
        _assert_order_financial_access(db, current_user, order_id)
        order = settle_order(db, order_id, req.payments, current_user.id)
        write_access_log(db, current_user.id, "order_settle", "order", str(order_id))
        db.commit()
        db.refresh(order)
        return success({"order_id": str(order.id), "status": order.status.value})
    except DomainError as exc:
        raise APIError(status_code=400, code="settlement_failed", message=str(exc)) from exc


@router.post("/orders/{order_id}/receipt/print", summary="Print settled order receipt")
def print_receipt_handler(
    order_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(RoleType.CASHIER, RoleType.STORE_MANAGER, RoleType.OP_ADMIN)),
) -> dict[str, Any]:
    try:
        result = print_receipt_for_order(db, order_id=order_id, actor_user_id=current_user.id)
        write_access_log(db, current_user.id, "receipt_print", "order", str(order_id))
        db.commit()
        return success(result)
    except DomainError as exc:
        raise APIError(status_code=400, code="receipt_print_failed", message=str(exc)) from exc


@router.post("/after-sales/refund")
def refund_handler(
    req: RefundReq,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(RoleType.CASHIER, RoleType.STORE_MANAGER, RoleType.OP_ADMIN)),
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, Any]:
    if not idempotency_key:
        raise APIError(status_code=400, code="missing_idempotency_key", message="Idempotency-Key header is required")
    try:
        _assert_order_financial_access(db, current_user, req.order_id)
        row = process_refund(
            db,
            order_id=req.order_id,
            reason=req.reason,
            idempotency_key=idempotency_key,
            user_id=current_user.id,
            line_refunds=[l.model_dump() for l in req.line_refunds],
        )
        write_access_log(db, current_user.id, "after_sales_refund", "after_sales_order", str(row.id))
        db.commit()
        db.refresh(row)
        return success({"refund_id": str(row.id), "amount": float(row.amount), "idempotency_key": row.idempotency_key})
    except DomainError as exc:
        raise APIError(status_code=400, code="refund_failed", message=str(exc)) from exc


@router.post("/after-sales/exchange")
def exchange_handler(
    req: ExchangeReq,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(RoleType.CASHIER, RoleType.STORE_MANAGER, RoleType.OP_ADMIN)),
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, Any]:
    if not idempotency_key:
        raise APIError(status_code=400, code="missing_idempotency_key", message="Idempotency-Key header is required")
    try:
        _assert_order_financial_access(db, current_user, req.order_id)
        row = process_exchange(
            db,
            order_id=req.order_id,
            reason=req.reason,
            idempotency_key=idempotency_key,
            user_id=current_user.id,
            line_exchanges=[l.model_dump() for l in req.line_exchanges],
        )
        write_access_log(db, current_user.id, "after_sales_exchange", "after_sales_order", str(row.id))
        db.commit()
        db.refresh(row)
        return success({"after_sales_id": str(row.id), "type": row.type, "amount": float(row.amount)})
    except DomainError as exc:
        raise APIError(status_code=400, code="exchange_failed", message=str(exc)) from exc


@router.post("/after-sales/reverse-settlement")
def reverse_settlement_handler(
    req: ReverseSettlementReq,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(RoleType.CASHIER, RoleType.STORE_MANAGER, RoleType.OP_ADMIN)),
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, Any]:
    if not idempotency_key:
        raise APIError(status_code=400, code="missing_idempotency_key", message="Idempotency-Key header is required")
    try:
        _assert_order_financial_access(db, current_user, req.order_id)
        row = process_reverse_settlement(
            db,
            order_id=req.order_id,
            reason=req.reason,
            idempotency_key=idempotency_key,
            user_id=current_user.id,
        )
        write_access_log(db, current_user.id, "after_sales_reverse_settlement", "after_sales_order", str(row.id))
        db.commit()
        db.refresh(row)
        return success({"after_sales_id": str(row.id), "type": row.type, "amount": float(row.amount)})
    except DomainError as exc:
        raise APIError(status_code=400, code="reverse_settlement_failed", message=str(exc)) from exc


@router.post("/orders/auto-void")
def auto_void_handler(
    db: Session = Depends(get_db),
    _: User = Depends(require_roles(RoleType.OP_ADMIN, RoleType.STORE_MANAGER)),
) -> dict[str, Any]:
    return success({"voided_count": auto_void_unsettled_orders(db)})


@router.post("/projects")
def create_project_handler(
    req: ProjectCreateReq,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(RoleType.PROJECT_APPLICANT, RoleType.OP_ADMIN)),
) -> dict[str, Any]:
    row = create_project(db, current_user.id, req.title, req.content)
    return success({"id": str(row.id), "status": row.status.value})


@router.post("/projects/{project_id}/submit")
async def submit_project_handler(
    project_id: uuid.UUID,
    req: ProjectSubmitReq,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(RoleType.PROJECT_APPLICANT, RoleType.OP_ADMIN)),
) -> dict[str, Any]:
    _assert_project_access(db, current_user, project_id)
    try:
        row = submit_project(db, project_id, req.content, current_user.id)
        return success(
            {
                "id": str(row.id),
                "status": row.status.value,
                "version": row.current_version_no,
                "notification_id": None,
            }
        )
    except DomainError as exc:
        raise APIError(status_code=400, code="project_submit_failed", message=str(exc)) from exc


@router.post("/shifts", summary="Create shift schedule")
def create_shift_handler(
    req: ShiftCreateReq,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(RoleType.OP_ADMIN)),
) -> dict[str, Any]:
    try:
        shift = create_shift(
            db,
            assigned_user_id=req.assigned_user_id,
            starts_at=req.starts_at,
            ends_at=req.ends_at,
            note=req.note,
            actor_user_id=current_user.id,
        )
        write_access_log(db, current_user.id, "shift_create", "shift", str(shift.id))
        db.commit()
        return success(
            {
                "id": str(shift.id),
                "assigned_user_id": str(shift.assigned_user_id),
                "status": shift.status.value,
                "starts_at": shift.starts_at.isoformat(),
                "ends_at": shift.ends_at.isoformat(),
                "note": shift.note,
            }
        )
    except DomainError as exc:
        raise APIError(status_code=400, code="shift_create_failed", message=str(exc)) from exc


@router.get("/shifts", summary="List shifts (admin)")
def list_shifts_handler(
    assigned_user_id: uuid.UUID | None = None,
    db: Session = Depends(get_db),
    _: User = Depends(require_roles(RoleType.OP_ADMIN)),
) -> dict[str, Any]:
    rows = list_shifts(db, assigned_user_id=assigned_user_id)
    return success(
        [
            {
                "id": str(row.id),
                "assigned_user_id": str(row.assigned_user_id),
                "status": row.status.value,
                "starts_at": row.starts_at.isoformat(),
                "ends_at": row.ends_at.isoformat(),
                "note": row.note,
            }
            for row in rows
        ]
    )


@router.get("/shifts/me", summary="List current user's shifts")
def my_shifts_handler(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)) -> dict[str, Any]:
    rows = list_shifts(db, assigned_user_id=current_user.id)
    return success(
        [
            {
                "id": str(row.id),
                "assigned_user_id": str(row.assigned_user_id),
                "status": row.status.value,
                "starts_at": row.starts_at.isoformat(),
                "ends_at": row.ends_at.isoformat(),
                "note": row.note,
            }
            for row in rows
        ]
    )


@router.get("/shifts/{shift_id}", summary="Get shift details")
def get_shift_handler(
    shift_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, Any]:
    try:
        row = get_shift(db, shift_id)
        user_roles = _current_user_roles(db, current_user.id)
        if current_user.id != row.assigned_user_id and not user_roles.intersection({RoleType.OP_ADMIN.value}):
            raise APIError(status_code=403, code="forbidden", message="Insufficient role permissions")
        return success(
            {
                "id": str(row.id),
                "assigned_user_id": str(row.assigned_user_id),
                "status": row.status.value,
                "starts_at": row.starts_at.isoformat(),
                "ends_at": row.ends_at.isoformat(),
                "note": row.note,
            }
        )
    except DomainError as exc:
        raise APIError(status_code=404, code="shift_not_found", message=str(exc)) from exc


@router.patch("/shifts/{shift_id}", summary="Update shift schedule")
def update_shift_handler(
    shift_id: uuid.UUID,
    req: ShiftUpdateReq,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(RoleType.OP_ADMIN)),
) -> dict[str, Any]:
    try:
        row = update_shift(
            db,
            shift_id=shift_id,
            starts_at=req.starts_at,
            ends_at=req.ends_at,
            note=req.note,
            actor_user_id=current_user.id,
        )
        write_access_log(db, current_user.id, "shift_update", "shift", str(shift_id))
        db.commit()
        return success({"id": str(row.id), "status": row.status.value, "starts_at": row.starts_at.isoformat(), "ends_at": row.ends_at.isoformat(), "note": row.note})
    except DomainError as exc:
        raise APIError(status_code=400, code="shift_update_failed", message=str(exc)) from exc


@router.patch("/shifts/{shift_id}/status", summary="Transition shift status")
def update_shift_status_handler(
    shift_id: uuid.UUID,
    req: ShiftStatusReq,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(RoleType.OP_ADMIN)),
) -> dict[str, Any]:
    try:
        row = update_shift_status(db, shift_id=shift_id, status=req.status, actor_user_id=current_user.id)
        write_access_log(db, current_user.id, "shift_status_update", "shift", str(shift_id))
        db.commit()
        return success({"id": str(row.id), "status": row.status.value})
    except DomainError as exc:
        raise APIError(status_code=400, code="shift_status_failed", message=str(exc)) from exc


@router.post("/permissions/grant", summary="Grant role binding")
def grant_permission_handler(
    req: PermissionGrantReq,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(RoleType.OP_ADMIN)),
) -> dict[str, Any]:
    try:
        row = grant_role_binding(db, target_user_id=req.target_user_id, role=req.role, actor_user_id=current_user.id)
        write_access_log(db, current_user.id, "permission_grant", "user_role_binding", str(row.id))
        db.commit()
        return success({"binding_id": row.id, "target_user_id": str(row.user_id), "role": row.role.value})
    except DomainError as exc:
        raise APIError(status_code=400, code="permission_grant_failed", message=str(exc)) from exc


@router.post("/permissions/revoke", summary="Revoke role binding")
def revoke_permission_handler(
    req: PermissionRevokeReq,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(RoleType.OP_ADMIN)),
) -> dict[str, Any]:
    try:
        revoke_role_binding(db, target_user_id=req.target_user_id, role=req.role, actor_user_id=current_user.id)
        write_access_log(db, current_user.id, "permission_revoke", "user_role_binding", str(req.target_user_id))
        db.commit()
        return success({"target_user_id": str(req.target_user_id), "role": req.role.value, "revoked": True})
    except DomainError as exc:
        raise APIError(status_code=400, code="permission_revoke_failed", message=str(exc)) from exc


@router.patch("/permissions/bindings/{binding_id}", summary="Update existing role binding")
def update_permission_handler(
    binding_id: int,
    req: PermissionUpdateReq,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(RoleType.OP_ADMIN)),
) -> dict[str, Any]:
    try:
        row = update_role_binding(db, binding_id=binding_id, new_role=req.role, actor_user_id=current_user.id)
        write_access_log(db, current_user.id, "permission_update", "user_role_binding", str(binding_id))
        db.commit()
        return success({"binding_id": row.id, "target_user_id": str(row.user_id), "role": row.role.value})
    except DomainError as exc:
        raise APIError(status_code=400, code="permission_update_failed", message=str(exc)) from exc


@router.patch("/projects/{project_id}/status")
def project_status_handler(
    project_id: uuid.UUID,
    req: ProjectStatusReq,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(RoleType.REVIEWER, RoleType.OP_ADMIN)),
) -> dict[str, Any]:
    action_to_status = {
        "start_review": ProjectStatus.UNDER_REVIEW,
        "approve": ProjectStatus.APPROVED,
        "reject": ProjectStatus.REJECTED,
        "deactivate": ProjectStatus.DEACTIVATED,
    }
    if req.action == "resubmit":
        raise APIError(
            status_code=400,
            code="resubmit_requires_submit_flow",
            message="Resubmit must use /projects/{id}/submit with updated content to create a new version and diff",
            details={},
        )
    target_status = action_to_status.get(req.action)
    if target_status is None:
        raise APIError(
            status_code=422,
            code="invalid_action",
            message=f"Unsupported action '{req.action}'",
            details={"allowed_actions": sorted(action_to_status.keys())},
        )
    try:
        row = update_project_status(db, project_id, target_status, current_user.id)
        write_access_log(db, current_user.id, "project_review_status_change", "project", str(project_id))
        db.commit()
        return success({"id": str(row.id), "status": row.status.value, "action": req.action})
    except DomainError as exc:
        raise APIError(status_code=400, code="project_transition_failed", message=str(exc)) from exc


@router.get("/projects/{project_id}/diff")
def project_diff_handler(
    project_id: uuid.UUID,
    v1: int,
    v2: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, Any]:
    _assert_project_access(db, current_user, project_id)
    try:
        return success({"project_id": str(project_id), "v1": v1, "v2": v2, "diff": get_project_diff(db, project_id, v1, v2)})
    except DomainError as exc:
        raise APIError(status_code=404, code="project_diff_not_found", message=str(exc)) from exc


@router.post("/projects/{project_id}/attachments")
async def upload_attachment(
    project_id: uuid.UUID,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, Any]:
    _assert_project_access(db, current_user, project_id)
    data = await file.read()
    try:
        row = save_attachment(
            db,
            project_id=project_id,
            filename=file.filename or "file.bin",
            mime_type=file.content_type or "application/octet-stream",
            file_bytes=data,
        )
        write_access_log(db, current_user.id, "attachment_upload", "attachment", str(row.id))
        db.commit()
        return success({"id": str(row.id), "fingerprint": row.sha256_fingerprint})
    except DomainError as exc:
        raise APIError(status_code=400, code="attachment_invalid", message=str(exc)) from exc


@router.get("/attachments/{attachment_id}/verify")
def verify_attachment(attachment_id: uuid.UUID, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)) -> dict[str, Any]:
    row = db.get(Attachment, attachment_id)
    if not row:
        raise APIError(status_code=404, code="attachment_not_found", message="Attachment not found")
    _assert_project_access(db, current_user, row.project_id)
    write_access_log(db, current_user.id, "attachment_verify", "attachment", str(attachment_id))
    db.commit()
    return success({"attachment_id": str(attachment_id), "valid": verify_attachment_integrity(row)})


@router.post("/notifications")
def create_notification(
    req: NotificationReq,
    db: Session = Depends(get_db),
    _: User = Depends(require_roles(RoleType.OP_ADMIN, RoleType.STORE_MANAGER, RoleType.REVIEWER)),
) -> dict[str, Any]:
    row = push_notification(
        db,
        NotificationEvent(
            event_type=req.event_type,
            object_id=req.object_id,
            recipient_user_id=req.recipient_user_id,
            message=req.message,
        ),
    )
    if row is None:
        return success({"status": "throttled"})
    return success({"id": str(row.id), "delivered_at": row.delivered_at.isoformat() if row.delivered_at else None})


@router.get("/notifications")
def notifications_inbox(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, Any]:
    rows = list_notifications(db, current_user.id)
    return success(
        [
            {
                "id": str(r.id),
                "event_type": r.event_type,
                "object_id": r.object_id,
                "message": r.message,
                "delivered_at": r.delivered_at.isoformat() if r.delivered_at else None,
                "read_at": r.read_at.isoformat() if r.read_at else None,
            }
            for r in rows
        ]
    )


@router.patch("/notifications/{notification_id}/read")
def read_notification(
    notification_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, Any]:
    try:
        row = mark_notification_read_for_user(db, notification_id, current_user.id)
        write_access_log(db, current_user.id, "notification_mark_read", "notification", str(notification_id))
        db.commit()
        return success({"id": str(row.id), "read_at": row.read_at.isoformat() if row.read_at else None})
    except DomainError as exc:
        code = "notification_not_found" if "not found" in str(exc).lower() else "forbidden_notification_access"
        status_code = 404 if code == "notification_not_found" else 403
        raise APIError(status_code=status_code, code=code, message=str(exc)) from exc


@router.websocket("/notifications/stream")
async def notifications_ws(websocket: WebSocket) -> None:
    token = websocket.query_params.get("token")
    if not token:
        await websocket.close(code=1008, reason="Missing token")
        return
    try:
        with SessionLocal() as db:
            user = resolve_user_from_token(db, token)
            user_id = str(user.id)
    except APIError as exc:
        await websocket.close(code=1008, reason=exc.message)
        return
    await ws_hub.connect(user_id, websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws_hub.disconnect(user_id, websocket)


@router.post("/features/definitions")
def create_feature_definition(
    name: str,
    calculation_type: str,
    ttl_seconds: int,
    lineage_note: str,
    db: Session = Depends(get_db),
    _: User = Depends(require_roles(RoleType.OP_ADMIN)),
) -> dict[str, Any]:
    row = FeatureDefinition(name=name, calculation_type=calculation_type, ttl_seconds=ttl_seconds, lineage_note=lineage_note)
    db.add(row)
    db.commit()
    db.refresh(row)
    return success({"id": str(row.id)})


@router.post("/features/values")
def upsert_feature(req: FeatureValueReq, db: Session = Depends(get_db), _: User = Depends(require_roles(RoleType.OP_ADMIN))) -> dict[str, Any]:
    row = upsert_feature_value(db, feature_id=req.feature_id, entity_key=req.entity_key, value=req.value)
    return success({"id": row.id, "consistency_hash": row.consistency_hash})


@router.post("/features/compute")
def compute_feature(req: FeatureComputeReq, db: Session = Depends(get_db), _: User = Depends(require_roles(RoleType.OP_ADMIN))) -> dict[str, Any]:
    try:
        result = compute_feature_value(db, feature_id=req.feature_id, entity_key=req.entity_key, payload=req.payload)
        return success(result)
    except DomainError as exc:
        raise APIError(status_code=400, code="feature_compute_failed", message=str(exc)) from exc


@router.post("/features/compact")
def compact_features(db: Session = Depends(get_db), _: User = Depends(require_roles(RoleType.OP_ADMIN))) -> dict[str, Any]:
    return success({"moved_to_cold": compact_feature_values(db)})


@router.get("/features/consistency")
def feature_consistency(
    feature_id: uuid.UUID,
    entity_key: str,
    db: Session = Depends(get_db),
    _: User = Depends(require_roles(RoleType.OP_ADMIN)),
) -> dict[str, Any]:
    return success({"consistent": verify_feature_consistency(db, feature_id, entity_key)})


@router.post("/configs")
def create_config(
    req: ConfigReq,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(RoleType.OP_ADMIN)),
) -> dict[str, Any]:
    row = set_operation_config(db, req.config_key, req.payload, req.rollout_percent, current_user.id)
    return success({"id": str(row.id), "version": row.version, "rollout_percent": row.rollout_percent})


@router.post("/configs/{config_key}/rollback/{version}")
def rollback_config(
    config_key: str,
    version: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_roles(RoleType.OP_ADMIN)),
) -> dict[str, Any]:
    row = rollback_operation_config(db, config_key, version)
    return success({"id": str(row.id), "version": row.version, "is_active": row.is_active})


@router.post("/analytics/aggregate")
def aggregate_metrics(
    day: datetime | None = None,
    db: Session = Depends(get_db),
    _: User = Depends(require_roles(RoleType.STORE_MANAGER, RoleType.OP_ADMIN)),
) -> dict[str, Any]:
    row = aggregate_daily_metrics(db, day=day)
    return success({"date": row.date.isoformat(), "transaction_volume": row.transaction_volume, "dispute_rate": row.dispute_rate})


@router.get("/analytics/daily-metrics")
def daily_metrics(
    date_start: datetime,
    date_end: datetime,
    db: Session = Depends(get_db),
    _: User = Depends(require_roles(RoleType.STORE_MANAGER, RoleType.OP_ADMIN)),
) -> dict[str, Any]:
    rows = query_daily_metrics(db, date_start, date_end)
    return success(
        [
            {
                "date": r.date.isoformat(),
                "transaction_volume": r.transaction_volume,
                "conversion_rate": r.conversion_rate,
                "activity_score": r.activity_score,
                "dispute_rate": r.dispute_rate,
            }
            for r in rows
        ]
    )


@router.get("/analytics/export")
def export_metrics(
    date_start: datetime | None = None,
    date_end: datetime | None = None,
    db: Session = Depends(get_db),
    _: User = Depends(require_roles(RoleType.STORE_MANAGER, RoleType.OP_ADMIN)),
) -> StreamingResponse:
    csv_data = export_daily_metrics_csv(db, start_date=date_start, end_date=date_end)
    return StreamingResponse(
        iter([csv_data]),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="analytics.csv"'},
    )
