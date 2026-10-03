When traffic gets busy, pages that worked a moment ago start returning errors and some users are refused outright. Browsing, search, and dashboards recover once the rush passes. Background work stays stuck even when traffic is quiet: queued emails, report exports, and scheduled jobs never run.

These are separate failures, so do not stop after fixing one of them. Make sure both problems are resolved.

Preserve queued work and keep queue capacity finite and loss-intolerant through a broker restart. A cache-style eviction policy, flushing data, worker scaling, traffic suppression, or restart-only recovery is not acceptable.

When you trust the fix, run `declare_repair_complete` to hand the system back — that ends your session, so leave nothing half-applied. Writing up what you found with `submit_incident_report` is optional and does not end the session; if you do, describe the causal chain more than the symptom.
