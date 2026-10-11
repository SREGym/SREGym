# Canonical supplier catalog adapter regression

This incident evolves the historical materialization problem into a partial schema migration. A supplier import has 80 current hotel rate cards and 256 small terms documents. The raw rate-card schema stores `supplier.hotelCode`; the raw terms schema stores `provider.hotelCode`. Two compatibility adapters publish the same canonical `hotelRef` and remove the raw envelopes. The enrichment recipe still joins `supplier.hotelCode` to `provider.hotelCode` **after** those adapters. Both fields are absent. MongoDB's missing-field/null equality makes each rate card match all 256 terms rows.

The derived rate rows have normal prices, codes and arrival dates. There is no historical union or inline media. Each booking-conditions document is about 3.9 KB; each hotel's 256-row cached aggregate is below memcached's 1 MiB item limit. A multi-hotel RPC can exceed gRPC's default 4 MiB response budget. JSON parsing, copying and sorting also create pressure and deadlines. The source records and existing inventory IDs remain preserved; generated rows have stable supplier-derived IDs.

There are four related representations, all from one catalog publication workflow:

1. Raw source collections are individually correct. Looking only at them makes the join names appear appropriate.
2. The canonical adapters and enrichment are MongoDB views. The final `catalog_export` is merely a projection over the enrichment view.
3. `catalog_jobs` stores authoritative recipes as JSON strings. A normal `catalog-agent` sidecar reconciles view definitions and incrementally merges the export into physical inventory every 30 seconds. A direct view correction is reverted. Correcting the recipe stops new erroneous matches but cannot remove obsolete materialized IDs.
4. Rate caches newline-delimited plans without expiration. Old cache values survive source and physical-inventory correction. Concurrent old workers must be drained before cache invalidation.

The prior fixer narrowed an archive union, deleted historical inventory and flushed cache. Here there is no archive union to narrow, date filtering retains the bad rows, and the raw source cardinalities remain small. Catalog changes live in durable database recipes, so pod restarts and application rollbacks do not repair them. Increasing resources cannot fix excessive response bytes.

An expert starts with search dependency errors and correlates rate spans, aggregate inventory counts and byte sizes. Compare a raw rate card and terms document with output from `rate_cards_current` and `supplier_terms_current`; the legacy join keys are missing from the adapter outputs, despite existing in the raw sources. Inspect all view definitions, then the publication ConfigMap and persisted job recipes. The correct join is `hotelRef` to `hotelRef`. Rebuild materialized inventory with replacement, not incremental merge, and invalidate caches after draining old workers. Disabling the obsolete incremental publisher prevents old publication commands from racing repair.

`mitigate.sh` implements this repair using only live cluster objects and database state. It does not use replay snapshots or private values. `recover.sh` additionally restores the original 27-record inventory from the preserved legacy catalog and restores exact saved Deployment specs. `inject.sh` snapshots original specs once, creates deterministic source documents and recipes, drains workers, publishes and warms cache, then resumes workers. The recurring production publication loop is the only replay sleep; sequencing uses rollout and pod-deletion conditions. No helper Pod or Job is created.

Rate CPU and cache capacity are raised and garbage collection uses a conservative 25 percent target to make the incident about catalog semantics rather than low resource limits. Search has one rate attempt so retries do not dominate diagnosis. These settings are restored during recovery.

Validation evidence is written to `validation/`. The final state is the active incident.
