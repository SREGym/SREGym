-- Payments database: card captures of sent invoices (the processor's settlement import) and refunds.
CREATE TABLE captures (
    capture_id  bigserial PRIMARY KEY,
    account     text NOT NULL,
    invoice_id  bigint NOT NULL UNIQUE,
    amount      numeric(14,2) NOT NULL CHECK (amount > 0),
    captured_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE refunds (
    refund_id   bigserial PRIMARY KEY,
    capture_id  bigint NOT NULL REFERENCES captures,
    amount      numeric(14,2) NOT NULL CHECK (amount > 0),
    memo        text NOT NULL,
    issued_by   text NOT NULL,
    refunded_at timestamptz NOT NULL DEFAULT now()
);
