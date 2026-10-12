Normal traffic looks fine, but concurrent sends keep making message delivery unreliable. Track down the problem and fix it without losing any accepted messages or changing their order. Make sure the repair survives a message-service restart and stays healthy while traffic continues.

When you trust the fix, run `declare_repair_complete` to hand the system back — that ends your session, so leave nothing half-applied. Writing up what you found with `submit_incident_report` is optional and does not end the session; if you do, describe the causal chain more than the symptom.
