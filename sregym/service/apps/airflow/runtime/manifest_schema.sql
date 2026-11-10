CREATE TABLE IF NOT EXISTS manifests (
    manifest_id  bigserial PRIMARY KEY,
    created_at   timestamptz NOT NULL DEFAULT now(),
    closed_at    timestamptz,
    dag_run_id   text,
    parcels      integer
);
SELECT setval('manifests_manifest_id_seq', 4100) WHERE NOT EXISTS (SELECT 1 FROM manifests);

CREATE TABLE IF NOT EXISTS manifest_lines (
    manifest_id  bigint NOT NULL REFERENCES manifests (manifest_id),
    order_id     text NOT NULL UNIQUE
);
