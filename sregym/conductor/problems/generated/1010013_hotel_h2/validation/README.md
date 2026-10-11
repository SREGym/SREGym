# Live validation

All three scripts succeeded on repeated execution. Deployment specs matched pre-incident snapshots after recovery; the original 27 inventory records and collection topology were restored. The finalized replay was validated from the fully recovered deployment. No temporary publication or recovery directory remains.

A direct view repair was reverted by persisted job reconciliation (see `direct-view-repair.txt` and `direct-view-reverted.txt`). A rate rollout restart left the SLO violated. All application pods were Running and Ready after the final restart.

Final spaced checks were 193 seconds apart. Search error-log surges satisfied the observability requirement.

| Evidence | UTC capture | Violating rounds | Error ratios | p99 milliseconds |
|---|---|---|---|---|
| replay-01.json | 2026-10-10T12:25:36+00:00 | 4/4 | 54.0%–60.6% | 5620–6170 |
| replay-02.json | 2026-10-10T12:27:25+00:00 | 4/4 | 58.6%–60.1% | 4510–6370 |
| final-mitigation-healthy.json | 2026-10-10T12:31:15+00:00 | 0/4 | 0.0%–0.0% | 58–132 |
| final-recovery-healthy.json | 2026-10-10T12:35:00+00:00 | 0/4 | 0.0%–0.0% | 51–64 |
| final-01.json | 2026-10-10T12:40:45+00:00 | 4/4 | 57.6%–59.9% | 5300–7970 |
| final-02.json | 2026-10-10T12:43:58+00:00 | 4/4 | 54.3%–59.4% | 5210–14980 |
