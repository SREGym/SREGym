Busy periods are knocking users out of Desk. Pages that worked a moment earlier return errors until traffic settles, then browsing, search, and dashboards recover. Background work is broken even during quiet hours: queued emails and report exports never arrive, and scheduled jobs do not run.

These are separate failures, so do not stop after fixing one of them. Make sure both problems are resolved.

Preserve queued work and keep the queue's finite, loss-intolerant memory policy through a broker restart. Flushing data, changing the cache tier, worker scaling, traffic suppression, or restart-only recovery is not acceptable.

When you trust the fix, run `declare_repair_complete` to hand the system back — that ends your session, so leave nothing half-applied. Writing up what you found with `submit_incident_report` is optional and does not end the session; if you do, describe the causal chain more than the symptom.
