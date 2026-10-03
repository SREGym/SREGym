Message sends worked normally all morning. At one specific moment they began failing outright, and sign-ins became slow at exactly the same time. Neither has recovered, and there was no deployment.

There is more than one cause here, so keep going until each one is fixed and both sends and sign-ins are back to normal.

When you trust the fix, run `declare_repair_complete` to hand the system back — that ends your session, so leave nothing half-applied. Writing up what you found with `submit_incident_report` is optional and does not end the session; if you do, describe the causal chain more than the symptom.
