-- Finance database: the ledger copy from billing, invoices, adjustments and credit notes.
CREATE TABLE accounts (
    account     text PRIMARY KEY,
    name        text NOT NULL,
    billing_day integer NOT NULL CHECK (billing_day BETWEEN 1 AND 28),
    email       text NOT NULL,
    committed_discount numeric NOT NULL DEFAULT 0 CHECK (committed_discount BETWEEN 0 AND 100)
);

CREATE TABLE ledger_lines (
    line_id     bigint PRIMARY KEY,
    account     text NOT NULL,
    metric      text NOT NULL,
    quantity    numeric(12,3) NOT NULL,
    amount      numeric(14,4) NOT NULL,
    rated_at    timestamptz NOT NULL,
    invoice_id  bigint,
    rate        text NOT NULL
);
CREATE INDEX ledger_lines_open ON ledger_lines (account) WHERE invoice_id IS NULL;

CREATE TABLE invoices (
    invoice_id  bigserial PRIMARY KEY,
    account     text NOT NULL REFERENCES accounts,
    issued_at   timestamptz NOT NULL DEFAULT now(),
    total       numeric(14,2) NOT NULL,
    status      text NOT NULL DEFAULT 'sent'
);

CREATE TABLE invoice_items (
    invoice_id  bigint NOT NULL REFERENCES invoices,
    kind        text NOT NULL CHECK (kind IN ('usage', 'adjustment')),
    ref         bigint NOT NULL,
    line_id     bigint,
    amount      numeric(14,4) NOT NULL,
    rate        text
);
CREATE INDEX invoice_items_line ON invoice_items (line_id);

-- Changes to usage that was already invoiced are never applied to the invoiced
-- line; they are billed on the account's next invoice.
CREATE TABLE ledger_adjustments (
    adjustment_id bigserial PRIMARY KEY,
    account     text NOT NULL,
    line_id     bigint,
    amount      numeric(14,4) NOT NULL,
    reason      text NOT NULL,
    booked_at   timestamptz NOT NULL DEFAULT now(),
    invoice_id  bigint
);
CREATE INDEX ledger_adjustments_line ON ledger_adjustments (line_id);

CREATE TABLE credit_notes (
    credit_id   bigserial PRIMARY KEY,
    account     text NOT NULL REFERENCES accounts,
    amount      numeric(14,2) NOT NULL CHECK (amount > 0),
    memo        text NOT NULL,
    issued_by   text NOT NULL,
    issued_at   timestamptz NOT NULL DEFAULT now()
);

CREATE FUNCTION usage_charge(list_amount numeric, rate text, discount numeric) RETURNS numeric
    LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE WHEN split_part(rate, ',', 2) = 'committed)'
                THEN round(list_amount * (1 - discount / 100), 4)
                ELSE list_amount END
$$;

CREATE FUNCTION hold_invoiced_line() RETURNS trigger LANGUAGE plpgsql SET search_path = public AS $$
DECLARE
    acct text;
    sent numeric;
    billed numeric;
    new_charge numeric := 0;
    target numeric;
    corrected_rate text;
BEGIN
    IF OLD.invoice_id IS NULL THEN
        IF TG_OP = 'DELETE' THEN
            RETURN OLD;
        END IF;
        RETURN NEW;
    END IF;
    SELECT amount INTO STRICT sent FROM invoice_items
    WHERE invoice_id = OLD.invoice_id AND line_id = OLD.line_id AND kind = 'usage';
    IF TG_OP = 'UPDATE' THEN
        SELECT usage_charge(NEW.amount, NEW.rate, committed_discount) INTO STRICT new_charge
        FROM accounts WHERE account = NEW.account;
    END IF;
    FOR acct IN
        SELECT OLD.account
        UNION SELECT account FROM ledger_adjustments WHERE line_id = OLD.line_id
        UNION SELECT NEW.account WHERE TG_OP = 'UPDATE'
    LOOP
        SELECT CASE WHEN acct = OLD.account THEN sent ELSE 0 END
               + coalesce(sum(amount), 0)
          INTO billed
          FROM ledger_adjustments WHERE line_id = OLD.line_id AND account = acct;
        target := CASE WHEN TG_OP = 'UPDATE' AND acct = NEW.account THEN new_charge ELSE 0 END;
        IF target <> billed THEN
            INSERT INTO ledger_adjustments (account, line_id, amount, reason)
            VALUES (acct, OLD.line_id, target - billed,
                    CASE WHEN TG_OP = 'DELETE' THEN 'usage voided after invoice'
                         WHEN acct <> NEW.account THEN 'usage moved to ' || NEW.account
                         WHEN billed = 0 THEN 'usage moved from ' || OLD.account
                         ELSE 'usage re-rated after invoice' END);
        END IF;
    END LOOP;
    -- A local reconciliation can correct the snapshot. The sent item retains
    -- the rate used at close and the money changes only through adjustments.
    IF TG_OP = 'UPDATE' AND current_setting('session_replication_role') <> 'replica'
       AND NEW.rate IS DISTINCT FROM OLD.rate THEN
        corrected_rate := NEW.rate;
        NEW := OLD;
        NEW.rate := corrected_rate;
        RETURN NEW;
    END IF;
    RETURN NULL;
END $$;

CREATE TRIGGER hold_invoiced_line BEFORE UPDATE OR DELETE ON ledger_lines
    FOR EACH ROW EXECUTE FUNCTION hold_invoiced_line();
ALTER TABLE ledger_lines ENABLE ALWAYS TRIGGER hold_invoiced_line;

-- Closes each due account's open usage and pending adjustments into one invoice.
CREATE FUNCTION run_invoices(p_accounts text[]) RETURNS integer LANGUAGE plpgsql AS $$
DECLARE
    acct text;
    discount numeric;
    inv bigint;
    n integer := 0;
BEGIN
    FOREACH acct IN ARRAY p_accounts LOOP
        SELECT committed_discount INTO discount FROM accounts WHERE account = acct FOR UPDATE;
        IF NOT EXISTS (SELECT 1 FROM ledger_lines WHERE account = acct AND invoice_id IS NULL)
           AND NOT EXISTS (SELECT 1 FROM ledger_adjustments WHERE account = acct AND invoice_id IS NULL) THEN
            CONTINUE;
        END IF;
        INSERT INTO invoices (account, total) VALUES (acct, 0) RETURNING invoice_id INTO inv;
        WITH lines AS (
            UPDATE ledger_lines SET invoice_id = inv
            WHERE account = acct AND invoice_id IS NULL
            RETURNING line_id, amount, rate)
        INSERT INTO invoice_items (invoice_id, kind, ref, line_id, amount, rate)
        SELECT inv, 'usage', line_id, line_id, usage_charge(amount, rate, discount), rate FROM lines;
        WITH adj AS (
            UPDATE ledger_adjustments SET invoice_id = inv
            WHERE account = acct AND invoice_id IS NULL
            RETURNING adjustment_id, line_id, amount)
        INSERT INTO invoice_items (invoice_id, kind, ref, line_id, amount)
        SELECT inv, 'adjustment', adjustment_id, line_id, amount FROM adj;
        UPDATE invoices SET total = (SELECT round(coalesce(sum(amount), 0), 2) FROM invoice_items WHERE invoice_id = inv)
        WHERE invoice_id = inv;
        n := n + 1;
    END LOOP;
    RETURN n;
END $$;
