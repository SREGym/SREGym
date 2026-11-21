-- Support database: customer tickets, their notes, and the correction files sent to customers.
CREATE TABLE tickets (
    ticket_id   text PRIMARY KEY,
    account     text,
    opened_at   timestamptz NOT NULL DEFAULT now(),
    subject     text NOT NULL,
    status      text NOT NULL DEFAULT 'open'
);

CREATE TABLE ticket_notes (
    ticket_id   text NOT NULL REFERENCES tickets,
    at          timestamptz NOT NULL DEFAULT now(),
    author      text NOT NULL,
    body        text NOT NULL
);
CREATE INDEX ticket_notes_ticket ON ticket_notes (ticket_id, at);

-- Files of usage corrections for customers to apply on their side.
-- rows: [{"correction_id", "event_id", "kind", "before", "after"}, ...]
-- status: 'sent' (sent by hand), 'ready' (waiting for the customer to pick it up), 'loaded' (picked up).
CREATE TABLE extract_files (
    file_id     bigserial PRIMARY KEY,
    ticket_id   text REFERENCES tickets,
    account     text NOT NULL,
    sent_at     timestamptz NOT NULL DEFAULT now(),
    status      text NOT NULL DEFAULT 'ready',
    rows        jsonb NOT NULL
);
CREATE INDEX extract_files_account ON extract_files (account, status);

-- Each delivery is queued, including retries of the same customer return.
CREATE TABLE statement_return_inbox (
    delivery_id   bigserial PRIMARY KEY,
    account       text NOT NULL,
    return_ref    text NOT NULL,
    period_start  timestamptz NOT NULL,
    period_end    timestamptz NOT NULL CHECK (period_end > period_start),
    accepted_rows jsonb NOT NULL CHECK (jsonb_typeof(accepted_rows) = 'array'),
    received_at   timestamptz NOT NULL DEFAULT now(),
    imported_at   timestamptz
);

-- Allocation approvals and close eligibility are recorded separately from file delivery.
CREATE TABLE allocation_closes (
    close_ref          text PRIMARY KEY,
    ticket_id          text NOT NULL REFERENCES tickets,
    cutoff             timestamptz NOT NULL,
    eligible_revision  integer NOT NULL,
    larkspur_reader    text NOT NULL DEFAULT 'larkspur_live'
);

CREATE TABLE allocation_approvals (
    close_ref   text NOT NULL REFERENCES allocation_closes,
    revision    integer NOT NULL,
    approved_at timestamptz NOT NULL,
    effective_for text NOT NULL,
    PRIMARY KEY (close_ref, revision)
);

CREATE TABLE statement_waiver_approvals (
    account      text NOT NULL,
    close_ref    text NOT NULL REFERENCES allocation_closes,
    line_id      bigint NOT NULL,
    approved_at  timestamptz NOT NULL,
    reason       text NOT NULL,
    PRIMARY KEY (account, close_ref, line_id)
);

ALTER TABLE statement_return_inbox
    ADD COLUMN close_ref text REFERENCES allocation_closes,
    ADD COLUMN batch_revision integer,
    ADD COLUMN statement_read_at timestamptz,
    ADD COLUMN signed_at timestamptz,
    ADD COLUMN statement_digest text;

-- Customer confirmation of posted usage at settlement, independently of the filed return.
CREATE TABLE allocation_settlements (
    close_ref   text NOT NULL REFERENCES allocation_closes,
    account     text NOT NULL,
    revision    integer NOT NULL,
    line_count  integer NOT NULL,
    cents       bigint NOT NULL,
    settled_at  timestamptz NOT NULL,
    PRIMARY KEY (close_ref, account, revision)
);

-- Acknowledgement to the sender is distinct from the customer's settlement confirmation.
CREATE TABLE statement_return_acknowledgements (
    account         text NOT NULL,
    return_ref      text NOT NULL,
    acknowledged_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (account, return_ref)
);

CREATE EXTENSION pgcrypto;
CREATE TABLE return_signing_keys (
    account text PRIMARY KEY,
    signing_key text NOT NULL
);

CREATE FUNCTION acknowledge_statement_return(p_account text, p_ref text)
RETURNS void LANGUAGE plpgsql SET search_path = public AS $$
DECLARE
    digest text;
BEGIN
    PERFORM 1 FROM return_signing_keys WHERE account = p_account FOR UPDATE;
    SELECT statement_digest INTO digest FROM statement_return_inbox
    WHERE account = p_account AND return_ref = p_ref AND imported_at IS NOT NULL
    ORDER BY imported_at DESC LIMIT 1;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'no completed import for % / %', p_account, p_ref;
    END IF;
    -- An identical retry arriving during finance's import has the same decision.
    UPDATE statement_return_inbox SET imported_at = now()
    WHERE account = p_account AND return_ref = p_ref AND imported_at IS NULL AND statement_digest = digest;
    INSERT INTO statement_return_acknowledgements (account, return_ref)
    VALUES (p_account, p_ref) ON CONFLICT DO NOTHING;
END $$;

-- One call delivers all outstanding files for the sending account. A lost
-- acknowledgement can cause a duplicate delivery, which finance can re-import.
CREATE FUNCTION receive_statement_returns(p_account text, p_returns jsonb, p_settlements jsonb)
RETURNS SETOF bigint LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE
    secret text;
BEGIN
    SELECT signing_key INTO STRICT secret FROM return_signing_keys WHERE account = p_account FOR UPDATE;
    IF EXISTS (
        SELECT FROM jsonb_populate_recordset(null::statement_return_inbox, p_returns) r
        WHERE r.statement_digest IS DISTINCT FROM encode(hmac(
            jsonb_build_array(p_account, r.return_ref, r.close_ref, r.batch_revision, extract(epoch from r.period_start),
                              extract(epoch from r.period_end), r.accepted_rows)::text, secret, 'sha256'), 'hex')
    ) THEN
        RAISE EXCEPTION 'invalid statement authentication for %', p_account;
    END IF;
    IF EXISTS (
        SELECT FROM jsonb_populate_recordset(null::statement_return_inbox, p_returns) r
        JOIN statement_return_inbox i ON i.account = p_account AND i.return_ref = r.return_ref
        WHERE i.statement_digest IS DISTINCT FROM r.statement_digest
    ) THEN
        RAISE EXCEPTION 'changed content for an existing statement return for %', p_account;
    END IF;
    INSERT INTO allocation_settlements (close_ref, account, revision, line_count, cents, settled_at)
    SELECT r.close_ref, p_account, r.revision, r.line_count, r.cents, r.settled_at
    FROM jsonb_populate_recordset(null::allocation_settlements, p_settlements) r
    ON CONFLICT DO NOTHING;
    RETURN QUERY
    INSERT INTO statement_return_inbox (account, return_ref, period_start, period_end, accepted_rows,
                                        close_ref, batch_revision, statement_read_at, signed_at, statement_digest)
    SELECT p_account, r.return_ref, r.period_start, r.period_end, r.accepted_rows,
           r.close_ref, r.batch_revision, r.statement_read_at, r.signed_at, r.statement_digest
    FROM jsonb_populate_recordset(null::statement_return_inbox, p_returns) r
    WHERE NOT EXISTS (SELECT FROM statement_return_acknowledgements a
                      WHERE a.account = p_account AND a.return_ref = r.return_ref)
    ORDER BY r.period_start, r.return_ref
    RETURNING statement_return_inbox.delivery_id;
END $$;
REVOKE ALL ON FUNCTION receive_statement_returns(text, jsonb, jsonb) FROM PUBLIC;
