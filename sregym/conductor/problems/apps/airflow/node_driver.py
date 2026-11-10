"""Conductor-side process control on kind nodes, for the Airflow shutdown-window problem.

Everything here runs from the conductor through ``docker exec`` on the kind node
container. Nothing is written into the cluster and nothing is visible through
the Kubernetes API: the agent has no node access, and the processes and shells
used here end before handover.
"""

from __future__ import annotations

import contextlib
import queue
import subprocess
import threading
import time

SIGTERM_MASK = 1 << (15 - 1)


class NodeShell:
    """A persistent ``sh`` on a kind node, so a signal can be sent within milliseconds."""

    def __init__(self, container: str):
        self.container = container
        self.proc = subprocess.Popen(
            ["docker", "exec", "-i", container, "sh"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        self.lines: queue.Queue[str] = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()
        self._seq = 0

    def _read(self):
        for line in self.proc.stdout:
            self.lines.put(line.rstrip("\n"))

    def send(self, command: str) -> None:
        """Fire and forget: the command runs as soon as the shell reads it."""
        self.proc.stdin.write(command + "\n")
        self.proc.stdin.flush()

    def run(self, command: str, timeout: float = 60) -> list[str]:
        self._seq += 1
        marker = f"__done_{self._seq}__"
        self.send(f"{command}; echo {marker}")
        out = []
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"node command timed out: {command[:120]}")
            try:
                line = self.lines.get(timeout=remaining)
            except queue.Empty:
                continue
            if line == marker:
                return out
            out.append(line)

    def close(self):
        with contextlib.suppress(Exception):
            self.proc.stdin.close()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def container_processes(shell: NodeShell, pod: str, container: str) -> list[dict]:
    """Processes of one container, from the node: [{pid, ppid, args}], root first."""
    root = shell.run(
        f"cid=$(crictl ps -q --label io.kubernetes.pod.name={pod} --label io.kubernetes.container.name={container}); "
        "crictl inspect -o go-template --template '{{.info.pid}}' $cid"
    )
    root_pid = int(root[-1].strip())
    rows = shell.run("ps -e -o pid=,ppid=,args=")
    procs = {}
    for row in rows:
        parts = row.split(None, 2)
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            procs[int(parts[0])] = {
                "pid": int(parts[0]),
                "ppid": int(parts[1]),
                "args": parts[2] if len(parts) > 2 else "",
            }
    tree, frontier = [], [root_pid]
    while frontier:
        pid = frontier.pop(0)
        if pid in procs:
            tree.append(procs[pid])
            frontier.extend(sorted(p for p, info in procs.items() if info["ppid"] == pid))
    return tree


def signal_pending(shell: NodeShell, pid: int, mask: int = SIGTERM_MASK) -> bool:
    """True once a signal in ``mask`` is pending (process- or thread-directed) on a stopped process."""
    rows = shell.run(f"grep -E '^(SigPnd|ShdPnd):' /proc/{pid}/status 2>/dev/null")
    if not rows:
        return True  # the process is gone
    return any(int(row.split()[1], 16) & mask for row in rows)
