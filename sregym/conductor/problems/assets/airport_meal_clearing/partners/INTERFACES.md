# Airport meal partner HTTP API

Run `python3 app.py` on port 8080. Set `ROLE` to `carrier`, `cafe`, or `clearing`; set `SITE` to `north` or `south`, `gate-a` through `gate-d`, or `clearing`, respectively. Give each instance its own `DATA_DIR`.

Every body and response is JSON. Successful GET and POST return HTTP 200, including a retry that returns the previously recorded result. Body fields are required unless marked optional. Input integers must fit in a signed 64-bit field. IDs and names are nonempty case-sensitive strings. Amounts are integer cents, nonnegative unless specified. Revisions start at 1 unless stated otherwise. HTTP 400 is malformed input, 404 is an absent resource, 409 is a business conflict (including reuse of a key with different contents), and 503 means SQLite is unavailable. Errors are `{"error":"message"}`. Journal collections in exports are arrays. Role identifiers use the literal names above.

## Shared records and reads

- Action — `{"action_id":"a1","benefit_id":"b1","carrier":"north","cafe":"gate-a","revision":1,"amount_cents":1500,"kind":"grant","bank":"night-1"}`. Kinds are `grant`, `revise`, `withdraw`, `offer`. Optional `predecessor` is the prior benefit ID for a replacement. A withdrawal has zero cents. A benefit is scoped by carrier and keeps its café. Revision uniqueness and ordering apply to grants, revisions, and withdrawals. Offers have their own action IDs and do not authorize a benefit.
- A stream order adds flat `input_topic`, `input_partition`, `input_offset`, supplied by Flink, and `output_topic`, `output_partition`, `output_offset`, supplied by the issuer bridge. Topics are strings, partitions and offsets nonnegative integers. Optional `input_topic_id` and `output_topic_id` identify broker topic incarnations. Each coordinate uses the topic ID when present, otherwise its name. The same ingestion stream should use a consistent coordinate convention. An authoritative replay can send the action alone to the revision path.
- The optional string `writer_job_id` is the Flink job ID carried by a stream order's envelope. Clearing preserves it on the keyed delivery. Earlier deliveries retain their original shape.
- `GET /health` — `{"ok":true,"role":"carrier","site":"north","sqlite_version":"…"}`.
- `GET /state` — all durable business records for the current role in one SQLite read snapshot. Every export includes `role`, `site` and the keys listed below. A collection also has a `GET /<collection>` returning the same array, with underscores in export names represented as hyphens in paths. Individual reads that serve as acknowledgment lookup are listed explicitly. Percent-encode path components.

## Carrier

State keys are `actions`, `dispatches`, `handouts`, `closes`, `statements`, `support`, `support_requests`, `amendments`, `accounts`, and `dispatch_paused` (boolean). `support_requests` retains the original accepted submissions and establishes authority to post them. A request accepted with `status:"pending"` is approved and queued for posting.

| Endpoint | Request and response |
| --- | --- |
| `POST /actions` | Send an action for this carrier. Returns the original action. Intake stays open during a dispatch pause. The first authorizing action for a benefit is a grant at revision 1. Later authorizing actions advance the revision. A replacement names an existing predecessor at the same café. `GET /actions/{action_id}` retrieves the action. |
| `POST /dispatch-control` | `{"paused":true}` or false. Returns the same object. `GET /dispatch-control` reads it. Dispatchers check this before sending a new Kafka record. |
| `POST /dispatches` | `{"action_id":"a1","input_topic":"allowances","input_partition":3,"input_offset":71}`. Optional `input_topic_id`. Returns the same object. One authoritative acknowledged coordinate per action. Acks from in-flight sends can be recorded during a pause. `GET /dispatches/{action_id}` retrieves the ack. |
| `POST /handouts` | `{"action_id":"a1","voucher_id":"V-00000001"}` records a printed number supplied to a passenger. Returns the same object. One immutable record per voucher ID. `GET /handouts/{voucher_id}` retrieves it. |
| `POST /closes` | `{"close_id":"n1","bank":"night-1","issue_topic":"issue-orders","issue_run":"night-production"}`. Returns those fields plus `actions` and `dispatches`, the full records for that bank visible at close in one transaction. `GET /closes/{close_id}` retrieves this immutable manifest. |
| `POST /statements` | `{"statement_id":"s1","close_id":"n1","lines":[{"benefit_id":"b1","revision":1,"amount_cents":1500}]}`. Returns the same immutable original. An empty statement is allowed. Each line records the original per-benefit total and one benefit appears at most once. Acceptance creates the account at revision 0. `GET /statements/{statement_id}` retrieves the original. |
| `GET /accounts/{statement_id}` | `{"statement_id":"s1","account_revision":0,"lines":[{"benefit_id":"b1","revision":1,"original_cents":1500,"support_cents":0,"amended_cents":0,"balance_cents":1500}]}`. The balance is the original plus signed amendments plus posted support. Queued support is visible in its own collection. |
| `POST /support` | `{"support_id":"u1","statement_id":"s1","benefit_id":"b1","revision":1,"amount_cents":500,"status":"pending"}`. Amount is positive. Status is `pending` to queue approved support or `posted` to post it on acceptance. Returns these fields plus `account_revision` (null until posted). An exact retry of the original submission returns the current support record. Changing a field on resubmission, including `status`, gives 409. `GET /support/{support_id}` reads current status and `GET /support-requests/{support_id}` reads the original. Pending entries leave the account revision intact. |
| `POST /support/{support_id}/post` | Posts the approved pending entry and advances its account revision once. For entries under a signed support batch, send `{"release_payment": payment}` using the exact keyed clearing payment specified by that batch. Send `{}` for other entries. Repeat the same body when retrying. The posted record includes the payment for entries that require one. |
| `POST /amendments` | `{"request_id":"m1","statement_id":"s1","expected_account_revision":1,"lines":[{"benefit_id":"b1","revision":2,"amount_cents":-250}]}`. Signed amounts are per-benefit adjustments, each benefit appears once. Include `transfer_approval_id` to identify an independent signed transfer when its lines could also describe a close correction. A linked amendment uses exact lines from that approval, in one request or in separate requests, and a linked line can post once. Returns the request plus `account_revision`. A new request must match the current account revision. An exact retry returns the applied record even if the account has since advanced. `GET /amendments/{request_id}` retrieves it. |

Support and amendments can name a benefit omitted from the original. The account line revision is the highest recorded statement, posted support, or amendment revision for that benefit. Signed amendments may reduce a balance to zero.

## Clearing

State keys are `orders`, `revisions`, `benefits`, `aliases`, `offers`, `support`, `uploads`, `remittances`, `settlement_events`, and `cursors`. The clearing role receives orders and accepts café receipt uploads through HTTP. The bridge selects the issuance endpoint.

| Endpoint | Request and response |
| --- | --- |
| `POST /issuance-cutover` | Send `{}` after draining legacy orders and before switching the issuer to revision mode. Returns `{"orders_through":200}`, the exclusive end of the legacy clearing order journal. An exact retry returns the original boundary. The first accepted authorizing revision request also establishes it if it has not already been recorded. |
| `GET /issuance-cutover` | Returns the same durable boundary. The clearing state export includes it as `issuance_cutover`, with `null` before cutover. Order positions within the boundary follow the insertion order of `GET /orders`. |
| `POST /orders/legacy` | Send a stream order. Returns the full order plus `mode:"legacy"` and `voucher_id` (string or null). A grant or revision prints one alias per unique output coordinate until that revision is committed through the revision path. Exact coordinate retries return the stored order. Offers and withdrawals are journaled without a voucher. |
| `POST /orders/revision` | Send a stream order or an action alone. Returns `{"action":{…},"voucher_id":"V-00000001"}` (voucher ID can be null). Resolves grant, revision and withdrawal by carrier, benefit and revision, reusing a voucher already printed for that revision where available. Equal revisions with different action contents give 409. Stream deliveries are also journaled by output coordinate with `mode:"revision"`. |
| `GET /orders/{topic_key}/{partition}/{offset}` | Stored stream order. Use the output topic ID if supplied on delivery, otherwise use the topic name. |
| `GET /revisions/{carrier}/{benefit_id}/{revision}` | Committed revision-path result `{"action":{…},"voucher_id":"…" }`. |
| `GET /benefits/{carrier}/{benefit_id}` | Latest action for a benefit, including withdrawals. Clearing `benefits` is an array of these actions. |
| `GET /aliases/{voucher_id}` | Immutable printed record `{"voucher_id":"…","carrier":"north","benefit_id":"b1","cafe":"gate-a","revision":1,"amount_cents":1500}`. The revision and amount identify what was printed. Current eligibility and value come from the benefits view. |
| `GET /offers/{carrier}/{action_id}` | Stored offer action. |
| `POST /support` | Send a posted carrier support record including `account_revision`, plus `carrier`. Returns the same object. Clearing can apply posted support to the first purchase if recorded before it is remitted. `GET /support/{carrier}/{support_id}` retrieves the delivery. |
| `POST /uploads` | Send a café receipt as returned by redemption, referencing a printed number issued for that café and benefit. Returns the stored receipt. Receipts can arrive out of order. The upload key is café and receipt sequence. `GET /uploads/{cafe}/{receipt_seq}` retrieves it. |
| `POST /remittances` | `{"cafe":"gate-a"}` pays all currently contiguous uploaded receipts from that café. Add a positive integer `through_receipt_seq` to limit payment to that sequence, inclusive. Returns `{"cafe":"gate-a","receipt_seq":2}`, acknowledging the payment boundary. |
| `GET /remittances/{cafe}/{receipt_seq}` | `{"cafe":"gate-a","receipt_seq":1,"carrier":"north","benefit_id":"b1","amount_cents":1200,"carrier_cents":700,"support_cents":500,"operator_cents":0,"support_ids":["u1"]}`. Every row is an immutable payment. |
| `GET /settlement-events` | The same immutable event array as `settlement_events` in `GET /state`, in sequence order. A support delivery is `{"sequence":1,"kind":"support","carrier":"north","support_id":"u1"}`. A payment is `{"sequence":2,"kind":"payment","cafe":"gate-a","receipt_seq":1}`. |
| `GET /cursors/{cafe}` | `{"cafe":"gate-a","receipt_seq":2}`. |

Clearing pays the first eligible accepted receipt for each benefit in local receipt sequence order. The covered amount is the lower of the accepted sale and the allowance on that receipt. Posted support for the benefit reduces the carrier share up to the covered amount. The airport operator pays any remainder, including all later accepted purchases for that benefit. Totals always equal the accepted sale. Later support posting leaves earlier payments intact.

Each remittance applies its payments and advances the cursor in a single transaction. A gap stops payment at the last contiguous receipt, with zero as the initial boundary. Once the requested bound is paid, repeating the bounded request acknowledges that sequence even if the live cursor has advanced. Read `GET /remittances/{cafe}/{receipt_seq}` for the immutable payment and `GET /cursors/{cafe}` for the current cursor.

Settlement events share one sequence across carriers and cafés, starting at 1. Clearing appends one event in the same transaction as each newly accepted posted support delivery or payment. A remittance that pays several receipts appends one event per receipt in payment order. An exact retry of a support delivery or a remittance with no new contiguous receipts appends zero events. To reconstruct the support available for a payment, use earlier support events and look up their posted records by carrier and support ID. The support event marks when clearing received the posting from the carrier.

## Café

State keys are `catalog`, `staged`, `receipts`, `closes`, `mode` (initially `voucher`, then `benefit`), `catalog_revision` (initially 0), `receipt_seq` (initially 0), and `sales_paused` (boolean).

A catalog row is `{"voucher_id":"…","carrier":"north","benefit_id":"b1","revision":1,"amount_cents":1500,"active":true}`. The voucher identity is permanent. Revision, amount and active status describe the current authorization. In benefit mode all rows for a benefit share its current authorization.

| Endpoint | Request and response |
| --- | --- |
| `POST /catalog` | Send one catalog row. Returns the applied row. In voucher mode this updates that voucher. In benefit mode the update propagates current authorization to all local aliases for that benefit. Older changes give 409. `GET /catalog/{voucher_id}` reads a row. |
| `POST /staged` | Send one catalog row for the next close. Returns the staged row. Staging is durable and does not change what tills accept today. `GET /staged/{voucher_id}` reads a staged row. |
| `POST /closes` | `{"request_id":"shift-2","expected_catalog_revision":0}`. Atomically applies staged rows using the newest authorization per benefit, switches the spend key to carrier and benefit, carries forward every accepted spend, and records the greatest accepted receipt sequence. Returns the request plus `catalog_revision` and `receipt_seq`. Only a successful new close advances the catalog revision. An exact retry returns the original boundary. `GET /closes/{request_id}` reads it. |
| `POST /sales-control` | `{"paused":true}` or false. Returns the same object. Pausing blocks new sales. Previously accepted sale requests can still be retrieved or retried. `GET /sales-control` reads it. |
| `POST /receipts` | `{"request_id":"sale-1","voucher_id":"…","amount_cents":1200}`. Sale amount is positive and within the current allowance. Returns the request plus `cafe`, `carrier`, `benefit_id`, `revision`, `allowance_cents`, `receipt_seq`, `catalog_revision`. The old mode checks voucher use and the new mode checks benefit use. An accepted request retry returns the same receipt even across a close. |
| `GET /receipts/{receipt_seq}` | Immutable accepted receipt. |
| `GET /requests/{request_id}` | Accepted receipt by sale request ID. |

A café receipt becomes payable upon acceptance. Shift closes and uploads leave it intact. Each role owns its own database. A client can retry an uncertain request and use the receiver's keyed GET to confirm the record that actually committed.

## Departure exports and carrier close

A range reference has `range_id`, `topic`, `topic_id`, `writer_job_id`, and `partitions`. The partition map has keys `"0"` through `"3"`. Each value has inclusive `start` and exclusive `end` offsets. An export has `reference` and `rows`. Each row is a stream order with both input and output topic IDs. Use the ID for keyed coordinate lookups after a topic name changes or is reused. The stream archive retains source rows and the corresponding Flink job record after broker retention.

| Endpoint | Request and response |
| --- | --- |
| Carrier `POST /exports`, `GET /exports/{range_id}` | Archive or read an immutable export. Acceptance validates all output coordinates against the reference. |
| Carrier `POST /close-settings`, `GET /close-settings/{bank}` | Set or read `{"bank":"early","range_id":"primary-early"}`. New closes use the configured export. |
| Carrier `POST /closes`, `GET /closes/{close_id}` | To compile a close, send `close_id`, `bank` and `statement_id`. Include `contingency_admissions` when the close tracks approved scopes. Each member holds an `approval_id` and its clearing `receipt_id`. The service verifies the linked records and compiles the statement using the configured export. The close retains the admission list, and both the close and statement retain the export they used. An exact retry returns the original close. |
| Carrier `POST /export-approvals`, `GET /export-approvals/{approval_id}` | Record or read a signed scope with `approval_id`, `cohort`, `bank`, `signed_by` and `benefit_ids`. The response includes the carrier. |
| Clearing `POST /export-receipts`, `GET /export-receipts/{receipt_id}` | Accept or read a scoped export. Send `receipt_id`, `approval_id`, `carrier`, `cohort`, `bank`, `benefit_ids`, `reference` and the export `rows`. Clearing admits matching rows and records their delivery under their original coordinates in one transaction. The receipt includes only the admitted rows. |

## Carrier transfer, receiver, counter and support records

All collections in this section are also available through `GET /state`.

- `GET /transfer-approvals/{approval_id}` returns the signed transfer authority, including its statement ID and signed line amounts. The matching keyed amendments show which lines have posted. `POST /transfer-approvals` records that authority with `approval_id`, `statement_id`, `signed_by` and a balanced array of ordinary amendment `lines`.
- `GET /adjustment-files/{file_id}` returns the original accepted file, its statement and export range IDs, and ordered lines. `POST /adjustment-files` accepts those fields and requires a unique `line_id` on each signed line.
- `GET /adjustment-files/{file_id}/postings` returns applied acknowledgments with `file_id`, `line_id` and the `posting_id` in the amendment journal. `POST /adjustment-files/{file_id}/post` takes `{}` to process all lines or `{"through":"line-id"}` to process an inclusive prefix. It returns the resulting acknowledgments in original file order. A retry, including after restart, returns those acknowledgments and advances the account only for newly posted lines.
- `POST /counter-actions` records `record_id`, `signed_by` and a canonical `action` together at intake. `GET /counter-actions/{action_id}` returns that record. The dispatcher preserves any genuine broker acknowledgment it already has. It leaves the local action for fulfillment at the counter. Apply the authorized revision through clearing and update the café authorization for its supplied number. Record completion at `POST /counter-dispositions` with `action_id`, `record_id` and `disposition:"fulfilled-locally"`. Read it by action ID.
- `GET /support-approvals/{approval_id}` returns independently signed pending `entries` for a `cafe` and the receipt sequence in `release_after`. `POST /support-approvals` records those fields with `approval_id` and `signed_by`. Read `GET /remittances/{cafe}/{release_after}` on clearing and attach that payment to each carrier support post for the batch. The carrier verifies the payment against clearing. Deliver the entire posted record to clearing with its carrier name. Clearing's settlement sequence shows which delivery each subsequent receipt could use.
