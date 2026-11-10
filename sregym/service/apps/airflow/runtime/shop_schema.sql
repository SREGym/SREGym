CREATE SEQUENCE IF NOT EXISTS order_number START 1001;

CREATE TABLE IF NOT EXISTS orders (
    order_id       text PRIMARY KEY,
    sku            text NOT NULL,
    qty            integer NOT NULL,
    amount_cents   integer NOT NULL,
    placed_at      timestamptz NOT NULL DEFAULT now(),
    sent_at        timestamptz,
    sqs_message_id text
);

CREATE TABLE IF NOT EXISTS shipments (
    shipment_id    bigserial PRIMARY KEY,
    order_id       text NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    dag_run_id     text,
    sqs_message_id text
);
CREATE INDEX IF NOT EXISTS shipments_order_id ON shipments (order_id);

CREATE TABLE IF NOT EXISTS settlement_reports (
    report_id      bigserial PRIMARY KEY,
    created_at     timestamptz NOT NULL DEFAULT now(),
    dag_run_id     text,
    shipped_today  integer,
    full_reconcile boolean
);

CREATE TABLE IF NOT EXISTS ops_notifications (
    id          bigserial PRIMARY KEY,
    received_at timestamptz NOT NULL DEFAULT now(),
    channel     text NOT NULL,
    dag_id      text,
    run_id      text,
    payload     jsonb
);
