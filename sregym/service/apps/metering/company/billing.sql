-- Billing database: rates every usage change it receives from core into the ledger.
CREATE TABLE usage_events (
    event_id    bigint PRIMARY KEY,
    record_id   text NOT NULL,
    tenant_id   text NOT NULL,
    metric      text NOT NULL,
    quantity    numeric(12,3) NOT NULL,
    occurred_at timestamptz NOT NULL,
    batch_id    bigint NOT NULL
);

CREATE TABLE price_book (
    metric      text PRIMARY KEY,
    unit_price  numeric(12,6) NOT NULL,
    band        text
);
INSERT INTO price_book VALUES ('api_calls', 0.002), ('tokens_k', 0.0150), ('storage_gb_h', 0.0400);

CREATE TYPE rate_info AS (unit_price numeric);

CREATE FUNCTION rate_snapshot(p price_book) RETURNS rate_info LANGUAGE sql STABLE AS $$
    SELECT ROW(p.unit_price)::rate_info
$$;

CREATE TABLE ledger_lines (
    line_id     bigint PRIMARY KEY,
    account     text NOT NULL,
    metric      text NOT NULL,
    quantity    numeric(12,3) NOT NULL,
    amount      numeric(14,4) NOT NULL,
    rated_at    timestamptz NOT NULL,
    rate        rate_info NOT NULL
);

CREATE FUNCTION rate_usage() RETURNS trigger LANGUAGE plpgsql SET search_path = public AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        DELETE FROM ledger_lines WHERE line_id = OLD.event_id;
        RETURN OLD;
    END IF;
    INSERT INTO ledger_lines (line_id, account, metric, quantity, amount, rated_at, rate)
    SELECT NEW.event_id, NEW.tenant_id, NEW.metric, NEW.quantity, round(NEW.quantity * p.unit_price, 4),
           statement_timestamp(), rate_snapshot(p)
    FROM price_book p WHERE p.metric = NEW.metric
    -- Quantity and account corrections keep the band from the original rating.
    ON CONFLICT (line_id) DO UPDATE
        SET account = EXCLUDED.account, metric = EXCLUDED.metric, quantity = EXCLUDED.quantity,
            amount = EXCLUDED.amount, rate.unit_price = (EXCLUDED.rate).unit_price;
    RETURN NEW;
END $$;

CREATE TRIGGER rate_usage AFTER INSERT OR UPDATE OR DELETE ON usage_events
    FOR EACH ROW EXECUTE FUNCTION rate_usage();
-- usage_events is written by the subscription from core.
ALTER TABLE usage_events ENABLE ALWAYS TRIGGER rate_usage;

CREATE SCHEMA ops;
CREATE TABLE ops.billing_releases (
    revision    text PRIMARY KEY,
    released_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE ops.pubsync_log (
    id          bigserial PRIMARY KEY,
    at          timestamptz NOT NULL DEFAULT now(),
    revision    text NOT NULL,
    publication text NOT NULL,
    change      text NOT NULL
);
