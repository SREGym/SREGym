-- A customer's usage database (customer code): the live usage copy from the provider and the usage statement.
CREATE SEQUENCE statement_line_seq;
CREATE SEQUENCE statement_close_seq;
CREATE TABLE usage_events (
    event_id    bigint NOT NULL,
    record_id   text NOT NULL,
    tenant_id   text NOT NULL,
    metric      text NOT NULL,
    quantity    numeric(12,3) NOT NULL,
    occurred_at timestamptz NOT NULL,
    batch_id    bigint NOT NULL,
    billing_period date NOT NULL,
    line_id     bigint NOT NULL DEFAULT nextval('statement_line_seq'),
    PRIMARY KEY (:identity_columns)
) :partition_clause;
\if :partition_by_period
SELECT format('CREATE TABLE usage_events_prior PARTITION OF usage_events FOR VALUES IN (%L)',
              (:'open_period'::date - interval '1 month')::date) \gexec
CREATE TABLE usage_events_open PARTITION OF usage_events FOR VALUES IN (:'open_period');
CREATE TABLE usage_events_other PARTITION OF usage_events DEFAULT;
\endif
CREATE INDEX usage_events_line ON usage_events (line_id);

-- Every row we receive is booked once on our usage statement; a booked row
-- that is no longer in the copy is reversed.
CREATE TABLE statement_lines (
    line_id     bigint NOT NULL,
    event_id    bigint NOT NULL,
    kind        text NOT NULL CHECK (kind IN ('line', 'reversal')),
    billing_period date NOT NULL,
    tenant_id   text NOT NULL,
    metric      text NOT NULL,
    quantity    numeric(12,3) NOT NULL,
    close_id    bigint NOT NULL,
    issued_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (line_id, kind)
);

CREATE FUNCTION close_statement() RETURNS bigint LANGUAGE plpgsql SET search_path = public AS $$
DECLARE
    c bigint := nextval('statement_close_seq');
BEGIN
    -- Amend a retained booking when its usage changes. A removed booking
    -- still receives a reversal, including corrections by replacement.
    UPDATE statement_lines s
    SET billing_period = u.billing_period, tenant_id = u.tenant_id, metric = u.metric, quantity = u.quantity
    FROM usage_events u
    WHERE s.line_id = u.line_id AND s.event_id = u.event_id AND s.kind = 'line'
      AND (s.billing_period, s.tenant_id, s.metric, s.quantity)
          IS DISTINCT FROM (u.billing_period, u.tenant_id, u.metric, u.quantity)
      AND NOT EXISTS (SELECT 1 FROM statement_lines r WHERE r.line_id = s.line_id AND r.kind = 'reversal');
    INSERT INTO statement_lines (line_id, event_id, kind, billing_period, tenant_id, metric, quantity, close_id)
    SELECT u.line_id, u.event_id, 'line', u.billing_period, u.tenant_id, u.metric, u.quantity, c
    FROM usage_events u
    WHERE NOT EXISTS (SELECT 1 FROM statement_lines s WHERE s.line_id = u.line_id AND s.kind = 'line');
    INSERT INTO statement_lines (line_id, event_id, kind, billing_period, tenant_id, metric, quantity, close_id)
    SELECT s.line_id, s.event_id, 'reversal', s.billing_period, s.tenant_id, s.metric, s.quantity, c
    FROM statement_lines s
    WHERE s.kind = 'line'
      AND NOT EXISTS (SELECT 1 FROM usage_events u WHERE u.line_id = s.line_id)
      AND NOT EXISTS (SELECT 1 FROM statement_lines r WHERE r.line_id = s.line_id AND r.kind = 'reversal');
    RETURN c;
END $$;

-- Save the accepted usage at close so a resend carries the same return.
CREATE TABLE statement_returns (
    return_ref    text PRIMARY KEY,
    period_start  timestamptz NOT NULL,
    period_end    timestamptz NOT NULL,
    accepted_rows jsonb NOT NULL
);

-- Applies a corrections file from the provider: each row is deleted, and put
-- back as it is after the correction when it still belongs to us.
CREATE FUNCTION load_correction_file(p_rows jsonb, p_tenant text) RETURNS integer
LANGUAGE plpgsql SET search_path = public AS $$
DECLARE
    r jsonb;
    n integer := 0;
BEGIN
    FOR r IN SELECT value FROM jsonb_array_elements(p_rows) ORDER BY (value->>'correction_id')::bigint LOOP
        DELETE FROM usage_events WHERE event_id = (r->>'event_id')::bigint;
        IF jsonb_typeof(r->'after') = 'object' AND r->'after'->>'tenant_id' = p_tenant THEN
            INSERT INTO usage_events (event_id, record_id, tenant_id, metric, quantity, occurred_at, batch_id, billing_period)
            SELECT event_id, record_id, tenant_id, metric, quantity, occurred_at, batch_id, billing_period
            FROM jsonb_populate_record(null::usage_events, r->'after');
        END IF;
        n := n + 1;
    END LOOP;
    RETURN n;
END $$;

-- Correction files from the provider, received by subscription (customers that load them automatically).
CREATE TABLE extract_files (
    file_id     bigint PRIMARY KEY,
    ticket_id   text,
    account     text NOT NULL,
    sent_at     timestamptz NOT NULL,
    status      text NOT NULL,
    rows        jsonb NOT NULL
);
CREATE TABLE loaded_files (
    file_id     bigint PRIMARY KEY,
    loaded_at   timestamptz NOT NULL DEFAULT now()
);

-- Nightly loader: applies every ready file not loaded yet, oldest first, and records it.
CREATE FUNCTION load_ready_files() RETURNS integer LANGUAGE plpgsql SET search_path = public AS $$
DECLARE
    f extract_files;
    n integer := 0;
BEGIN
    FOR f IN SELECT * FROM extract_files e
             WHERE e.status = 'ready' AND NOT EXISTS (SELECT 1 FROM loaded_files l WHERE l.file_id = e.file_id)
             ORDER BY e.file_id LOOP
        PERFORM load_correction_file(f.rows, f.account);
        INSERT INTO loaded_files (file_id) VALUES (f.file_id);
        n := n + 1;
    END LOOP;
    RETURN n;
END $$;

CREATE TABLE allocation_receipts (
    close_ref   text NOT NULL,
    revision    integer NOT NULL,
    tenant_id   text NOT NULL,
    PRIMARY KEY (close_ref, revision, tenant_id)
);

ALTER TABLE statement_returns
    ADD COLUMN close_ref text,
    ADD COLUMN batch_revision integer,
    ADD COLUMN statement_read_at timestamptz,
    ADD COLUMN signed_at timestamptz,
    ADD COLUMN statement_digest text;

CREATE EXTENSION pgcrypto;
CREATE EXTENSION dblink;
CREATE TABLE return_sender (account text PRIMARY KEY, signing_key text NOT NULL);

-- The earlier dispute uses the same signing and delivery workflow as later closes.
CREATE FUNCTION save_statement_return(p_ref text, p_start timestamptz, p_end timestamptz, p_disputed bigint[])
RETURNS void LANGUAGE sql SET search_path = public AS $$
    INSERT INTO statement_returns (return_ref, period_start, period_end, accepted_rows, signed_at, statement_digest)
    SELECT p_ref, p_start, p_end, r.accepted, clock_timestamp(),
           encode(hmac(jsonb_build_array(s.account, p_ref, null::text, null::integer, extract(epoch from p_start),
                                         extract(epoch from p_end), r.accepted)::text, s.signing_key, 'sha256'), 'hex')
    FROM return_sender s CROSS JOIN LATERAL (
        SELECT coalesce(jsonb_agg(jsonb_build_object('event_id', u.event_id, 'metric', u.metric, 'quantity', u.quantity)
                                 ORDER BY u.event_id), '[]'::jsonb) AS accepted
        FROM usage_events u JOIN statement_lines l ON l.line_id = u.line_id AND l.kind = 'line'
        WHERE u.occurred_at >= p_start AND u.occurred_at < p_end AND NOT (u.event_id = ANY (p_disputed))
    ) r;
$$;

-- Build the file against the same transaction snapshot as its batch header.
CREATE FUNCTION file_allocation_return(p_ref text, p_close text, p_start timestamptz, p_end timestamptz,
                                       p_disputed bigint[], p_read_at timestamptz)
RETURNS void LANGUAGE plpgsql SET search_path = public AS $$
DECLARE
    revision integer;
    accepted jsonb;
    sender return_sender;
BEGIN
    IF current_setting('transaction_isolation') <> 'repeatable read' THEN
        RAISE EXCEPTION 'statement returns require repeatable read';
    END IF;
    SELECT max(r.revision) INTO revision FROM allocation_receipts r WHERE r.close_ref = p_close;
    IF revision IS NULL THEN
        RAISE EXCEPTION 'no batch receipt for %', p_close;
    END IF;
    SELECT coalesce(jsonb_agg(jsonb_build_object('event_id', event_id, 'metric', metric, 'quantity', quantity)
                             ORDER BY event_id), '[]'::jsonb)
    INTO accepted
    FROM usage_events
    WHERE occurred_at >= p_start AND occurred_at < p_end AND NOT (event_id = ANY (p_disputed));
    SELECT * INTO STRICT sender FROM return_sender;
    INSERT INTO statement_returns (return_ref, period_start, period_end, accepted_rows, close_ref, batch_revision,
                                   statement_read_at, signed_at, statement_digest)
    VALUES (p_ref, p_start, p_end, accepted, p_close, revision, p_read_at, clock_timestamp(),
            encode(hmac(jsonb_build_array(sender.account, p_ref, p_close, revision, extract(epoch from p_start),
                                         extract(epoch from p_end), accepted)::text, sender.signing_key, 'sha256'), 'hex'));
END $$;

CREATE TABLE allocation_settlements (
    close_ref   text NOT NULL,
    revision    integer NOT NULL,
    line_count  integer NOT NULL,
    cents       bigint NOT NULL,
    settled_at  timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (close_ref, revision)
);

CREATE FUNCTION send_statement_returns(p_ref text DEFAULT NULL) RETURNS SETOF bigint LANGUAGE sql SET search_path = public AS $$
    SELECT d.delivery_id
    FROM dblink('statement_support', format('select * from receive_statement_returns(%L, %L::jsonb, %L::jsonb)',
                 (SELECT account FROM return_sender),
                 (SELECT coalesce(jsonb_agg(r), '[]'::jsonb) FROM statement_returns r WHERE p_ref IS NULL OR r.return_ref = p_ref),
                 (SELECT coalesce(jsonb_agg(s), '[]'::jsonb) FROM allocation_settlements s)))
         AS d(delivery_id bigint);
$$;
