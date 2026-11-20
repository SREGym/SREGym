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
