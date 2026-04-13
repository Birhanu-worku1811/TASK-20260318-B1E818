# Offline Platform API

FastAPI + SQLAlchemy + PostgreSQL backend for offline retail checkout and entrepreneurship project incubation operations.

## PostgreSQL mode (recommended / accepted runtime)

### Local startup (native Postgres)

1) Start PostgreSQL and create database/user matching your `DATABASE_URL`.
2) Configure env (example):

```bash
export APP_ENV=dev
export DATABASE_URL="postgresql+psycopg://postgres:postgres@localhost:5432/offline_platform"
export ALLOW_DEV_SQLITE_OVERRIDE=false
```

3) Create venv, install, run API:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
uvicorn app.main:app --reload --reload-dir app --reload-exclude ".venv/*"
```

### Docker startup (PostgreSQL)

```bash
docker compose up --build
```

This brings up `postgres` and `api` with PostgreSQL-backed runtime settings from `.env.example`.

### Explicit SQLite dev override (only for local development)

Runtime now fails fast unless PostgreSQL is used, except when this explicit override is enabled:

```bash
export APP_ENV=dev
export DATABASE_URL="sqlite+pysqlite:///./offline_platform.db"
export ALLOW_DEV_SQLITE_OVERRIDE=true
```

## Swagger / OpenAPI

- Swagger UI: `http://127.0.0.1:8000/docs`
- Alias: `http://127.0.0.1:8000/swagger`
- ReDoc: `http://127.0.0.1:8000/redoc`
- OpenAPI JSON: `http://127.0.0.1:8000/openapi.json`

## Seed admin

```bash
python scripts/seed_admin.py
```

## Mandatory tests

```bash
./run_tests.sh
```

## Operational domain routes

- **Shift scheduling**
  - `POST /api/v1/shifts` (operation_admin)
  - `GET /api/v1/shifts` (operation_admin)
  - `GET /api/v1/shifts/me` (authenticated user)
  - `GET /api/v1/shifts/{shift_id}` (assigned user or operation_admin)
  - `PATCH /api/v1/shifts/{shift_id}` (operation_admin)
  - `PATCH /api/v1/shifts/{shift_id}/status` (operation_admin)
- **Permission lifecycle (strict admin)**
  - `POST /api/v1/permissions/grant`
  - `POST /api/v1/permissions/revoke`
  - `PATCH /api/v1/permissions/bindings/{binding_id}`
- **Receipt printing**
  - `POST /api/v1/orders/{order_id}/receipt/print` (cashier / store_manager / operation_admin, settled orders only)

## Receipt printer configuration

- `RECEIPT_PRINTER_BACKEND`: `noop` (default), `network`, or `device`
- `RECEIPT_PRINTER_HOST`: printer host for `network`
- `RECEIPT_PRINTER_PORT`: printer port for `network` (default `9100`)
- `RECEIPT_PRINTER_DEVICE_PATH`: device path for `device` backend
