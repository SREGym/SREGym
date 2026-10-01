Deleting anything in Frappe returns an error. Records can still be created and edited, and browsing, search, and dashboards all work. Nothing handed to the background completes either: queued emails and report exports never arrive, and scheduled jobs do not run.

These are separate failures, so do not stop after fixing one of them. Make sure both problems are resolved.

Preserve work already in the queue. Do not replace a targeted permission repair with a broad grant, a flush, worker scaling, or a restart-only workaround.

When you trust the fix, run `declare_repair_complete` to hand the system back — that ends your session, so leave nothing half-applied. Writing up what you found with `submit_incident_report` is optional and does not end the session; if you do, describe the causal chain more than the symptom.
