"""Summarise the request ledger of an Incident Arena load generator.

The Incident Arena load generators (``loadgen-common`` in abundant-ai/sre-world)
append one JSON record per scheduled arrival to ``$GRADER_DIR/loadgen.jsonl``::

    {"phase": "c12.peak", "sent_s": 731.2, "latency_ms": 18.4, "ok": true,
     "correct": true, "timeout": false, "dropped": false, "driver": "write_readback", ...}

``sent_s`` is relative to the generator's own clock origin, so SREGym windows
the ledger by first reading the newest ``sent_s`` (``latest`` mode) and later
summarising every record sent after that mark (``summary`` mode). ``status``
mode adds the sidecar's ``episode_done.json`` and log tail, which explain an
episode that ended before sending anything.

This module is stdlib-only on purpose: SREGym pipes its source into the load
generator pod (``python3 - <mode> ...``) so the ledger never has to leave the
pod, and imports it directly in tests. The arithmetic mirrors the Incident
Arena outcome gate (``tests/verifier/providers/outcome.py``): error rate counts
timeouts and non-ok responses over non-dropped arrivals, goodput counts
ok-and-correct responses over the same base, and latency uses a nearest-rank
percentile per phase kind (peak/trough).
"""

from __future__ import annotations

import json
import math
import os
import sys

LEDGER_NAME = "loadgen.jsonl"
# Written by the sidecar when its episode ends, including when it fails to start.
EPISODE_DONE_NAME = "episode_done.json"
# The sidecar's own log, teed into the grader directory (Saleor only).
SIDECAR_LOG_NAME = "sidecar.log"
DEFAULT_TAIL_BYTES = 262144
LOG_TAIL_BYTES = 8192


def grader_dir() -> str:
    return os.environ.get("GRADER_DIR", "/grader")


def ledger_path() -> str:
    return os.path.join(grader_dir(), LEDGER_NAME)


def read_episode_done(directory):
    """The sidecar's episode-end record, or None while the episode runs."""
    try:
        with open(os.path.join(directory, EPISODE_DONE_NAME), encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, ValueError):
        return None
    return document if isinstance(document, dict) else {"raw": document}


def read_log_tail(directory, tail_bytes=LOG_TAIL_BYTES):
    """The last lines of the sidecar log, or None when the sidecar keeps none."""
    try:
        with open(os.path.join(directory, SIDECAR_LOG_NAME), "rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - tail_bytes))
            return handle.read().decode("utf-8", errors="replace").splitlines()[-40:]
    except OSError:
        return None


def parse_records(lines):
    """Yield arrival records, skipping summary rows and torn or foreign lines."""
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict) and "sent_s" in record and "phase" in record:
            yield record


def latest_sent_s(path, tail_bytes=DEFAULT_TAIL_BYTES):
    """Return the newest ``sent_s`` in the ledger, or None when it is empty."""
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - tail_bytes))
            tail = handle.read().decode("utf-8", errors="replace")
    except FileNotFoundError:
        return None
    lines = tail.splitlines()
    if size > tail_bytes and lines:
        # The first line of a tail read is usually cut in half.
        lines = lines[1:]
    sent = [float(r["sent_s"]) for r in parse_records(lines) if r.get("sent_s") is not None]
    return max(sent) if sent else None


def phase_kind(phase):
    phase = str(phase)
    if phase == "peak" or phase.endswith(".peak"):
        return "peak"
    if phase == "trough" or phase.endswith(".trough"):
        return "trough"
    return None


def percentile(values, pct):
    """Nearest-rank percentile (ceil(pct/100 * n), 1-indexed); None when empty."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, min(len(ordered), math.ceil((pct / 100.0) * len(ordered))))
    return float(ordered[rank - 1])


def _new_bucket():
    return {"offered": 0, "dropped": 0, "failures": 0, "good": 0, "latency": {"peak": [], "trough": []}}


def _finish(bucket, pct):
    offered = bucket["offered"]
    latency = {}
    for kind, values in bucket["latency"].items():
        latency[kind] = {"n": len(values), "p_ms": percentile(values, pct)}
    return {
        "offered": offered,
        "dropped": bucket["dropped"],
        "failures": bucket["failures"],
        "good": bucket["good"],
        "error_rate": (bucket["failures"] / offered) if offered else None,
        "goodput_ratio": (bucket["good"] / offered) if offered else None,
        "latency": latency,
    }


def summarize(records, since_s=None, latency_percentile=99.0, settle_s=0.0):
    """Aggregate arrivals sent strictly after ``since_s``.

    Latency samples sent within ``settle_s`` of ``since_s`` are excluded (the
    Incident Arena "settle window"); error rate and goodput keep them.
    """
    overall = _new_bucket()
    by_driver = {}
    first = last = None
    settle_cutoff = None if since_s is None else float(since_s) + float(settle_s or 0.0)
    for record in records:
        sent = record.get("sent_s")
        if sent is None:
            continue
        sent = float(sent)
        if since_s is not None and sent <= float(since_s):
            continue
        first = sent if first is None else min(first, sent)
        last = sent if last is None else max(last, sent)
        driver = str(record.get("driver") or "unknown")
        buckets = (overall, by_driver.setdefault(driver, _new_bucket()))
        if record.get("dropped"):
            for bucket in buckets:
                bucket["dropped"] += 1
            continue
        failed = bool(record.get("timeout")) or not record.get("ok", False)
        good = bool(record.get("ok", False)) and bool(record.get("correct", False))
        kind = phase_kind(record.get("phase", ""))
        latency = record.get("latency_ms")
        keep_latency = kind is not None and latency is not None and (settle_cutoff is None or sent >= settle_cutoff)
        for bucket in buckets:
            bucket["offered"] += 1
            bucket["failures"] += int(failed)
            bucket["good"] += int(good)
            if keep_latency:
                bucket["latency"][kind].append(float(latency))
    summary = _finish(overall, latency_percentile)
    summary["since_s"] = since_s
    summary["first_sent_s"] = first
    summary["last_sent_s"] = last
    summary["latency_percentile"] = latency_percentile
    summary["by_driver"] = {name: _finish(bucket, latency_percentile) for name, bucket in sorted(by_driver.items())}
    return summary


def main(argv):
    path = ledger_path()
    mode = argv[1] if len(argv) > 1 else "latest"
    if mode == "latest":
        print(json.dumps({"latest_sent_s": latest_sent_s(path)}))
        return 0
    if mode == "status":
        directory = grader_dir()
        print(
            json.dumps(
                {
                    "latest_sent_s": latest_sent_s(path),
                    "episode_done": read_episode_done(directory),
                    "log_tail": read_log_tail(directory),
                }
            )
        )
        return 0
    if mode == "summary":
        since_s = float(argv[2]) if len(argv) > 2 and argv[2] != "none" else None
        pct = float(argv[3]) if len(argv) > 3 else 99.0
        settle_s = float(argv[4]) if len(argv) > 4 else 0.0
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                print(json.dumps(summarize(parse_records(handle), since_s, pct, settle_s)))
        except FileNotFoundError:
            print(json.dumps({"error": "ledger_missing", "path": path}))
        return 0
    print(json.dumps({"error": "unknown_mode", "mode": mode}))
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
