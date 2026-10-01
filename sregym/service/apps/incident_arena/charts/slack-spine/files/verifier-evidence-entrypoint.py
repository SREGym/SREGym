"""Durable verifier episode ownership for the pinned v13 loadgen image."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import os
from typing import Any


def claim_episode(sidecar: Any) -> str:
    """Return new, finalized, or interrupted without replacing any evidence."""
    grader = sidecar.GRADER
    done = sidecar.EPISODE_DONE_JSON
    started = grader / ".episode-started.json"
    grader.mkdir(parents=True, exist_ok=True)
    if done.exists():
        receipt = json.loads(done.read_text(encoding="utf-8"))
        if not isinstance(receipt, dict) or not isinstance(receipt.get("done"), bool):
            raise RuntimeError("persistent episode receipt has an invalid shape")
        return "finalized"
    try:
        fd = os.open(started, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return "interrupted"
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(
            {"started_at": datetime.now(timezone.utc).isoformat()},
            handle,
            sort_keys=True,
        )
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    return "new"


async def _serve_only(sidecar: Any) -> None:
    state = {
        "lg": None,
        "grader_access_token": sidecar.load_grader_access_token(),
        "require_baseline": sidecar.SOURCE_SNAPSHOT_ENABLED,
        "baseline_ready": not sidecar.SOURCE_SNAPSHOT_ENABLED,
    }
    await sidecar.start_http_server(state)
    await sidecar._sleep_forever()


def main() -> None:
    import loadgen_sidecar as sidecar

    # Future images may own the same protocol natively. Do not claim their
    # marker before delegating or the native code would classify itself as a
    # restart.
    if hasattr(sidecar, "_claim_or_resume_episode"):
        sidecar.main()
        return

    state = claim_episode(sidecar)
    if state == "new":
        sidecar.main()
        return
    if state == "interrupted":
        error = (
            "InfrastructureEvidenceRestart: loadgen restarted while evidence "
            "collection was in flight; partial evidence was frozen and the "
            "episode was not rerun"
        )
        sidecar._write_episode_done(
            {
                "done": False,
                "error": error,
                "declare_ts_s": None,
                "soak_start_s": None,
                "end_s": None,
            }
        )
    asyncio.run(_serve_only(sidecar))


if __name__ == "__main__":
    main()
