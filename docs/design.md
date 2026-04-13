# design.md

## 1. Implementation Strategy Scope

This document defines implementation strategies only.

Strategy scope:
- Auth with JWT and lockout policy.
- Retail POS flow: product search -> calculate -> checkout -> settle.
- Refund flow with idempotency.
- Project lifecycle with version diffing and attachment fingerprinting.
- Notification stream and read receipts.
- Daily analytics and export.

Out of scope:
- Cloud dependencies.
- Multi-node deployment.
- UI implementation.

---

## 2. Code Layout Strategy (Service + SQLAlchemy)

Implementation will use this package structure:

```text
repo/app/
  main.py
  api/
    v1/
      auth.py
      products.py
      orders.py
      after_sales.py
      projects.py
      notifications.py
      analytics.py
  domain/
    auth_service.py
    pricing_service.py
    order_service.py
    refund_service.py
    project_service.py
    notification_service.py
    analytics_service.py
  models/
    user.py
    product.py
    order.py
    promotion.py
    refund.py
    project.py
    notification.py
    audit.py
  repositories/
    user_repo.py
    product_repo.py
    order_repo.py
    refund_repo.py
    project_repo.py
    notification_repo.py
    analytics_repo.py
  infra/
    db.py
    security.py
    event_bus.py
    scheduler.py
    files.py
```

Rule: interface modules only validate input and call one domain service entrypoint. All business logic stays in `domain/*`.

---

## 3. Database Implementation (PostgreSQL)

### 3.1 Tables

Implement these tables first:
- `users`: `id`, `username` (unique), `password_hash`, `failed_attempts`, `lockout_until`, `role`, `created_at`.
- `products`: `id`, `barcode` (indexed), `code` (indexed), `pinyin` (indexed), `name`, `price`, `stock`, `updated_at`.
- `orders`: `id`, `status`, `subtotal`, `discount_total`, `payable_total`, `created_at`, `expires_at`, `settled_at`.
- `order_lines`: `id`, `order_id` (fk), `product_id` (fk), `unit_price`, `quantity`, `line_subtotal`, `line_discount`.
- `order_payments`: `id`, `order_id` (fk), `method`, `amount`.
- `promotion_rules`: `id`, `type`, `priority`, `threshold_json`, `benefit_json`, `active`.
- `refund_requests`: `id`, `idempotency_key` (unique), `order_id` (fk), `status`, `refund_total`, `created_at`.
- `refund_lines`: `id`, `refund_id` (fk), `order_line_id` (fk), `quantity`, `amount`, `reason`.
- `projects`: `id`, `owner_id`, `status`, `current_version`, `created_at`, `updated_at`.
- `project_versions`: `id`, `project_id` (fk), `version_num`, `payload_json`, `diff_summary_json`, `created_at`.
- `project_attachments`: `id`, `project_id` (fk), `file_path`, `mime_type`, `sha256`, `size_bytes`.
- `notifications`: `id`, `recipient_id`, `event_type`, `object_type`, `object_id`, `content_json`, `delivered_at`, `read_at`.
- `notification_throttle`: `id`, `recipient_id`, `event_type`, `object_id`, `last_sent_at`, unique on (`recipient_id`, `event_type`, `object_id`).
- `audit_logs`: `id`, `actor_id`, `operation`, `resource_type`, `resource_id`, `payload_json`, `ip_address`, `created_at`.

### 3.2 Constraints and Indexes

Mandatory constraints:
- Unique: `users.username`, `refund_requests.idempotency_key`.
- Check: all money fields `>= 0`.
- Check: `refund_lines.quantity > 0`.
- FK cascade rules:
  - `orders -> order_lines` on delete restrict.
  - `projects -> project_versions` on delete cascade.

Mandatory indexes:
- `products(barcode)`, `products(code)`, `products(pinyin)`.
- `orders(status, created_at)`.
- `notifications(recipient_id, read_at)`.
- `project_versions(project_id, version_num)`.

---

## 4. Domain Strategy Implementation

### 4.1 Authentication and Access Strategy

- Flow:
  1. Load user by username.
  2. If `lockout_until > now`, return `423`.
  3. Verify password hash.
  4. On failure: increment `failed_attempts`; if `>= 5`, set `lockout_until = now + 15m`; write audit log; return `401` or `423`.
  5. On success: reset counters, issue JWT (`exp=3600`), write audit log.

### 4.2 Retail and Checkout Strategy

1. Product discovery strategy:
   - Use indexed matching on barcode, internal code, and pinyin.
2. Pricing strategy:
   - Load product price snapshots for the transaction.
   - Compute subtotal.
   - Apply promotions by ascending `priority`.
   - Persist both pre-discount and post-discount totals.
3. Order creation strategy:
  1. Reuse pricing result.
  2. Create `orders` + `order_lines` in one transaction (`status=PENDING`, `expires_at=now+30m`).
  3. Publish `order.pending.created` event for auto-void scheduling.
4. Settlement strategy:
  1. Lock order row (`SELECT ... FOR UPDATE`).
  2. Validate order is `PENDING` and not expired.
  3. Validate `sum(payments) == payable_total`.
  4. Insert `order_payments`, set order `COMPLETED`, set `settled_at`.
  5. Write audit log.

### 4.3 After-Sales Strategy

- Use idempotency-first execution:
  1. If existing row with same `idempotency_key`, return stored result.
  2. Lock target order and lines.
  3. Validate within 7 days and quantities/amounts not exceeding original.
  4. Insert `refund_requests` + `refund_lines`.
  5. Update stock and write audit log in same transaction.

### 4.4 Project Lifecycle Strategy

- Creation strategy:
  1. Validate MIME: `application/pdf`, `image/jpeg`, `image/png`.
  2. Save file to local storage path.
  3. Compute SHA-256 and store in `project_attachments`.
  4. Insert `projects(status=DRAFT, current_version=1)` and first `project_versions` row.
- Transition strategy:
  1. Validate state transition map.
  2. If data changes, increment version and persist new `project_versions`.
  3. Compute `diff_summary_json` against previous version.
  4. Write audit log.
- Diff strategy:
  - Read two stored version payloads and compute field-level old/new pairs.

### 4.5 Notification Strategy

- Event bus: in-process pub/sub in `infra/event_bus.py`.
- Delivery strategy:
  - Register subscriber per authenticated user session.
  - Push messages produced by `NotificationService.publish(...)`.
- Throttling rule (1 per 10 minutes/object):
  1. Check `notification_throttle` for `(recipient_id, event_type, object_id)`.
  2. If `now - last_sent_at < 10m`, skip.
  3. Else insert notification + update throttle row.
- Read receipt strategy:
  - Set `read_at=now` only if null.

### 4.6 Analytics Strategy

- Aggregate strategy:
  - Aggregate from orders/refunds by date window.
  - Metrics: `transaction_volume`, `conversion_rate`, `dispute_rate`.
- Export strategy:
  - Generate CSV in memory for downstream download handlers.

---

## 5. Background Jobs

Implement two scheduled jobs:

1. `order_auto_void_job` (runs every minute):
   - Query `orders` where `status=PENDING` and `expires_at < now`.
   - Update to `VOIDED` in batches.

2. `notification_retry_job` (optional if websocket push fails):
   - Retry undelivered notifications with backoff.

Use APScheduler running inside the service container for single-node deployment.

---

## 6. Security Implementation

- Passwords: Argon2 hash + verify via `infra/security.py`.
- JWT: HS256, 1-hour expiry, role claim included.
- PII fields (contact/id): encrypt before persistence with AES-256 helper in `infra/security.py`.
- File safety: reject unsupported MIME and max upload size > 20MB.

---

## 7. Error Handling Strategy

All handlers return one of:
- Success: `{ "status": "success", "data": ..., "message": ... }`
- Error: `{ "status": "error", "code": "...", "message": "...", "details": [...] }`

HTTP mappings used in implementation:
- `400`: validation and business rule violations.
- `401`: bad credentials / invalid token.
- `403`: permission denied.
- `404`: missing resource.
- `409`: refund amount/quantity conflicts.
- `423`: login lockout.

---

## 8. Build Order

Implementation sequence:
1. Create SQLAlchemy models + Alembic migration for all tables.
2. Implement repositories with transaction boundaries.
3. Implement domain services (`auth`, `pricing`, `order`, `refund`, `project`).
4. Expose interface handlers and bind DTO schemas.
5. Add event bus + websocket notification flow.
6. Add scheduled jobs.
7. Add audit hooks on critical paths.
8. Add integration tests for:
   - Login lockout.
   - Promotion pipeline.
   - Refund idempotency.
   - Project version diff.
   - Notification 10-minute throttle.