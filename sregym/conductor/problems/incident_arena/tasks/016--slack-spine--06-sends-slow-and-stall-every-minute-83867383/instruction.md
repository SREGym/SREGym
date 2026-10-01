During busy periods, sending a message hangs for many seconds and sometimes times out. It goes back to normal once traffic settles. There is another pattern too: sends that do get through freeze for several seconds at the same point in each busy period, about once a minute. People have learned to wait it out, and quiet days have not made it go away.

The traffic-related slowdown and the once-a-minute stall are separate problems, so fix both. Routine maintenance still needs to run on schedule. Make sure sends stay healthy through a busy period.

When you trust the fix, run `declare_repair_complete` to hand the system back — that ends your session, so leave nothing half-applied. Writing up what you found with `submit_incident_report` is optional and does not end the session; if you do, describe the causal chain more than the symptom.
