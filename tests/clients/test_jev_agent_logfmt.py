"""Structured (JSON) log lines rewritten so what they say comes first."""

from __future__ import annotations

import json

from clients.jev_agent import logfmt, tools


def test_a_mongodb_json_line_puts_the_storage_error_first():
    obj = {
        "t": {"$date": "2026-10-11T00:25:16.863+00:00"},
        "s": "E",
        "c": "STORAGE",
        "id": 22435,
        "ctx": "initandlisten",
        "msg": "WiredTiger error",
        "attr": {
            "error": 0,
            "message": "[1791678316:863177][1:0xffff91c6345]: file:_mdb_catalog.wt: read checksum error for 4096B block",
        },
    }
    line = "[pod/db-1/mongo] 2026-10-11T00:25:16.863Z " + json.dumps(obj)
    out = logfmt.compact(line)
    assert out.startswith("[pod/db-1/mongo] 2026-10-11T00:25:16.863Z ERROR STORAGE WiredTiger error: message=file:")
    assert "read checksum error" in out[:200] and "initandlisten" not in out and "22435" not in out


def test_other_lines_are_left_alone():
    for line in ("plain text error: connection refused", "map {a: 1} and more", '{"not": "closed"'):
        assert logfmt.compact(line) == line


def test_a_go_style_json_line():
    line = json.dumps({"level": "error", "ts": 1.7e9, "caller": "x.go:12", "msg": "query failed", "error": "deadlock"})
    assert logfmt.compact(line) == "ERROR query failed: error=deadlock"


def test_crash_summary_keeps_the_first_distinct_error_before_a_dump():
    tail = (
        ["ERROR read checksum error for 4096B block at offset 32768"]
        + [f"ERROR corrupt dump (chunk {i} of 40): 84 69 10 c1" for i in range(1, 41)]
        + ["FATAL Fatal assertion msgid=50853", "FATAL aborting after fassert() failure", "FATAL Got signal: 6"]
    )
    words = tools._crash_words(tail)
    assert words[0].startswith("ERROR read checksum error") and words[-1] == "FATAL Got signal: 6"
    assert len(words) == 3
