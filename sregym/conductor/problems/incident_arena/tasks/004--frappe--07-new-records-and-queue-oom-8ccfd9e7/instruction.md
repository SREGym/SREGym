People can still edit existing records and use browsing, search, and dashboards. Creating something new is broken: records, comments, and file uploads all return a server error. Background work has stopped too: queued emails, report exports, and scheduled jobs sit without ever running.

This is separate from the create errors, so make sure both problems are resolved.

Preserve queued work and keep queue capacity finite and loss-intolerant through a broker restart. A broad database grant, cache-style eviction, flushing data, worker scaling, traffic suppression, or restart-only recovery is not acceptable.

When you trust the fix, run `declare_repair_complete` to hand the system back — that ends your session, so leave nothing half-applied. Writing up what you found with `submit_incident_report` is optional and does not end the session; if you do, describe the causal chain more than the symptom.
