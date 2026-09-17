"""Bounded, read-only collection of recent application observations."""

import asyncio
import json
import re
import subprocess
from datetime import UTC, datetime

from clients.jev.evidence import Observation

_SENSITIVE = re.compile(
    r"(?i)(?:authorization|password|passwd|access[_-]?token|refresh[_-]?token|api[_-]?key|secret)"
    r"[\s\"']*[:=][\s\"']*\S+|bearer\s+\S+|https?://[^\s/]+:[^\s/]+@"
    r"|\bsk-[A-Za-z0-9_-]{12,}|\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\."
)


def safe_log_excerpt(text: str) -> str:
    """Omit obvious credential lines; this is not a general secret detector."""
    if "PRIVATE KEY" in text:
        return "[Log omitted: private-key material]"
    return "\n".join(
        "[Credential-like log line omitted]" if _SENSITIVE.search(line) else line for line in text.splitlines()
    )


async def collect_observations(namespace: str) -> list[Observation]:
    if not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?", namespace):
        raise ValueError("Use a valid Kubernetes namespace name")
    result = await asyncio.to_thread(
        subprocess.run,
        ["kubectl", "get", "pods", "-n", namespace, "--request-timeout=3s", "-o", "json"],
        capture_output=True,
        text=True,
        timeout=4,
        check=False,
    )
    if result.returncode or len(result.stdout.encode()) > 4_194_304:
        raise ValueError("Cannot collect bounded Pod state")
    document = json.loads(result.stdout)
    pods = document.get("items") if isinstance(document, dict) else None
    if not isinstance(pods, list) or not 1 <= len(pods) <= 64:
        raise ValueError("Expected between 1 and 64 Pods")
    semaphore = asyncio.Semaphore(8)

    async def inspect(pod):
        name = pod["metadata"]["name"]
        status = pod.get("status", {})
        state = {
            "phase": status.get("phase"),
            "containers": [
                {
                    "name": c.get("name"),
                    "ready": c.get("ready"),
                    "restarts": c.get("restartCount"),
                    "state": {
                        k: {f: v[f] for f in ("reason", "exitCode", "startedAt", "finishedAt") if f in v}
                        for k, v in c.get("state", {}).items()
                    },
                }
                for c in status.get("containerStatuses", [])
            ],
        }
        async with semaphore:
            try:
                logs = await asyncio.to_thread(
                    subprocess.run,
                    [
                        "kubectl",
                        "logs",
                        name,
                        "-n",
                        namespace,
                        "--all-containers=true",
                        "--prefix=true",
                        "--timestamps=true",
                        "--since=60s",
                        "--tail=20",
                        "--limit-bytes=2200",
                        "--request-timeout=3s",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=4,
                    check=False,
                )
                excerpt = safe_log_excerpt(logs.stdout) if logs.returncode == 0 else "[Recent logs unavailable]"
            except (OSError, subprocess.TimeoutExpired):
                excerpt = "[Recent log collection timed out or failed]"
        output = json.dumps(state) + "\nRecent logs (last 60 seconds, bounded tail):\n" + excerpt
        return Observation(
            source=f"pod/{name}: current status and recent logs",
            observed_at=datetime.now(UTC).isoformat(),
            output=output[:3000],
        )

    return await asyncio.gather(*(inspect(pod) for pod in sorted(pods, key=lambda p: p["metadata"]["name"])))
