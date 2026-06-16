"""Confirmed deterministic: https://github.com/etcd-io/etcd/issues/18089

Title: Watch dropping an event when compacting on delete
Buggy: v3.5.0 through v3.5.15. Fixed: v3.5.16 (PR #18474).

Reproduction:
  1. Deploy etcd v3.5.15 (or earlier)
  2. PUT key three times (create multiple revisions in one generation)
  3. DELETE key (creates tombstone at revision R)
  4. PUT key again (new generation)
  5. COMPACT at revision R (triggers the bug: tombstone removed from index)
  6. WATCH --rev=R  →  DELETE event at R is silently dropped

Root cause: keyIndex.compact() in server/mvcc/key_index.go (lines 225-229)
unconditionally removes a tombstone from the `available` map when it is the
only remaining revision in a generation.  After compaction, the watch stream
replaying from that revision skips the DELETE event.

Fix: Remove the 4-line tombstone deletion block from compact().
"""

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoEtcd18089(GenericCustomBuildProblem):
    db_name = "etcd"
    db_version = "3.5.17"
    source_git_ref = "v3.5.15"
    root_cause_description = (
        "Watch silently drops DELETE events when the compaction target "
        "revision equals a delete tombstone revision. The bug is in "
        "keyIndex.compact() in server/mvcc/key_index.go: it unconditionally "
        "removes a tombstone from the available map when it is the only "
        "remaining revision in a generation. A subsequent watch --rev at "
        "that revision never sees the DELETE event."
    )
    reproducer = (
        "#!/bin/sh\n"
        "etcdctl del --prefix test18089/ >/dev/null 2>&1 || true\n"
        "etcdctl put test18089/k v2 >/dev/null\n"
        "etcdctl put test18089/k v3 >/dev/null\n"
        "etcdctl put test18089/k v4 >/dev/null\n"
        "DEL_OUT=$(etcdctl del test18089/k -w json 2>/dev/null)\n"
        "DEL_REV=$(echo \"$DEL_OUT\" | grep -o '\"revision\":[0-9]*' | head -1 | grep -o '[0-9]*')\n"
        "etcdctl put test18089/k v6 >/dev/null\n"
        "etcdctl compact \"$DEL_REV\" --physical >/dev/null 2>&1\n"
        "timeout 3 etcdctl watch test18089/k --rev=\"$DEL_REV\" > /tmp/watch.out 2>&1 || true\n"
        "if grep -q DELETE /tmp/watch.out; then\n"
        "  echo 'PASS: DELETE event visible'\n"
        "else\n"
        "  echo 'FAIL: DELETE event missing'\n"
        "  exit 1\n"
        "fi\n"
    )
    expected_output = "FAIL: DELETE event missing"
    continuous_reproducer = True
