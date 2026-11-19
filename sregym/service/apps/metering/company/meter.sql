-- Meter database: records the API gateways flush every second.
CREATE TABLE meter_inbox (
    id          bigserial PRIMARY KEY,
    record_id   text NOT NULL,
    tenant_id   text NOT NULL,
    metric      text NOT NULL,
    quantity    numeric(12,3) NOT NULL,
    occurred_at timestamptz NOT NULL
);
