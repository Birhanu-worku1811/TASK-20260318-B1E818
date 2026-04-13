-- Shift scheduling and permission lifecycle domain additions.
-- PostgreSQL migration companion for ORM changes.

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'shiftstatus') THEN
    CREATE TYPE shiftstatus AS ENUM ('SCHEDULED', 'ACTIVE', 'COMPLETED', 'CANCELLED');
  END IF;
END
$$;

CREATE TABLE IF NOT EXISTS permission_change_logs (
  id UUID PRIMARY KEY,
  target_user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  actor_user_id UUID NOT NULL REFERENCES users(id),
  operation VARCHAR(32) NOT NULL,
  from_role roletype NULL,
  to_role roletype NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS shift_schedules (
  id UUID PRIMARY KEY,
  assigned_user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_by_user_id UUID NOT NULL REFERENCES users(id),
  starts_at TIMESTAMPTZ NOT NULL,
  ends_at TIMESTAMPTZ NOT NULL,
  status shiftstatus NOT NULL DEFAULT 'SCHEDULED',
  note VARCHAR(255) NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT ck_shift_window CHECK (starts_at < ends_at)
);
