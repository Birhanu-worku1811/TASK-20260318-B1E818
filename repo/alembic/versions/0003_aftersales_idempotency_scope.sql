-- Harden after-sales idempotency scope/fingerprint.
-- PostgreSQL migration companion for ORM changes.

ALTER TABLE after_sales_orders
  ADD COLUMN IF NOT EXISTS idempotency_scope VARCHAR(120);

ALTER TABLE after_sales_orders
  ADD COLUMN IF NOT EXISTS request_fingerprint VARCHAR(64);

UPDATE after_sales_orders
SET idempotency_scope = COALESCE(idempotency_scope, idempotency_key),
    request_fingerprint = COALESCE(request_fingerprint, md5(COALESCE(reason, '') || ':' || COALESCE(type, '')))
WHERE idempotency_scope IS NULL OR request_fingerprint IS NULL;

ALTER TABLE after_sales_orders
  ALTER COLUMN idempotency_scope SET NOT NULL;

ALTER TABLE after_sales_orders
  ALTER COLUMN request_fingerprint SET NOT NULL;

DO $$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'uq_after_sales_idempotency'
  ) THEN
    ALTER TABLE after_sales_orders DROP CONSTRAINT uq_after_sales_idempotency;
  END IF;
END
$$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'uq_after_sales_idempotency_scope'
  ) THEN
    ALTER TABLE after_sales_orders
      ADD CONSTRAINT uq_after_sales_idempotency_scope UNIQUE (idempotency_scope);
  END IF;
END
$$;
