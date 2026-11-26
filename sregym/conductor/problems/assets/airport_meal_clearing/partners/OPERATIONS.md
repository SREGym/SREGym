# Airport meal partners

The partner services listen on port 8080. Carrier desks are `north` and
`south`. Café tills are `gate-a` through `gate-d`. Issuance and remittance
are at `clearing`. Each site runs the same Python source with its own
`ROLE`, `SITE`, and persistent `DATA_DIR`. The database and its WAL live
in that directory. Keep the volume attached to the same site on restart.
`GET /health` reports the running role, site, and SQLite version.

`GET /state` exports the site's records in one read transaction. Use the
keyed reads in [INTERFACES.md](INTERFACES.md) when confirming a single action,
receipt, close, or account update. After a timeout, keep the same request
identifier and body. The receiver's keyed record tells you whether the
operation committed.

Carrier dispatch can pause while desks keep accepting bookings. Dispatch
receipts record the Kafka coordinate returned to the sender. A bank close
captures the accepted actions and known dispatch receipts at that point.
Original statements and support requests stay in the journal. Account
revisions advance when support posts or when a new amendment is accepted.
An accepted support request with `status:"pending"` is approved for posting.
Look up its original request at `GET /support-requests/{support_id}` and its
current status at `GET /support/{support_id}`. Use
`POST /support/{support_id}/post` to post approved queued support. For a
signed batch, attach the keyed clearing payment named in the approval.
For other support, send `{}`.
Read the current account revision before submitting a new amendment.

Clearing retains every printed number, along with its original issue
revision. The latest benefit record gives the current authorization.
The issue bridge controls which issuance endpoint receives stream output.
In shared redemption mode, all aliases at a café have one allowance and
one spend key for the benefit.

A café can stage catalog changes while tills accept sales. A shift close
applies the staged catalog and returns the last receipt sequence from the
previous catalog revision. Sites can also pause new sales while coordinating
a close. Accepted receipts stay available by sequence and request ID.

Upload actual café receipts using their original fields. Clearing can
receive later sequences first. Remittance advances only through consecutive
receipts and records the payer split for each sale. Read the café cursor
and the keyed remittance records to confirm payment after an uncertain
response. Clearing's `/state` includes `settlement_events` in the same read
snapshot as support deliveries, remittances, and cursors. Match a payment
event by café and receipt sequence. Earlier support events identify which
carrier postings had reached clearing when it paid that receipt.
