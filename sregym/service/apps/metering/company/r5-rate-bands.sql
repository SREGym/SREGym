BEGIN;
ALTER TYPE rate_info ADD ATTRIBUTE band text;
UPDATE price_book SET band = CASE WHEN metric = 'storage_gb_h' THEN 'list' ELSE 'committed' END;
CREATE OR REPLACE FUNCTION rate_snapshot(p price_book) RETURNS rate_info LANGUAGE sql STABLE AS $$
    SELECT ROW(p.unit_price, p.band)::rate_info
$$;
INSERT INTO ops.billing_releases (revision) VALUES ('r5 rate bands');
COMMIT;
