-- A customer's usage database (customer code): the live usage copy from the provider and the usage statement.
CREATE SEQUENCE statement_line_seq;
CREATE SEQUENCE statement_close_seq;
CREATE TABLE usage_events (
    event_id    bigint PRIMARY KEY,
    record_id   text NOT NULL,
    tenant_id   text NOT NULL,
    metric      text NOT NULL,
    quantity    numeric(12,3) NOT NULL,
    occurred_at timestamptz NOT NULL,
    batch_id    bigint NOT NULL,
    line_id     bigint NOT NULL DEFAULT nextval('statement_line_seq')
);
CREATE INDEX usage_events_line ON usage_events (line_id);

-- Every row we receive is booked once on our usage statement; a booked row
-- that is no longer in the copy is reversed.
CREATE TABLE statement_lines (
    line_id     bigint NOT NULL,
    event_id    bigint NOT NULL,
    kind        text NOT NULL CHECK (kind IN ('line', 'reversal')),
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
    INSERT INTO statement_lines (line_id, event_id, kind, tenant_id, metric, quantity, close_id)
    SELECT u.line_id, u.event_id, 'line', u.tenant_id, u.metric, u.quantity, c
    FROM usage_events u
    WHERE NOT EXISTS (SELECT 1 FROM statement_lines s WHERE s.line_id = u.line_id AND s.kind = 'line');
    INSERT INTO statement_lines (line_id, event_id, kind, tenant_id, metric, quantity, close_id)
    SELECT s.line_id, s.event_id, 'reversal', s.tenant_id, s.metric, s.quantity, c
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

CREATE FUNCTION save_statement_return(p_ref text, p_start timestamptz, p_end timestamptz, p_disputed bigint[])
RETURNS void LANGUAGE sql SET search_path = public AS $$
    INSERT INTO statement_returns
    SELECT p_ref, p_start, p_end,
           coalesce(jsonb_agg(jsonb_build_object('event_id', u.event_id, 'metric', u.metric, 'quantity', u.quantity)
                              ORDER BY u.event_id), '[]'::jsonb)
    FROM usage_events u JOIN statement_lines s ON s.line_id = u.line_id AND s.kind = 'line'
    WHERE u.occurred_at >= p_start AND u.occurred_at < p_end AND NOT (u.event_id = ANY (p_disputed));
$$;

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
            INSERT INTO usage_events (event_id, record_id, tenant_id, metric, quantity, occurred_at, batch_id)
            SELECT event_id, record_id, tenant_id, metric, quantity, occurred_at, batch_id
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
