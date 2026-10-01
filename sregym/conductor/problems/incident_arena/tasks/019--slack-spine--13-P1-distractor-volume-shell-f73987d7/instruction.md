The message delivery path has been flaky even though traffic looks normal.
Figure out what is actually causing it and fix the problem without broad or
unrelated changes. Make sure it stays gone after one restart and remains stable
while traffic continues.

When you trust the fix, run `declare_repair_complete` to hand the system back —
that ends your session, so leave nothing half-applied. Writing up what you
found with `submit_incident_report` is optional and does not end the session;
if you do, describe the causal chain more than the symptom.
