# API Specification (Implementation-Accurate)

## 1. Runtime Overview

- Base API prefix: `/api/v1`
- Swagger/OpenAPI UI: `/docs` (also `/swagger` redirects to `/docs`)
- Health endpoint: `GET /health`
- Content type: `application/json` unless explicitly stated otherwise

## 2. Authentication and Authorization

- Protected HTTP endpoints use `Authorization: Bearer <JWT>`.
- WebSocket auth uses query param token: `/api/v1/notifications/stream?token=<JWT>`.
- If bearer token is missing: `401` with code `missing_token`.
- Invalid/expired token: `401` with code `invalid_token`.
- Inactive user: `403` with code `inactive_user`.
- Password-change-required users are blocked from most protected endpoints with `403` + `password_change_required` (except change-password).
- Role check failure: `403` with code `forbidden`.

### 2.1 Role enum (`RoleType`)

- `cashier`
- `store_manager`
- `project_applicant`
- `reviewer`
- `operation_admin`

### 2.2 Other enums

- `ShiftStatus`: `scheduled | active | completed | cancelled`
- `ProjectStatus`: `draft | submitted | under_review | rejected | approved | deactivated`
- `OrderStatus`: `draft | settled | void`
- `PaymentMethod` (used in settlement payload objects): `cash | bank_card | stored_value`

## 3. Global Response Contracts

### 3.1 Success envelope (JSON endpoints)

```json
{
  "status": "success",
  "message": "ok",
  "data": {}
}
```

- `status`: always `"success"`
- `message`: string (defaults to `"ok"`)
- `data`: endpoint-specific payload (object, array, or primitive-compatible object)

### 3.2 Error envelope

```json
{
  "status": "error",
  "code": "error_code",
  "message": "Human readable message",
  "details": {}
}
```

- Validation errors return:
  - `status`: `error`
  - `code`: `validation_error`
  - `message`: `Request validation failed`
  - `details.errors`: array of FastAPI/Pydantic validation entries
- Sensitive validation inputs are redacted as `***REDACTED***` for fields containing:
  - `password`, `token`, `secret`, `id_number`, `contact`

### 3.3 Non-envelope endpoints

- `GET /` and `GET /swagger`: HTTP redirect to `/docs`
- `GET /api/v1/analytics/export`: `text/csv` streaming response
- `WS /api/v1/notifications/stream`: text frames over websocket, no JSON envelope

## 4. Endpoint Reference

All paths below are full paths.

---

## 4.1 System

### GET `/health`

- Auth: none
- Response `200`:
  - `data`: `{ "status": "ok" }`

---

## 4.2 Auth

### POST `/api/v1/auth/seed-admin`

- Auth: no bearer token
- Required header: `X-Install-Token: string`
- Body:
  - `username: string` (optional, default `"admin"`)
  - `password: string` (required)
- Response `200` data:
  - `id: string` (UUID)
  - `username: string`
  - `password_change_required: boolean`
- Errors:
  - `403/bootstrap_disabled`
  - `500/bootstrap_misconfigured`
  - `401/invalid_install_token`
  - `400/seed_admin_failed`

### POST `/api/v1/auth/users`

- Auth: `operation_admin`
- Body:
  - `username: string`
  - `password: string`
  - `display_name: string`
  - `role: RoleType`
  - `id_number: string | null` (optional)
  - `contact: string | null` (optional)
- Response `200` data:
  - `id: string` (UUID)
  - `username: string`
  - `role: RoleType`
- Errors:
  - `409/username_exists`
  - `400/invalid_password`

### POST `/api/v1/auth/login`

- Auth: none
- Body:
  - `username: string`
  - `password: string`
- Response `200` data:
  - `access_token: string`
  - `token_type: "bearer"`
  - `expires_in: integer` (seconds)
  - `user:`
    - `id: string` (UUID)
    - `username: string`
    - `roles: string[]`
    - `password_change_required: boolean`
- Errors:
  - `423/account_locked`
  - `401/invalid_credentials`

### POST `/api/v1/auth/change-password`

- Auth: bearer token (password-change-required users allowed)
- Body:
  - `current_password: string`
  - `new_password: string`
- Response `200` data:
  - `id: string` (UUID)
  - `password_change_required: boolean`
- Errors:
  - `400/password_change_failed`
  - `400/invalid_password`

---

## 4.3 Products and Promotions

### POST `/api/v1/products`

- Auth: `store_manager` or `operation_admin`
- Body:
  - `name: string`
  - `barcode: string`
  - `internal_code: string`
  - `pinyin: string`
  - `unit_price: number`
- Response `200` data:
  - `id: string` (UUID)

### GET `/api/v1/products/search`

- Auth: any authenticated active user
- Query:
  - `q: string` (required)
- Response `200` data: array of
  - `id: string` (UUID)
  - `name: string`
  - `barcode: string`
  - `price: number`

### POST `/api/v1/promotions`

- Auth: `store_manager` or `operation_admin`
- Body:
  - `name: string`
  - `scope: string` (regex: `^(item|order|global)$`)
  - `rule_type: string`
  - `config: object` (optional, default `{}`)
- Response `200` data:
  - `id: string` (UUID)

---

## 4.4 Orders and Settlement

### Shared cart line schema

- `product_id: string` (UUID)
- `quantity: integer` (`>= 1`)

### POST `/api/v1/orders/calculate`

- Auth: any authenticated active user
- Body:
  - `lines: CartLine[]`
- Response `200` data:
  - `lines:`
    - `product_id: string` (UUID)
    - `quantity: integer`
    - `unit_price: number`
    - `line_subtotal: number`
    - `line_discount: number`
  - `subtotal: number`
  - `discount_total: number`
  - `final_total: number`
- Errors:
  - `400/cart_validation_failed`

### POST `/api/v1/orders/checkout`

- Auth: `cashier | store_manager | operation_admin`
- Body:
  - `customer_name: string | null` (optional)
  - `lines: CartLine[]`
- Response `200` data:
  - `order_id: string` (UUID)
  - `status: OrderStatus`
  - `final_amount: number`
- Errors:
  - `400/checkout_failed`

### POST `/api/v1/orders/{order_id}/settle`

- Auth: `cashier | store_manager | operation_admin`
- Path:
  - `order_id: string` (UUID)
- Body:
  - `payments: object[]`
  - Each payment object is expected to carry:
    - `method: PaymentMethod`
    - `amount: number`
    - `reference: string | null` (optional)
- Response `200` data:
  - `order_id: string` (UUID)
  - `status: OrderStatus`
- Errors:
  - `404/order_not_found`
  - `403/forbidden_order_access`
  - `400/settlement_failed`

### POST `/api/v1/orders/{order_id}/receipt/print`

- Auth: `cashier | store_manager | operation_admin`
- Path:
  - `order_id: string` (UUID)
- Response `200` data:
  - `order_id: string` (UUID)
  - `backend: string`
  - `line_count: integer`
- Errors:
  - `400/receipt_print_failed`

### POST `/api/v1/orders/auto-void`

- Auth: `operation_admin | store_manager`
- Body: none
- Response `200` data:
  - `voided_count: integer`

---

## 4.5 After-Sales

All after-sales write endpoints require header:
- `Idempotency-Key: string` (required)

### Refund payload line

- `order_line_id: integer`
- `quantity: integer` (`>= 1`)
- `amount: number` (`> 0`)

### POST `/api/v1/after-sales/refund`

- Auth: `cashier | store_manager | operation_admin`
- Body:
  - `order_id: string` (UUID)
  - `reason: string`
  - `line_refunds: RefundLine[]`
- Response `200` data:
  - `refund_id: string` (UUID)
  - `amount: number`
  - `idempotency_key: string`
- Idempotency behavior:
  - Same key + same payload -> same `refund_id`
  - Same key + different payload -> `400/refund_failed`
- Errors:
  - `400/missing_idempotency_key`
  - `404/order_not_found`
  - `403/forbidden_order_access`
  - `400/refund_failed`

### POST `/api/v1/after-sales/exchange`

- Auth: `cashier | store_manager | operation_admin`
- Body:
  - `order_id: string` (UUID)
  - `reason: string`
  - `line_exchanges:`
    - `order_line_id: integer`
    - `quantity: integer` (`>= 1`)
    - `amount: number | null` (optional)
- Response `200` data:
  - `after_sales_id: string` (UUID)
  - `type: string`
  - `amount: number`
- Errors:
  - `400/missing_idempotency_key`
  - `404/order_not_found`
  - `403/forbidden_order_access`
  - `400/exchange_failed`

### POST `/api/v1/after-sales/reverse-settlement`

- Auth: `cashier | store_manager | operation_admin`
- Body:
  - `order_id: string` (UUID)
  - `reason: string`
- Response `200` data:
  - `after_sales_id: string` (UUID)
  - `type: string`
  - `amount: number`
- Errors:
  - `400/missing_idempotency_key`
  - `404/order_not_found`
  - `403/forbidden_order_access`
  - `400/reverse_settlement_failed`

---

## 4.6 Projects

### POST `/api/v1/projects`

- Auth: `project_applicant | operation_admin`
- Body:
  - `title: string`
  - `content: object`
- Response `200` data:
  - `id: string` (UUID)
  - `status: ProjectStatus`

### POST `/api/v1/projects/{project_id}/submit`

- Auth: `project_applicant | operation_admin`
- Path:
  - `project_id: string` (UUID)
- Body:
  - `content: object`
- Response `200` data:
  - `id: string` (UUID)
  - `status: ProjectStatus`
  - `version: integer`
  - `notification_id: string | null`
- Errors:
  - `404/project_not_found`
  - `403/forbidden_project_access`
  - `400/project_submit_failed`

### PATCH `/api/v1/projects/{project_id}/status`

- Auth: `reviewer | operation_admin`
- Path:
  - `project_id: string` (UUID)
- Body:
  - `action: string`
- Allowed actions:
  - `start_review` -> `under_review`
  - `approve` -> `approved`
  - `reject` -> `rejected`
  - `deactivate` -> `deactivated`
- Special cases:
  - `resubmit` is rejected with `400/resubmit_requires_submit_flow`
  - unknown action -> `422/invalid_action` with `details.allowed_actions`
- Response `200` data:
  - `id: string` (UUID)
  - `status: ProjectStatus`
  - `action: string`
- Errors:
  - `400/project_transition_failed`

### GET `/api/v1/projects/{project_id}/diff`

- Auth: any authenticated active user (plus project access checks)
- Path:
  - `project_id: string` (UUID)
- Query:
  - `v1: integer`
  - `v2: integer`
- Response `200` data:
  - `project_id: string` (UUID)
  - `v1: integer`
  - `v2: integer`
  - `diff: object` (dynamic old/new style map)
- Errors:
  - `404/project_not_found`
  - `403/forbidden_project_access`
  - `404/project_diff_not_found`

### POST `/api/v1/projects/{project_id}/attachments`

- Auth: any authenticated active user (plus project access checks)
- Path:
  - `project_id: string` (UUID)
- Content type: `multipart/form-data`
- Form:
  - `file: binary` (required)
- Response `200` data:
  - `id: string` (UUID)
  - `fingerprint: string` (SHA-256 hex)
- Errors:
  - `404/project_not_found`
  - `403/forbidden_project_access`
  - `400/attachment_invalid`

### GET `/api/v1/attachments/{attachment_id}/verify`

- Auth: any authenticated active user (plus project access checks)
- Path:
  - `attachment_id: string` (UUID)
- Response `200` data:
  - `attachment_id: string` (UUID)
  - `valid: boolean`
- Errors:
  - `404/attachment_not_found`
  - `403/forbidden_project_access`

---

## 4.7 Notifications

### POST `/api/v1/notifications/triggers/contract-expiration`

- Auth: `operation_admin | store_manager`
- Body:
  - `object_id: string`
  - `message: string`
- Response `200` data:
  - `queued: true`
  - `event_type: "contract_expiration"`
  - `object_id: string`

### POST `/api/v1/notifications/triggers/budget-alert`

- Auth: `operation_admin | store_manager`
- Body:
  - `object_id: string`
  - `message: string`
- Response `200` data:
  - `queued: true`
  - `event_type: "budget_alert"`
  - `object_id: string`

### POST `/api/v1/notifications`

- Auth: `operation_admin | store_manager | reviewer`
- Body:
  - `recipient_user_id: string` (UUID)
  - `event_type: string`
  - `object_id: string`
  - `message: string`
- Response `200` data, one of:
  - throttled: `{ "status": "throttled" }`
  - created:
    - `id: string` (UUID)
    - `delivered_at: string | null` (ISO datetime)

### GET `/api/v1/notifications`

- Auth: any authenticated active user
- Response `200` data: array of
  - `id: string` (UUID)
  - `event_type: string`
  - `object_id: string`
  - `message: string`
  - `delivered_at: string | null` (ISO datetime)
  - `read_at: string | null` (ISO datetime)

### PATCH `/api/v1/notifications/{notification_id}/read`

- Auth: any authenticated active user
- Path:
  - `notification_id: string` (UUID)
- Response `200` data:
  - `id: string` (UUID)
  - `read_at: string | null` (ISO datetime)
- Errors:
  - `404/notification_not_found`
  - `403/forbidden_notification_access`

### WS `/api/v1/notifications/stream?token=<jwt>`

- Auth: query token required
- On auth failure socket is closed with code `1008` and reason message.
- Server pushes notification messages as text frames (`string`).

---

## 4.8 Shifts

### POST `/api/v1/shifts`

- Auth: `operation_admin`
- Body:
  - `assigned_user_id: string` (UUID)
  - `starts_at: string` (ISO datetime)
  - `ends_at: string` (ISO datetime)
  - `note: string | null` (optional)
- Response `200` data:
  - `id: string` (UUID)
  - `assigned_user_id: string` (UUID)
  - `status: ShiftStatus`
  - `starts_at: string` (ISO datetime)
  - `ends_at: string` (ISO datetime)
  - `note: string | null`
- Errors:
  - `400/shift_create_failed`

### GET `/api/v1/shifts`

- Auth: `operation_admin`
- Query:
  - `assigned_user_id: string` (UUID, optional)
- Response `200` data: array of shift objects (same shape as create response)

### GET `/api/v1/shifts/me`

- Auth: any authenticated active user
- Response `200` data: array of shift objects

### GET `/api/v1/shifts/{shift_id}`

- Auth: any authenticated active user
- Path:
  - `shift_id: string` (UUID)
- Access rule: assignee or `operation_admin`
- Response `200` data: shift object
- Errors:
  - `403/forbidden`
  - `404/shift_not_found`

### PATCH `/api/v1/shifts/{shift_id}`

- Auth: `operation_admin`
- Path:
  - `shift_id: string` (UUID)
- Body (all optional):
  - `starts_at: string | null` (ISO datetime)
  - `ends_at: string | null` (ISO datetime)
  - `note: string | null`
- Response `200` data:
  - `id: string` (UUID)
  - `status: ShiftStatus`
  - `starts_at: string` (ISO datetime)
  - `ends_at: string` (ISO datetime)
  - `note: string | null`
- Errors:
  - `400/shift_update_failed`

### PATCH `/api/v1/shifts/{shift_id}/status`

- Auth: `operation_admin`
- Path:
  - `shift_id: string` (UUID)
- Body:
  - `status: ShiftStatus`
- Response `200` data:
  - `id: string` (UUID)
  - `status: ShiftStatus`
- Errors:
  - `400/shift_status_failed`

---

## 4.9 Permissions

### POST `/api/v1/permissions/grant`

- Auth: `operation_admin`
- Body:
  - `target_user_id: string` (UUID)
  - `role: RoleType`
- Response `200` data:
  - `binding_id: integer`
  - `target_user_id: string` (UUID)
  - `role: RoleType`
- Errors:
  - `400/permission_grant_failed`

### POST `/api/v1/permissions/revoke`

- Auth: `operation_admin`
- Body:
  - `target_user_id: string` (UUID)
  - `role: RoleType`
- Response `200` data:
  - `target_user_id: string` (UUID)
  - `role: RoleType`
  - `revoked: true`
- Errors:
  - `400/permission_revoke_failed`

### PATCH `/api/v1/permissions/bindings/{binding_id}`

- Auth: `operation_admin`
- Path:
  - `binding_id: integer`
- Body:
  - `role: RoleType`
- Response `200` data:
  - `binding_id: integer`
  - `target_user_id: string` (UUID)
  - `role: RoleType`
- Errors:
  - `400/permission_update_failed`

---

## 4.10 Features

### POST `/api/v1/features/definitions`

- Auth: `operation_admin`
- Query parameters:
  - `name: string`
  - `calculation_type: string`
  - `ttl_seconds: integer`
  - `lineage_note: string`
- Response `200` data:
  - `id: string` (UUID)

### POST `/api/v1/features/values`

- Auth: `operation_admin`
- Body:
  - `feature_id: string` (UUID)
  - `entity_key: string`
  - `value: number`
- Response `200` data:
  - `id: integer`
  - `consistency_hash: string`

### POST `/api/v1/features/compute`

- Auth: `operation_admin`
- Body:
  - `feature_id: string` (UUID)
  - `entity_key: string`
  - `payload: object`
- Response `200` data:
  - `value: number`
  - `lineage:`
    - `feature: string`
    - `calculation_type: string`
    - `payload_keys: string[]`
    - `computed_at: string` (ISO datetime)
    - `consistency_hash: string`
  - `consistent: boolean`
- Errors:
  - `400/feature_compute_failed`

### POST `/api/v1/features/compact`

- Auth: `operation_admin`
- Response `200` data:
  - `moved_to_cold: integer`

### GET `/api/v1/features/consistency`

- Auth: `operation_admin`
- Query:
  - `feature_id: string` (UUID)
  - `entity_key: string`
- Response `200` data:
  - `consistent: boolean`

---

## 4.11 Configurations

### POST `/api/v1/configs`

- Auth: `operation_admin`
- Body:
  - `config_key: string`
  - `payload: object`
  - `rollout_percent: integer` (`1..100`)
- Response `200` data:
  - `id: string` (UUID)
  - `version: integer`
  - `rollout_percent: integer`

### POST `/api/v1/configs/{config_key}/rollback/{version}`

- Auth: `operation_admin`
- Path:
  - `config_key: string`
  - `version: integer`
- Response `200` data:
  - `id: string` (UUID)
  - `version: integer`
  - `is_active: boolean`

---

## 4.12 Analytics

### POST `/api/v1/analytics/aggregate`

- Auth: `store_manager | operation_admin`
- Query:
  - `day: string | null` (ISO datetime, optional)
- Response `200` data:
  - `date: string` (ISO datetime)
  - `transaction_volume: integer`
  - `dispute_rate: number`

### GET `/api/v1/analytics/daily-metrics`

- Auth: `store_manager | operation_admin`
- Query:
  - `date_start: string` (ISO datetime)
  - `date_end: string` (ISO datetime)
- Response `200` data: array of
  - `date: string` (ISO datetime)
  - `transaction_volume: integer`
  - `conversion_rate: number`
  - `activity_score: number`
  - `dispute_rate: number`

### GET `/api/v1/analytics/export`

- Auth: `store_manager | operation_admin`
- Query:
  - `date_start: string | null` (ISO datetime, optional)
  - `date_end: string | null` (ISO datetime, optional)
- Response `200`:
  - `Content-Type: text/csv`
  - `Content-Disposition: attachment; filename="analytics.csv"`
  - CSV columns: `date,transaction_volume,conversion_rate,activity_score,dispute_rate`

## 5. Common Error Codes

In addition to endpoint-specific errors, these may occur on protected endpoints:

- `401/missing_token`
- `401/invalid_token`
- `403/inactive_user`
- `403/password_change_required`
- `403/forbidden`
- `422/validation_error`
- `500/internal_error`