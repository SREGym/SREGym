# Orphaned content snapshot coordinator

A content snapshot coordinator was started interactively in the existing
`media-mongodb` container. It connects to `post-storage-mongodb` and obtains
MongoDB's supported `fsync` write freeze. The separate transfer never commits,
and the coordinator has no deadline or exit condition. If someone unlocks the
database, it waits 210 seconds for a transfer drain and obtains the freeze again.
It reconnects after database restarts. This mirrors leftover operational backup
and snapshot guards which outlive the terminal or transfer that started them.

There are no Kubernetes object changes, new collections, document edits,
index changes, DNS changes, credentials, or cache policies. A detached `mongo`
shell and its ordinary snapshot script, PID and log files are the only additions
inside an existing application container. The startup FIFO is removed before
`inject.sh` returns; startup is acknowledged after the write freeze is acquired.
Repetition does not start another coordinator or increment the lock count.

Publishing waits for a post insert, so Thrift timeouts and a throughput collapse
appear at the frontend. MongoDB remains reachable and readable. The useful
diagnostic is `db.currentOp()`: it shows `fsyncLock: true` and post inserts waiting
for locks. `db.currentOp({$all:true})` also shows an idle Mongo shell connection
from the media datastore's Pod IP. Mapping that address to pods and examining
processes in that container reveals the coordinator and its source script.
The database logs provide another route to the administrative connection.

An unlock alone really repairs publishing, including a complete healthy
120-second measurement window. The delayed reacquisition defeats verification
based on a few successful requests or that short window. Normal application
rollouts do not terminate a process in a different datastore container. A
datastore restart can release its lock but cannot terminate the remote process.
An expert stops the coordinator first, then runs `fsyncUnlock` until no lock
references remain and verifies recovery beyond the 210-second retry interval.
`mitigate.sh` performs that repair using current cluster state alone.

Cleanup removes the same process and its files and releases its runtime lock.
The original unlocked state is recorded outside the cluster for `recover.sh`;
the scripts do not alter or delete application data. The operational loop's
five-second poll is part of the incident, not a race-dependent injection wait.

## Live validation

The baseline served approximately 102 requests/second with no errors and an
82 ms baseline p99. Two initial post-freeze checks, taken more than 60 seconds
apart, reported `verdict.violated: true`; the later window had 11/11 violating
rounds. Throughput fell to approximately 0.3-6.4 requests/second. The frontend's
error-like logs rose from zero to 72 lines in the reported window.

An administrative unlock at 05:54:38 UTC produced a complete 120-second window
with 11/11 healthy rounds, approximately 102 requests/second and no errors at
05:57:48 UTC. At 05:58:43 UTC the coordinator had reacquired the lock and writers
were queued again. The new low-throughput rounds began approximately 210 seconds
after the unlock. The detached process was still the same PID in media-mongodb.

`mitigate.sh` was run twice successfully at 05:59 UTC. All write queues drained,
the coordinator's files were removed, and subsequent 120-second windows had
11/11 healthy rounds with no errors. Recovery remained healthy past the
210-second retry interval. `recover.sh` also ran twice successfully; its final
check had 11/11 healthy rounds at approximately 102 requests/second.

The final injection was run twice successfully at 06:04 UTC. A complete
publishing-service Pod replacement left the write lock and remote coordinator
intact. At 06:06 UTC, all 27 application pods were Running and Ready while the SLO
was violated and frontend error-like logs had again surged above baseline.

The final persistence watch completed after 1,807 seconds with 39 checks. All
36 checks taken after the initial window cleared reported `violated: true`,
with every round violating the throughput SLO. Their throughput ranged from
0.2 to 11.8 requests/second, versus the minimum of 70. The final window had
11/11 violating rounds and 0.2-8.1 requests/second. Frontend error-like logs
rose from zero to 68 lines, and post-storage error-like logs from zero to 78
lines in the final reported window.

At completion, all 27 application pods remained Running and Ready. The remote
coordinator was still running and the write freeze was still active. All three
scripts were exercised twice consecutively with successful exit status. The
incident is left active for submission. Measurement snapshots are in
`state/`, including the healthy repair window and delayed regression.
