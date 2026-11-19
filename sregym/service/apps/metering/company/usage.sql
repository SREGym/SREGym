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
    batch_id    bigint NOT NULL
);
CREATE INDEX usage_events_tenant ON usage_events (tenant_id, occurred_at);
CREATE INDEX usage_events_batch ON usage_events (batch_id);
-- Feeds filter on tenant_id, so updates and deletes need the whole old row.
ALTER TABLE usage_events REPLICA IDENTITY FULL;

CREATE TABLE usage_corrections (
    correction_id bigserial PRIMARY KEY,
    event_id     bigint NOT NULL,
    kind         text NOT NULL CHECK (kind IN ('void', 'reduce', 'move')),
    before       jsonb NOT NULL,
    after        jsonb,
    reason       text NOT NULL,
    requested_by text NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now()
);

-- Used by the support console. Voids delete the event, reductions lower its
-- quantity, moves give it to the account that really used it.
CREATE FUNCTION correct_usage(p_event bigint, p_kind text, p_value text, p_reason text, p_by text)
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
    ELSE
        RAISE EXCEPTION 'unknown correction %', p_kind;
    END IF;
    INSERT INTO usage_corrections (event_id, kind, before, after, reason, requested_by)
    VALUES (p_event, p_kind, to_jsonb(old_row), to_jsonb(new_row), p_reason, p_by)
    RETURNING correction_id INTO id;
    RETURN id;
END $$;

CREATE SCHEMA ops;
CREATE TABLE ops.pubsync_log (
    id          bigserial PRIMARY KEY,
    at          timestamptz NOT NULL DEFAULT now(),
    revision    text NOT NULL,
    publication text NOT NULL,
    change      text NOT NULL
);
