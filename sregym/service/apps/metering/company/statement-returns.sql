-- Finance records support decisions against one charge on one return.
CREATE TABLE statement_waiver_revocations (
    account     text NOT NULL REFERENCES accounts,
    return_ref  text NOT NULL,
    line_id     bigint NOT NULL,
    ticket_id   text NOT NULL,
    revoked_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (account, return_ref, line_id)
);

-- Support can revoke an omission waiver with a ticket. This also clears the
-- current waiver so the open charge is eligible for the next invoice.
CREATE FUNCTION revoke_statement_waiver(p_account text, p_ref text, p_line bigint, p_ticket text)
RETURNS void LANGUAGE plpgsql SET search_path = public AS $$
BEGIN
    PERFORM 1 FROM accounts WHERE account = p_account FOR UPDATE;
    INSERT INTO statement_waiver_revocations (account, return_ref, line_id, ticket_id)
    VALUES (p_account, p_ref, p_line, p_ticket)
    ON CONFLICT DO NOTHING;
    UPDATE ledger_lines SET waived_by_return = NULL, waiver_reason = NULL
    WHERE account = p_account AND line_id = p_line AND waived_by_return = p_ref;
END $$;

-- An accepted return lists every charge the customer agrees to pay in the period.
CREATE FUNCTION import_statement_return(p_account text, p_ref text, p_start timestamptz, p_end timestamptz,
                                        p_rows jsonb)
RETURNS integer LANGUAGE plpgsql SET search_path = public AS $$
DECLARE
    waived integer;
BEGIN
    PERFORM 1 FROM accounts WHERE account = p_account FOR UPDATE;
    MERGE INTO ledger_lines AS charge
    USING jsonb_to_recordset(p_rows) AS accepted(event_id bigint, metric text, quantity numeric)
    ON charge.line_id = accepted.event_id
    WHEN NOT MATCHED BY SOURCE AND charge.account = p_account AND charge.invoice_id IS NULL
         AND charge.occurred_at >= p_start AND charge.occurred_at < p_end
         AND charge.waived_by_return IS DISTINCT FROM p_ref
         AND NOT EXISTS (
             SELECT 1 FROM statement_waiver_revocations r
             WHERE r.account = p_account AND r.return_ref = p_ref AND r.line_id = charge.line_id
         )
    THEN UPDATE SET waived_by_return = p_ref, waiver_reason = 'omitted from statement return';
    GET DIAGNOSTICS waived = ROW_COUNT;
    RETURN waived;
END $$;
