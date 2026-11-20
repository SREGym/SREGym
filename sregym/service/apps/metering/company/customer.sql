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
