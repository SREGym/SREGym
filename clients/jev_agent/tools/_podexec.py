"""Commands run inside pods (kubectl exec) and the choice of pod to run them in.

Tool modules (and crafted.py) call `_podexec._exec(...)`, so a test that stubs the exec patches it here, once.
"""

from __future__ import annotations

from clients.jev_diag import collector, probes


def _ready_pod(comp: dict) -> tuple[str | None, str | None]:
    """A Running pod whose containers report ready (a crash-looping container cannot host an exec)."""
    pods = [
        p
        for p in comp.get("pods") or []
        if p.get("phase") == "Running" and str(p.get("ready", "0/0")).split("/")[0] != "0"
    ]
    containers = [c for c in comp.get("containers") or [] if not c.get("init")]
    if not pods or not containers:
        return None, None
    return pods[0].get("name"), containers[0].get("name")


# kubectl-level exec failures: the command never ran in the container, so its exit code says nothing about the
# system being diagnosed. Such results must become tool errors, never evidence.
_EXEC_TRANSPORT_FAILURES = (
    "not in the replay bundle",
    "unable to upgrade connection",
    "error dialing backend",
    "forbidden",
    "container not found",
    "is not valid for pod",
    "cannot exec into a container",
    "error from server",
    "oci runtime exec failed",
    "timed out waiting for",
)


class ExecUnavailable(RuntimeError):
    """kubectl exec itself failed (permissions, replay miss, container gone)."""


def _exec(ns: str, pod: str, container: str | None, command: list[str]) -> tuple[int | None, str]:
    """(exit code, output) of a command run in a pod's container, 127 for a missing binary. Raises ExecUnavailable
    when kubectl exec itself failed, so the result says nothing about the system."""
    rc, text = probes.run_in_pod(ns, pod, container, command)
    low = (text or "").lower()
    if rc not in (None, 0) and ("executable file not found" in low or "no such file or directory" in low):
        return 127, text  # the command ran into a missing binary: callers treat 127 as "tool not in the image"
    if rc is None or (rc != 0 and any(marker in low for marker in _EXEC_TRANSPORT_FAILURES)):
        raise ExecUnavailable(f"exec in {pod} could not run: {collector.trim(text or 'no output', 120)}")
    return rc, text


def _missing_tool(rc: int | None, text: str) -> bool:
    """Whether an exec result says the command is not in the image."""
    low = text.lower()[:300]
    return rc in (126, 127) or "executable file not found" in low or ("not found" in low and "nxdomain" not in low)
