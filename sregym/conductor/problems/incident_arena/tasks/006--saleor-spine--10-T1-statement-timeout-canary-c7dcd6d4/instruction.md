Checkout failures line up with storefront traffic spikes, but they’re intermittent. Between bursts, the Saleor API looks healthy. That’s made the cause hard to catch.

Find the underlying cause and fix it without broad changes. We cannot restart Saleor or PostgreSQL during this incident, so the repair needs to work in place. Keep existing orders and the rest of the platform intact.

Before handing this back, make sure checkouts stay healthy through another busy period and across new database sessions.

When you trust the fix, run `declare_repair_complete` to hand the system back — that ends your session, so leave nothing half-applied. Writing up what you found with `submit_incident_report` is optional and does not end the session; if you do, describe the causal chain more than the symptom.
