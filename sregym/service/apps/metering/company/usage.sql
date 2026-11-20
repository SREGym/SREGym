-- Usage database on core: metered API usage, its corrections, and the feeds built on it.
CREATE TABLE tenants (
    tenant_id   text PRIMARY KEY,
    name        text NOT NULL,
    plan        text NOT NULL,
    streams_usage boolean NOT NULL DEFAULT false
);

-- The gateways' inbox lives in the meter database.
CREATE EXTENSION postgres_fdw;
CREATE SERVER meter FOREIGN DATA WRAPPER postgres_fdw OPTIONS (host '/var/run/postgresql', dbname 'meter');
CREATE FOREIGN TABLE meter_inbox (
    id          bigint NOT NULL,
    record_id   text NOT NULL,
    tenant_id   text NOT NULL,
    metric      text NOT NULL,
    quantity    numeric(12,3) NOT NULL,
    occurred_at timestamptz NOT NULL
) SERVER meter;

CREATE TABLE ingest_batches (
    batch_id    bigserial PRIMARY KEY,
    source      text NOT NULL,
    started_at  timestamptz NOT NULL DEFAULT clock_timestamp(),
    records     integer
);

CREATE TABLE usage_events (
    event_id    bigserial PRIMARY KEY,
    record_id   text NOT NULL UNIQUE,
    tenant_id   text NOT NULL REFERENCES tenants,
    metric      text NOT NULL,
    quantity    numeric(12,3) NOT NULL CHECK (quantity >= 0),
    occurred_at timestamptz NOT NULL,
    batch_id    bigint NOT NULL,
    billing_period date NOT NULL
);
CREATE INDEX usage_events_tenant ON usage_events (tenant_id, occurred_at);
CREATE INDEX usage_events_batch ON usage_events (batch_id);
-- Feeds filter on tenant_id, so updates and deletes need the whole old row.
ALTER TABLE usage_events REPLICA IDENTITY FULL;

-- Keep the period in the published row so subscribers can route by it.
CREATE FUNCTION set_billing_period() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.billing_period := date_trunc('month', NEW.occurred_at AT TIME ZONE 'UTC')::date;
    RETURN NEW;
END $$;
CREATE TRIGGER set_billing_period BEFORE INSERT OR UPDATE OF occurred_at ON usage_events
    FOR EACH ROW EXECUTE FUNCTION set_billing_period();

CREATE TABLE usage_corrections (
    correction_id bigserial PRIMARY KEY,
    event_id     bigint NOT NULL,
    kind         text NOT NULL CHECK (kind IN ('void', 'reduce', 'move', 'redate')),
    before       jsonb NOT NULL,
    after        jsonb,
    reason       text NOT NULL,
    requested_by text NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    batch        text
);

-- Used by the support console. Voids delete the event, reductions lower its
-- quantity, moves change the account and re-dates correct the usage timestamp.
CREATE FUNCTION correct_usage(p_event bigint, p_kind text, p_value text, p_reason text, p_by text,
                              p_batch text DEFAULT NULL)
RETURNS bigint LANGUAGE plpgsql AS $$
DECLARE
    old_row usage_events;
    new_row usage_events;
    id bigint;
BEGIN
    SELECT * INTO old_row FROM usage_events WHERE event_id = p_event FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'usage event % not found', p_event;
    END IF;
    IF p_kind = 'void' THEN
        DELETE FROM usage_events WHERE event_id = p_event;
    ELSIF p_kind = 'reduce' THEN
        IF p_value::numeric >= old_row.quantity THEN
            RAISE EXCEPTION 'reduction must lower the quantity';
        END IF;
        UPDATE usage_events SET quantity = p_value::numeric WHERE event_id = p_event RETURNING * INTO new_row;
    ELSIF p_kind = 'move' THEN
        UPDATE usage_events SET tenant_id = p_value WHERE event_id = p_event RETURNING * INTO new_row;
    ELSIF p_kind = 'redate' THEN
        UPDATE usage_events SET occurred_at = p_value::timestamptz WHERE event_id = p_event RETURNING * INTO new_row;
    ELSE
        RAISE EXCEPTION 'unknown correction %', p_kind;
    END IF;
    INSERT INTO usage_corrections (event_id, kind, before, after, reason, requested_by, batch)
    VALUES (p_event, p_kind, to_jsonb(old_row), to_jsonb(new_row), p_reason, p_by, p_batch)
    RETURNING correction_id INTO id;
    RETURN id;
END $$;

-- Several corrections from one review, applied together or not at all.
-- p_items: [{"event": 123, "kind": "move", "value": "garnet", "reason": "..."}, ...]
CREATE FUNCTION correct_usage_batch(p_batch text, p_items jsonb, p_by text)
RETURNS integer LANGUAGE plpgsql AS $$
DECLARE
    item jsonb;
    n integer := 0;
BEGIN
    FOR item IN SELECT value FROM jsonb_array_elements(p_items) LOOP
        PERFORM correct_usage((item->>'event')::bigint, item->>'kind', item->>'value', item->>'reason', p_by, p_batch);
        n := n + 1;
    END LOOP;
    RETURN n;
END $$;

-- Usage keyed in by support when a meter record never arrived.
CREATE FUNCTION enter_usage(p_tenant text, p_metric text, p_quantity numeric, p_occurred_at timestamptz,
                            p_ticket text, p_by text)
RETURNS bigint LANGUAGE plpgsql AS $$
DECLARE
    batch bigint;
    event bigint;
BEGIN
    INSERT INTO ingest_batches (source, records) VALUES ('support-console', 1) RETURNING batch_id INTO batch;
    INSERT INTO usage_events (record_id, tenant_id, metric, quantity, occurred_at, batch_id)
    VALUES ('manual-' || p_ticket, p_tenant, p_metric, p_quantity, p_occurred_at, batch)
    RETURNING event_id INTO event;
    RAISE NOTICE 'usage event % entered by % for ticket %', event, p_by, p_ticket;
    RETURN event;
END $$;

CREATE SCHEMA ops;
CREATE TABLE ops.pubsync_log (
    id          bigserial PRIMARY KEY,
    at          timestamptz NOT NULL DEFAULT now(),
    revision    text NOT NULL,
    publication text NOT NULL,
    change      text NOT NULL
);

-- A receipt travels on the same subscription as the usage in its allocation batch.
CREATE TABLE allocation_receipts (
    close_ref   text NOT NULL,
    revision    integer NOT NULL,
    tenant_id   text NOT NULL,
    PRIMARY KEY (close_ref, revision, tenant_id)
);
ALTER TABLE allocation_receipts REPLICA IDENTITY FULL;

CREATE EXTENSION pg_walinspect;
CREATE TABLE ops.allocation_batches (
    close_ref   text NOT NULL,
    revision    integer NOT NULL,
    xid         xid NOT NULL DEFAULT pg_current_xact_id()::xid,
    commit_lsn  pg_lsn,
    PRIMARY KEY (close_ref, revision)
);
