Message writes slow down during each recurring peak, then recover on their own. Track down the pattern and fix it without disabling routine maintenance. Message delivery and maintenance both need to stay healthy when the next peak arrives.

When you trust the fix, run `declare_repair_complete` to hand the system back — that ends your session, so leave nothing half-applied. Writing up what you found with `submit_incident_report` is optional and does not end the session; if you do, describe the causal chain more than the symptom.
