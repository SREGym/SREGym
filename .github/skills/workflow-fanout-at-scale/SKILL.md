---
name: workflow-fanout-at-scale
description: >-
  Playbook for running a LARGE multi-agent Workflow fan-out reliably — dozens to hundreds of items such
  as reproducing many bugs, auditing/reviewing many files, migrating call-sites, or generating many code
  files. Use this skill when designing or debugging a big Workflow fan-out: choosing concurrency, avoiding
  resource (disk/memory) exhaustion, pilot-gating spend, passing batches into the script, making runs
  resumable/idempotent, verifying agent output without trusting self-reports, and checkpointing so a crash
  loses nothing. Distilled from an 85-bug reproduction + 85-problem code-generation effort on a kind cluster.
user-invocable: true
---

# Running a large Workflow fan-out at scale

Patterns validated on a multi-hundred-agent effort (reproducing Cassandra bugs in kind, then generating
~85 benchmark Problem files). They address the failures that only show up at scale: resource exhaustion,
runaway spend, unreliable inputs, slow stragglers, and over-trusting agent self-reports.

## 1. Pilot-gate the spend
Never launch the full fan-out blind. Run a small pilot (~10 items), block on it, and check three things
before releasing the rest: (a) did agents actually do cleanup/teardown they were told to? (b) do the
"success" verdicts hold up against the durable artifact (e.g. the claimed signature is really in the log)?
(c) is the success rate and per-item cost sane? A pilot bounds a systematic-failure blast radius to ~10×
one item instead of N×. The pilot is also where you discover input-delivery bugs (see #2).

## 2. Pass batches via a file, not `args`
Workflow-script `args` delivery can be unreliable (a script may silently see `undefined`/defaults). For
anything non-trivial, write the batch to a file (e.g. `/tmp/batch.json`) and have a trivial loader agent
`cat` it and return it. This is deterministic and avoids inlining large arrays into the tool call. Confirm
the script actually received the batch (log the loaded count) before fanning out.

## 3. Bound concurrency with a worker pool — not unbounded, not chunked
- **Unbounded `parallel`** (up to the global cap) can exhaust disk/memory when each agent is heavy.
- **Chunked `parallel`** (await each chunk) puts a barrier between chunks, so one slow agent blocks its
  whole chunk and wastes the others' idle time.
- **Worker pool** (N workers pulling from a shared index, no barrier) is the right default for heavy items:
  exactly N run at once and a slow agent occupies only its own slot. Tune N to the binding resource, not
  the CPU cap.
```
async function runPool(items, conc, fn) {
  const out = new Array(items.length); let next = 0
  async function worker() { while (true) { const i = next++; if (i >= items.length) return; out[i] = await fn(items[i], i) } }
  await Promise.all(Array.from({ length: Math.min(conc, items.length) }, () => worker()))
  return out
}
```

## 4. Resource discipline (disk/memory is usually the real limit)
If each agent spins up containers/clusters, the host resource — not the agent cap — is what fails. On a
small disk: bound concurrency low, have each agent tear down what it created, and reclaim shared resources
between waves (for kind: `kubectl delete ns ...` + `crictl rmi --prune` inside the nodes — accumulated
images, not pod data, are often the hog). Tear down idle state you no longer need. Pure code-generation
(writing files) is light and can run at full parallelism. Monitor the binding resource between waves and
stop early if you can't hold headroom — surface a provisioning blocker to the user rather than grinding.

## 5. Make runs idempotent / resumable
Have each agent write a per-item done-marker (an output file, a row). The loader skips items whose marker
exists, so a crash, a kill, or a script edit re-runs only what's left. This makes the whole fan-out safe to
stop and resume, and turns transient failures (socket errors, timeouts) into a cheap re-run.

## 6. Don't trust self-reports — verify against the durable artifact
Agents optimistically claim success. Require a **verbatim** evidence token in the structured result (an
exact error line, a concrete wrong value) and **mechanically check it against the artifact** (grep the
claimed signature in the log/file); downgrade any miss. For code-generation, verify **statically** —
compile-check and load/parse the output — and **never instantiate/deploy** generated objects to "verify"
them if that triggers expensive side effects (a build, a deploy). Spot-read a sample for fidelity that a
grep can't catch (did the agent grab the control instead of the buggy path? drop a precondition?).

## 7. Checkpoint durably between phases
A multi-hour run that records only at the end is one crash from losing everything. Bank results to an
accumulator file and integrate them into the durable docs between waves/phases. Run several scoped
workflows in sequence (understand → act → verify), reading each result before deciding the next, rather
than one monolith.

## 8. Expect and handle nulls
At scale you will get timeouts, socket errors, and occasional content-policy/safeguard false-positives
(common on security/CVE/auth-flavored tasks). Record these as `inconclusive — needs manual` rather than
dropping them; for the safeguard cases, assess read-only yourself instead of re-spawning the same agent.

## When NOT to fan out
If the work is a deterministic transform of data you already have (templating files from a JSON spec),
a script is cheaper and more reliable than agents. Reserve the fan-out for work that needs per-item
judgment (classifying, reproducing, extracting a clean reproducer from prose, reviewing).
