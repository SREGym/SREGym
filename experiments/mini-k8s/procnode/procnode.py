#!/usr/bin/env python3
"""procnode: a container-runtime-free node agent for a lightweight SREGym cluster.

Runs every pod bound to a procnode-managed node as ordinary Linux processes:

  network    one netns per pod, /32 address routed through the host (Calico
             style), so host iptables -- i.e. a real kube-proxy -- sees all traffic
  rootfs     each image unpacked once, overlay-mounted per container instance
  isolation  shared net/uts/ipc per pod; private mount+pid namespace per container
  limits     cgroup v1 memory/cpu per container (OOMKilled / CFS throttling are real)
  lifecycle  kubelet semantics: init containers, native sidecars, probes,
             CrashLoopBackOff, restartPolicy, graceful termination, events
  streaming  CRI-format log files plus per-pod kwok Logs/Exec/PortForward
             objects, so `kubectl logs/exec/port-forward` go through kwok-controller
  storage    a tiny hostPath provisioner binds pending PVCs

KWOK still owns Node objects (registration, leases, the kubelet endpoint that
the apiserver dials for logs/exec). procnode only owns pods.

This is an experiment, not production code: it runs as root, trusts images,
and skips many kubelet features (see README.md).
"""

import argparse
import asyncio
import base64
import contextlib
import datetime as dt
import ipaddress
import json
import logging
import os
import re
import shlex
import shutil
import signal
import socket
import threading
import time
from pathlib import Path

from kubernetes import client, config, watch
from kubernetes.client.rest import ApiException
from netpol import PROBE_MARK, Enforcer

log = logging.getLogger("procnode")

MANAGED_LABEL = "procnode.sregym.io/managed"
KWOK_GROUP, KWOK_VERSION = "kwok.x-k8s.io", "v1alpha1"
PROVISIONER = "procnode.sregym.io/hostpath"


def now():
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_cpu(q):
    if q is None:
        return None
    q = str(q)
    if q.endswith("m"):
        return int(q[:-1])
    return int(float(q) * 1000)


_MEM_UNITS = {
    "Ki": 2**10,
    "Mi": 2**20,
    "Gi": 2**30,
    "Ti": 2**40,
    "K": 10**3,
    "M": 10**6,
    "G": 10**9,
    "T": 10**12,
    "k": 10**3,
}


def parse_mem(q):
    if q is None:
        return None
    q = str(q)
    for u, m in _MEM_UNITS.items():
        if q.endswith(u):
            return int(float(q[: -len(u)]) * m)
    return int(float(q))


async def run(*args, check=True, input=None):
    p = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE if input else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await p.communicate(input)
    if check and p.returncode != 0:
        raise RuntimeError(f"{' '.join(map(str, args))}: rc={p.returncode} {err.decode(errors='replace').strip()}")
    return out.decode(errors="replace")


def in_root(root, path):
    """Resolve `path` inside `root` like runc's securejoin: symlinks, absolute
    or relative, are followed but can never escape `root`."""
    root = Path(root)
    parts = [p for p in str(path).split("/") if p]
    cur, hops = [], 0
    while parts:
        comp = parts.pop(0)
        if comp == ".":
            continue
        if comp == "..":
            if cur:
                cur.pop()
            continue
        host = root.joinpath(*cur, comp)
        if host.is_symlink():
            hops += 1
            if hops > 40:
                raise PodError("CreateContainerError", f"too many levels of symbolic links in {path}")
            link = os.readlink(host)
            if link.startswith("/"):
                cur = []
            parts = [p for p in link.split("/") if p] + parts
            continue
        cur.append(comp)
    return root.joinpath(*cur)


async def probe_connect(host, port):
    """Connect like a kubelet probe: marked so NetworkPolicy lets it through."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_MARK, PROBE_MARK)
    sock.setblocking(False)
    try:
        await asyncio.get_running_loop().sock_connect(sock, (host, port))
    except OSError:
        sock.close()
        raise
    return await asyncio.open_connection(sock=sock)


class PodError(Exception):
    """A pod-level failure that maps onto a kubelet waiting reason."""

    def __init__(self, reason, message):
        super().__init__(message)
        self.reason, self.message = reason, message


# --------------------------------------------------------------------------- images


class ImageStore:
    """Unpacks images with the host's docker into plain directories."""

    def __init__(self, root):
        self.root = Path(root) / "images"
        self.root.mkdir(parents=True, exist_ok=True)
        self.locks = {}
        self.pulled = set()

    def _lock(self, key):
        return self.locks.setdefault(key, asyncio.Lock())

    async def ensure(self, image, policy):
        async with self._lock(image):
            present = await self._inspect(image)
            if policy == "Never" and present is None:
                raise PodError(
                    "ErrImageNeverPull", f'Container image "{image}" is not present with pull policy of Never'
                )
            if present is None or (policy == "Always" and image not in self.pulled):
                try:
                    await run("docker", "pull", "-q", image)
                except RuntimeError as e:
                    if present is None:
                        raise PodError("ErrImagePull", f'Failed to pull image "{image}": {e}') from e
                self.pulled.add(image)
                present = await self._inspect(image)
            image_id, cfg = present
        rootfs = self.root / image_id.split(":")[-1][:16]
        async with self._lock(str(rootfs)):
            if not (rootfs / ".ready").exists():
                tmp = rootfs.with_suffix(".tmp")
                shutil.rmtree(tmp, ignore_errors=True)
                tmp.mkdir(parents=True)
                cid = (await run("docker", "create", image, "/")).strip()
                try:
                    p1 = await asyncio.create_subprocess_shell(
                        f"docker export {cid} | tar -x -C {shlex.quote(str(tmp))}",
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    _, err = await p1.communicate()
                    if p1.returncode != 0:
                        raise RuntimeError(err.decode())
                finally:
                    await run("docker", "rm", "-f", cid, check=False)
                (tmp / ".ready").touch()
                tmp.rename(rootfs)
        return image_id, cfg, rootfs

    async def _inspect(self, image):
        try:
            out = await run("docker", "image", "inspect", image)
        except RuntimeError:
            return None
        info = json.loads(out)[0]
        return info["Id"], info.get("Config") or {}


# --------------------------------------------------------------------------- cri logs


class CRILog:
    """Writes the CRI log format kwok-controller (via cri-client) reads."""

    def __init__(self, path):
        self.path = path
        self.f = open(path, "ab", buffering=0)  # noqa: SIM115 - lives as long as the container

    def write(self, stream, data, partial=False):
        ts = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.%f000Z").encode()
        self.f.write(b"%s %s %s %s\n" % (ts, stream, b"P" if partial else b"F", data))

    def close(self):
        self.f.close()


async def pump(reader, stream, sink, max_line=16384):
    buf = b""
    while True:
        chunk = await reader.read(65536)
        if not chunk:
            break
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            sink.write(stream, line)
        while len(buf) > max_line:
            sink.write(stream, buf[:max_line], partial=True)
            buf = buf[max_line:]
    if buf:
        sink.write(stream, buf)


# --------------------------------------------------------------------------- containers


class Container:
    def __init__(self, pod, spec, kind):
        self.pod, self.spec, self.kind = pod, spec, kind  # kind: init | sidecar | app
        self.name = spec["name"]
        self.dir = pod.dir / "containers" / self.name
        self.restart_count = 0
        self.state = {"waiting": {"reason": "ContainerCreating"}}
        self.last_state = {}
        self.ready = False
        self.started = False
        self.startup_ok = "startupProbe" not in spec
        self.proc = None
        self.pid1 = None
        self.image_id = ""
        self.exited = asyncio.Event()
        self.kill_reason = None
        self.cgroups = []

    # -- status
    def status(self):
        s = {
            "name": self.name,
            "image": self.spec["image"],
            "imageID": self.image_id,
            "containerID": f"procnode://{self.pod.uid[:8]}-{self.name}-{self.restart_count}",
            "ready": self.ready,
            "started": self.started,
            "restartCount": self.restart_count,
            "state": self.state,
        }
        if self.last_state:
            s["lastState"] = self.last_state
        return s

    # -- process
    async def prepare_and_start(self, store):
        pod, spec = self.pod, self.spec
        policy = spec.get("imagePullPolicy") or (
            "Always" if spec["image"].endswith(":latest") or ":" not in spec["image"].split("/")[-1] else "IfNotPresent"
        )
        pod.event("Normal", "Pulling", f'Pulling image "{spec["image"]}"', self)
        t0 = time.time()
        self.image_id, icfg, rootfs = await store.ensure(spec["image"], policy)
        pod.event("Normal", "Pulled", f'Successfully pulled image "{spec["image"]}" in {time.time() - t0:.3f}s', self)

        env = await pod.resolve_env(spec, icfg)
        argv = self._argv(icfg, env)
        if not argv:
            raise PodError("CreateContainerError", "no command specified")
        uid, gid = self._user(icfg, rootfs)

        inst = self.dir / str(self.restart_count)
        await self.teardown_fs()
        shutil.rmtree(self.dir, ignore_errors=True)
        for d in ("upper", "work", "merged"):
            (inst / d).mkdir(parents=True, exist_ok=True)
        merged = inst / "merged"
        await run(
            "mount",
            "-t",
            "overlay",
            "overlay",
            "-o",
            f"lowerdir={rootfs},upperdir={inst / 'upper'},workdir={inst / 'work'}",
            str(merged),
        )
        self.merged = merged
        await self._mount_basics(merged)
        for vm in spec.get("volumeMounts") or []:
            await pod.mount_volume(vm, merged)

        wd = spec.get("workingDir") or icfg.get("WorkingDir") or "/"
        in_root(merged, wd).mkdir(parents=True, exist_ok=True)
        self._setup_cgroups()
        # Exec helper inputs: current pid, env and user of this container.
        (self.dir / "env.json").write_text(json.dumps(env))
        (self.dir / "user.json").write_text(json.dumps([uid, gid]))

        ns = pod.ns_args()
        inner = [
            "/usr/bin/unshare",
            "--mount",
            "--pid",
            "--fork",
            "--kill-child",
            "--mount-proc",
            f"--root={merged}",
            f"--wd={wd}",
        ]
        if uid:
            inner += [f"--setgid={gid}", f"--setuid={uid}"]
        enter = " ".join(f"echo $$ > {c}/cgroup.procs;" for c in self.cgroups)
        cmd = ["/bin/sh", "-c", f'{enter} exec "$@"', "sh", "/usr/bin/nsenter", *ns, "--", *inner, "--", *argv]

        logdir = pod.logdir / self.name
        logdir.mkdir(parents=True, exist_ok=True)
        # kubelet's layout (<restart>.log), so node log shippers such as
        # promtail work unchanged; kwok's Logs object follows the symlinks.
        cur, prev = logdir / "current", logdir / "previous"
        if cur.is_symlink():
            prev.unlink(missing_ok=True)
            prev.symlink_to(os.readlink(cur))
        log_file = logdir / f"{self.restart_count}.log"
        sink = CRILog(log_file)
        cur.unlink(missing_ok=True)
        cur.symlink_to(log_file.name)
        self.proc = await asyncio.create_subprocess_exec(
            *cmd,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        self.pid1 = await self._find_pid1()
        (self.dir / "pid").write_text(str(self.pid1 or self.proc.pid))
        self.exited.clear()
        self.kill_reason = None
        started_at = now()
        self.state = {"running": {"startedAt": started_at}}
        self.started = self.startup_ok
        self.ready = False
        pod.event("Normal", "Created", f"Created container: {self.name}", self)
        pod.event("Normal", "Started", f"Started container {self.name}", self)

        async def waiter():
            await asyncio.gather(pump(self.proc.stdout, b"stdout", sink), pump(self.proc.stderr, b"stderr", sink))
            rc = await self.proc.wait()
            sink.close()
            code = 128 - rc if rc < 0 else rc
            reason = "Completed" if code == 0 else "Error"
            if self._oom_killed():
                reason, code = "OOMKilled", 137
            self.state = {
                "terminated": {
                    "exitCode": code,
                    "reason": reason,
                    "startedAt": started_at,
                    "finishedAt": now(),
                    "containerID": self.status()["containerID"],
                }
            }
            self.ready = False
            self.exited.set()
            pod.mark_dirty()

        asyncio.create_task(waiter())
        pod.mark_dirty()

    async def _find_pid1(self):
        # nsenter execs unshare, which forks the container's pid 1.
        for _ in range(100):
            try:
                kids = Path(f"/proc/{self.proc.pid}/task/{self.proc.pid}/children").read_text().split()
                if kids:
                    return int(kids[0])
            except OSError:
                return None
            await asyncio.sleep(0.02)
        return None

    def _argv(self, icfg, env):
        def expand(s):
            return re.sub(
                r"\$\$|\$\(([A-Za-z_][A-Za-z0-9_]*)\)",
                lambda m: "$" if m.group(0) == "$$" else env.get(m.group(1), m.group(0)),
                s,
            )

        ep, cmd = icfg.get("Entrypoint") or [], icfg.get("Cmd") or []
        command, args = self.spec.get("command"), self.spec.get("args")
        if command:
            argv = [expand(c) for c in command] + [expand(a) for a in (args or [])]
        elif args:
            argv = list(ep) + [expand(a) for a in args]
        else:
            argv = list(ep) + list(cmd)
        return argv

    def _user(self, icfg, rootfs):
        sc = {**(self.pod.spec.get("securityContext") or {}), **(self.spec.get("securityContext") or {})}
        uid, gid = sc.get("runAsUser"), sc.get("runAsGroup")
        if uid is None and icfg.get("User"):
            u, _, g = icfg["User"].partition(":")
            uid = self._lookup(rootfs / "etc/passwd", u)
            if g:
                gid = self._lookup(rootfs / "etc/group", g)
            elif uid:
                gid = self._lookup_gid(rootfs / "etc/passwd", u)
        return int(uid or 0), int(gid if gid is not None else (uid or 0))

    @staticmethod
    def _lookup(path, name):
        if name.isdigit():
            return int(name)
        try:
            for line in path.read_text().splitlines():
                f = line.split(":")
                if f[0] == name:
                    return int(f[2])
        except OSError:
            pass
        return 0

    @staticmethod
    def _lookup_gid(path, name):
        if name.isdigit():
            return int(name)
        try:
            for line in path.read_text().splitlines():
                f = line.split(":")
                if f[0] == name:
                    return int(f[3])
        except OSError:
            pass
        return 0

    async def _mount_basics(self, merged):
        pod = self.pod
        dev = in_root(merged, "/dev")
        dev.mkdir(exist_ok=True)
        await run("mount", "-t", "tmpfs", "-o", "mode=755,size=65536k", "tmpfs", str(dev))
        for d in ("null", "zero", "full", "random", "urandom", "tty"):
            (dev / d).touch()
            await run("mount", "--bind", f"/dev/{d}", str(dev / d))
        (dev / "pts").mkdir()
        await run("mount", "-t", "devpts", "-o", "newinstance,ptmxmode=0666", "devpts", str(dev / "pts"))
        os.symlink("pts/ptmx", dev / "ptmx")
        for name, target in (
            ("fd", "/proc/self/fd"),
            ("stdin", "/proc/self/fd/0"),
            ("stdout", "/proc/self/fd/1"),
            ("stderr", "/proc/self/fd/2"),
        ):
            os.symlink(target, dev / name)
        (dev / "shm").mkdir()
        await run("mount", "--bind", str(pod.shm), str(dev / "shm"))
        sysd = in_root(merged, "/sys")
        sysd.mkdir(exist_ok=True)
        await run("mount", "--rbind", "-o", "ro", "/sys", str(sysd))
        for f in ("hosts", "resolv.conf", "hostname"):
            target = in_root(merged, f"/etc/{f}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.touch()
            await run("mount", "--bind", str(pod.dir / f), str(target))

    def _setup_cgroups(self):
        res = self.spec.get("resources") or {}
        lim, req = res.get("limits") or {}, res.get("requests") or {}
        base = f"procnode/{self.pod.uid[:12]}-{self.name}"
        self.cgroups = []
        mem = Path("/sys/fs/cgroup/memory") / base
        mem.mkdir(parents=True, exist_ok=True)
        limit = parse_mem(lim.get("memory"))
        (mem / "memory.limit_in_bytes").write_text(str(limit) if limit else "-1")
        self.oom_baseline = self._oom_count(mem)
        self.cgroups.append(str(mem))
        cpu = Path("/sys/fs/cgroup/cpu") / base
        cpu.mkdir(parents=True, exist_ok=True)
        cl, cr = parse_cpu(lim.get("cpu")), parse_cpu(req.get("cpu") or lim.get("cpu"))
        (cpu / "cpu.cfs_quota_us").write_text(str(cl * 100) if cl else "-1")
        (cpu / "cpu.shares").write_text(str(max(2, (cr or 2) * 1024 // 1000)))
        self.cgroups.append(str(cpu))
        acct = Path("/sys/fs/cgroup/cpuacct") / base
        acct.mkdir(parents=True, exist_ok=True)
        self.cgroups.append(str(acct))
        self.mem_cg = mem

    @staticmethod
    def _oom_count(mem):
        try:
            for line in (mem / "memory.oom_control").read_text().splitlines():
                if line.startswith("oom_kill "):
                    return int(line.split()[1])
        except OSError:
            pass
        return 0

    def _oom_killed(self):
        return getattr(self, "mem_cg", None) is not None and self._oom_count(self.mem_cg) > self.oom_baseline

    async def kill(self, grace, reason=None):
        if self.proc is None or self.proc.returncode is not None:
            return
        self.kill_reason = reason
        target = self.pid1 or self.proc.pid
        try:
            os.kill(target, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(self.exited.wait(), timeout=max(grace, 0.1))
        except TimeoutError:
            for pid in (target, self.proc.pid):
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)
            await self.exited.wait()

    async def teardown_fs(self):
        merged = getattr(self, "merged", None)
        if merged is not None:
            await run("umount", "-R", "-l", str(merged), check=False)
            self.merged = None

    def cleanup_cgroups(self):
        for c in self.cgroups:
            with contextlib.suppress(OSError):
                os.rmdir(c)

    # -- probes
    async def probe(self, probe):
        timeout = probe.get("timeoutSeconds", 1)
        try:
            if "exec" in probe:
                p = await asyncio.create_subprocess_exec(
                    str(self.pod.agent.exec_helper),
                    self.pod.uid,
                    self.name,
                    *probe["exec"]["command"],
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                try:
                    out, _ = await asyncio.wait_for(p.communicate(), timeout=timeout)
                except TimeoutError:
                    p.kill()
                    return False, f"command timed out after {timeout}s"
                return p.returncode == 0, out.decode(errors="replace")[-200:]
            if "httpGet" in probe:
                h = probe["httpGet"]
                port = self._port(h.get("port"))
                path = h.get("path", "/")
                r, w = await asyncio.wait_for(probe_connect(self.pod.ip, port), timeout)
                w.write(
                    f"GET {path} HTTP/1.1\r\nHost: {self.pod.ip}:{port}\r\nUser-Agent: kube-probe/1.33\r\nConnection: close\r\n\r\n".encode()
                )
                await w.drain()
                line = await asyncio.wait_for(r.readline(), timeout)
                w.close()
                code = int(line.split()[1])
                return 200 <= code < 400, f"HTTP probe failed with statuscode: {code}"
            if "tcpSocket" in probe or "grpc" in probe:
                port = self._port((probe.get("tcpSocket") or probe.get("grpc"))["port"])
                _, w = await asyncio.wait_for(probe_connect(self.pod.ip, port), timeout)
                w.close()
                return True, ""
        except Exception as e:  # noqa: BLE001 - every failure is a probe failure
            return False, f"{type(e).__name__}: {e}"
        return True, ""

    def _port(self, port):
        if isinstance(port, int) or str(port).isdigit():
            return int(port)
        for p in self.spec.get("ports") or []:
            if p.get("name") == port:
                return p["containerPort"]
        return 0


# --------------------------------------------------------------------------- pods


class Pod:
    def __init__(self, agent, obj):
        self.agent = agent
        self.obj = obj
        md, self.spec = obj["metadata"], obj["spec"]
        self.name, self.namespace, self.uid = md["name"], md["namespace"], md["uid"]
        self.node = self.spec["nodeName"]
        self.dir = agent.root / "pods" / self.uid
        self.logdir = agent.logroot / f"{self.namespace}_{self.name}_{self.uid}"
        self.netns = f"pn-{self.uid[:8]}"
        self.veth = f"pn{self.uid[:8]}"
        self.ip = None
        self.containers = []
        self.init_containers = []
        self.phase = "Pending"
        self.start_time = now()
        self.conditions = {}
        self.waiting_reason = None
        self.dirty = asyncio.Event()
        self.stopping = False
        self.task = None
        self.terminal = False
        self.events = {}
        self.volumes = {}
        self.host_network = bool(self.spec.get("hostNetwork"))
        # One host can only run one copy of a hostNetwork pod per port, so
        # hostNetwork pods on secondary nodes are simulated (status only).
        self.virtual = self.host_network and self.node != agent.primary_node

    # -- api helpers
    def event(self, etype, reason, message, container=None):
        self.agent.emit_event(self, etype, reason, message, container)

    def mark_dirty(self):
        self.dirty.set()

    def ns_args(self):
        d = self.agent.nsdir
        net = [] if self.host_network else [f"--net=/run/netns/{self.netns}"]
        return net + [f"--uts={d}/{self.uid}.uts", f"--ipc={d}/{self.uid}.ipc"]

    def cond(self, ctype, status):
        prev = self.conditions.get(ctype)
        if prev is None or prev["status"] != status:
            self.conditions[ctype] = {
                "type": ctype,
                "status": status,
                "lastTransitionTime": now(),
                "lastProbeTime": None,
            }

    def build_status(self):
        init_done = all(
            c.kind == "sidecar" and c.started or c.state.get("terminated", {}).get("exitCode") == 0
            for c in self.init_containers
        )
        apps_ready = (
            bool(self.containers)
            and all(c.ready for c in self.containers)
            and all(c.ready for c in self.init_containers if c.kind == "sidecar")
        )
        self.cond("PodReadyToStartContainers", "True" if self.ip else "False")
        self.cond("Initialized", "True" if init_done else "False")
        self.cond("ContainersReady", "True" if apps_ready else "False")
        self.cond("Ready", "True" if apps_ready and not self.stopping else "False")
        sched = next(
            (c for c in (self.obj.get("status") or {}).get("conditions") or [] if c["type"] == "PodScheduled"), None
        )
        conds = list(self.conditions.values()) + ([sched] if sched else [])
        status = {
            "phase": self.phase,
            "conditions": conds,
            "hostIP": self.agent.node_ip,
            "hostIPs": [{"ip": self.agent.node_ip}],
            "startTime": self.start_time,
            "containerStatuses": [c.status() for c in self.containers],
        }
        if self.ip:
            status["podIP"] = self.ip
            status["podIPs"] = [{"ip": self.ip}]
        if self.init_containers:
            status["initContainerStatuses"] = [c.status() for c in self.init_containers]
        return status

    async def status_loop(self):
        while True:
            await self.dirty.wait()
            self.dirty.clear()
            await asyncio.sleep(0.2)
            if self.agent.gone(self.uid):
                return
            try:
                await self.agent.patch_status(self)
            except ApiException as e:
                if e.status == 404:
                    return
                log.warning("status patch %s/%s: %s", self.namespace, self.name, e.reason)

    # -- sandbox
    async def setup_sandbox(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "volumes").mkdir(exist_ok=True)
        if self.host_network:
            self.ip = self.agent.node_ip
        else:
            await self._setup_network()
        d = self.agent.nsdir
        for kind in ("uts", "ipc"):
            Path(f"{d}/{self.uid}.{kind}").touch()
        await run("unshare", f"--uts={d}/{self.uid}.uts", f"--ipc={d}/{self.uid}.ipc", "true")
        hostname = (self.spec.get("hostname") or self.name)[:63]
        await run("nsenter", f"--uts={d}/{self.uid}.uts", "hostname", hostname)
        # Pod-wide files bind-mounted into every container.
        fqdn = (
            f"{hostname}.{self.spec['subdomain']}.{self.namespace}.svc.{self.agent.domain}"
            if self.spec.get("subdomain")
            else ""
        )
        hosts = [
            "127.0.0.1\tlocalhost",
            "::1\tlocalhost ip6-localhost ip6-loopback",
            f"{self.ip}\t{fqdn} {hostname}".replace("\t ", "\t"),
        ]
        for ha in self.spec.get("hostAliases") or []:
            hosts.append(f"{ha['ip']}\t{' '.join(ha.get('hostnames') or [])}")
        (self.dir / "hosts").write_text("# Kubernetes-managed hosts file (procnode).\n" + "\n".join(hosts) + "\n")
        (self.dir / "hostname").write_text(hostname + "\n")
        (self.dir / "resolv.conf").write_text(self._resolv_conf())
        self.shm = self.dir / "shm"
        self.shm.mkdir(exist_ok=True)
        await run("mount", "-t", "tmpfs", "-o", "size=65536k", "shm", str(self.shm))
        await self.agent.create_debug_objects(self)

    async def _setup_network(self):
        # The API rejects podIP changes, so a pod re-run after a procnode
        # restart keeps the address it already reported.
        self.ip = self.agent.ipam.allocate(self.uid, (self.obj.get("status") or {}).get("podIP"))
        n, v = self.netns, self.veth
        await run("ip", "netns", "add", n)
        await run("ip", "link", "add", v, "type", "veth", "peer", "name", "eth0", "netns", n)
        for args in (
            ["addr", "add", f"{self.ip}/32", "dev", "eth0"],
            ["link", "set", "lo", "up"],
            ["link", "set", "eth0", "up"],
            ["route", "add", "169.254.1.1", "dev", "eth0"],
            ["route", "add", "default", "via", "169.254.1.1", "dev", "eth0"],
        ):
            await run("ip", "-n", n, *args)
        await run("ip", "link", "set", v, "up")
        Path(f"/proc/sys/net/ipv4/conf/{v}/proxy_arp").write_text("1")
        await run("ip", "route", "replace", f"{self.ip}/32", "dev", v)
        # Containers run without capabilities once they drop root, so allow
        # low ports the way CAP_NET_BIND_SERVICE would; then apply pod sysctls.
        sysctls = {"net.ipv4.ip_unprivileged_port_start": "0"}
        for sc in (self.spec.get("securityContext") or {}).get("sysctls") or []:
            sysctls[sc["name"]] = sc["value"]
        for k, val in sysctls.items():
            await run("ip", "netns", "exec", n, "sysctl", "-q", "-w", f"{k}={val}", check=False)

    def _resolv_conf(self):
        policy = self.spec.get("dnsPolicy", "ClusterFirst")
        dns_cfg = self.spec.get("dnsConfig") or {}
        ns_, search, opts = [], [], []
        if policy in ("ClusterFirst", "ClusterFirstWithHostNet"):
            ns_ = [self.agent.dns_ip]
            dom = self.agent.domain
            search = [f"{self.namespace}.svc.{dom}", f"svc.{dom}", dom]
            opts = ["ndots:5"]
        elif policy == "Default":
            return Path("/etc/resolv.conf").read_text()
        ns_ += dns_cfg.get("nameservers") or []
        search += dns_cfg.get("searches") or []
        opts += [o["name"] + (f":{o['value']}" if o.get("value") else "") for o in dns_cfg.get("options") or []]
        out = [f"nameserver {n}" for n in ns_[:3]]
        if search:
            out.append("search " + " ".join(search))
        if opts:
            out.append("options " + " ".join(opts))
        return "\n".join(out) + "\n"

    async def teardown_sandbox(self):
        for c in self.init_containers + self.containers:
            await c.teardown_fs()
            c.cleanup_cgroups()
        await run("umount", "-R", "-l", str(self.dir / "volumes"), check=False)
        for m in sorted((self.dir / "volumes").glob("*"), reverse=True) if (self.dir / "volumes").exists() else []:
            await run("umount", "-l", str(m), check=False)
        await run("umount", "-l", str(self.dir / "shm"), check=False)
        d = self.agent.nsdir
        for kind in ("uts", "ipc"):
            await run("umount", f"{d}/{self.uid}.{kind}", check=False)
            Path(f"{d}/{self.uid}.{kind}").unlink(missing_ok=True)
        if not self.host_network:
            await run("ip", "netns", "del", self.netns, check=False)
            await run("ip", "route", "del", f"{self.ip}/32", check=False)
            self.agent.ipam.release(self.uid)
        shutil.rmtree(self.dir, ignore_errors=True)

    # -- env and volumes
    async def resolve_env(self, spec, icfg):
        env = {}
        for kv in icfg.get("Env") or []:
            k, _, v = kv.partition("=")
            env[k] = v
        env.setdefault("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
        env["HOSTNAME"] = (self.spec.get("hostname") or self.name)[:63]
        env.update(await self.agent.service_env(self.namespace, self.spec.get("enableServiceLinks", True)))
        for src in spec.get("envFrom") or []:
            prefix = src.get("prefix", "")
            if "configMapRef" in src:
                data = await self.agent.get_cm(
                    self.namespace, src["configMapRef"]["name"], src["configMapRef"].get("optional")
                )
            else:
                data = await self.agent.get_secret(
                    self.namespace, src["secretRef"]["name"], src["secretRef"].get("optional")
                )
            for k, v in (data or {}).items():
                env[prefix + k] = v
        for e in spec.get("env") or []:
            if "value" in e:
                env[e["name"]] = re.sub(
                    r"\$\(([A-Za-z_][A-Za-z0-9_]*)\)", lambda m: env.get(m.group(1), m.group(0)), e["value"] or ""
                )
                continue
            vf = e.get("valueFrom") or {}
            val = None
            if "configMapKeyRef" in vf:
                r = vf["configMapKeyRef"]
                data = await self.agent.get_cm(self.namespace, r["name"], r.get("optional"))
                val = (data or {}).get(r["key"])
                if val is None and not r.get("optional"):
                    raise PodError(
                        "CreateContainerConfigError",
                        f"couldn't find key {r['key']} in ConfigMap {self.namespace}/{r['name']}",
                    )
            elif "secretKeyRef" in vf:
                r = vf["secretKeyRef"]
                data = await self.agent.get_secret(self.namespace, r["name"], r.get("optional"))
                val = (data or {}).get(r["key"])
                if val is None and not r.get("optional"):
                    raise PodError(
                        "CreateContainerConfigError",
                        f"couldn't find key {r['key']} in Secret {self.namespace}/{r['name']}",
                    )
            elif "fieldRef" in vf:
                val = self.field(vf["fieldRef"]["fieldPath"])
            elif "resourceFieldRef" in vf:
                val = self.resource_field(spec, vf["resourceFieldRef"])
            if val is not None:
                env[e["name"]] = val
        return env

    def field(self, path):
        md = self.obj["metadata"]
        m = re.match(r"metadata\.(labels|annotations)\['(.+)'\]", path)
        if m:
            return (md.get(m.group(1)) or {}).get(m.group(2), "")
        return {
            "metadata.name": self.name,
            "metadata.namespace": self.namespace,
            "metadata.uid": self.uid,
            "spec.nodeName": self.node,
            "spec.serviceAccountName": self.spec.get("serviceAccountName", "default"),
            "status.hostIP": self.agent.node_ip,
            "status.hostIPs": self.agent.node_ip,
            "status.podIP": self.ip or "",
            "status.podIPs": self.ip or "",
        }.get(path, "")

    def resource_field(self, spec, ref):
        res = spec.get("resources") or {}
        kind, _, what = ref["resource"].partition(".")
        val = (res.get(kind) or {}).get(what)
        if what == "cpu":
            m = parse_cpu(val) or (self.agent.node_cpu * 1000)
            return str(max(1, -(-m // 1000)))
        return str(parse_mem(val) or self.agent.node_mem)

    async def mount_volume(self, vm, merged):
        vol = next((v for v in self.spec.get("volumes") or [] if v["name"] == vm["name"]), None)
        if vol is None:
            raise PodError("CreateContainerConfigError", f'volume "{vm["name"]}" not found')
        src = await self.volume_path(vol)
        if vm.get("subPath"):
            src = in_root(src, vm["subPath"])
            if not src.exists():
                src.mkdir(parents=True)
        target = in_root(merged, vm["mountPath"])
        if src.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.touch()
        await run("mount", "--bind", str(src), str(target))
        if vm.get("readOnly"):
            await run("mount", "-o", "remount,bind,ro", str(target))

    async def volume_path(self, vol):
        """Materialise a volume once per pod; returns its host path."""
        if vol["name"] in self.volumes:
            return self.volumes[vol["name"]]
        base = self.dir / "volumes" / vol["name"]
        mode = 0o644
        if "emptyDir" in vol:
            base.mkdir(parents=True, exist_ok=True)
            if (vol["emptyDir"] or {}).get("medium") == "Memory":
                size = parse_mem((vol["emptyDir"] or {}).get("sizeLimit")) or 64 * 2**20
                await run("mount", "-t", "tmpfs", "-o", f"size={size}", "tmpfs", str(base))
            os.chmod(base, 0o777)
        elif "configMap" in vol or "secret" in vol:
            is_cm = "configMap" in vol
            ref = vol["configMap"] if is_cm else vol["secret"]
            name = ref.get("name") or ref.get("secretName")
            data = await (self.agent.get_cm if is_cm else self.agent.get_secret)(self.namespace, name, True)
            if data is None and not ref.get("optional"):
                kind = "configmap" if is_cm else "secret"
                raise PodError(
                    "ContainerCreating",
                    f'MountVolume.SetUp failed for volume "{vol["name"]}" : {kind} "{name}" not found',
                )
            self._write_items(base, data or {}, ref.get("items"), ref.get("defaultMode", mode))
        elif "projected" in vol:
            base.mkdir(parents=True, exist_ok=True)
            for s in vol["projected"].get("sources") or []:
                if "serviceAccountToken" in s:
                    t = s["serviceAccountToken"]
                    tok = await self.agent.sa_token(self, t.get("audience"), t.get("expirationSeconds", 3607))
                    self._write_items(base, {t["path"]: tok}, None, 0o644)
                elif "configMap" in s or "secret" in s:
                    is_cm = "configMap" in s
                    ref = s["configMap"] if is_cm else s["secret"]
                    data = await (self.agent.get_cm if is_cm else self.agent.get_secret)(
                        self.namespace, ref["name"], True
                    )
                    self._write_items(base, data or {}, ref.get("items"), 0o644)
                elif "downwardAPI" in s:
                    self._write_items(
                        base,
                        {
                            i["path"]: self.field(i["fieldRef"]["fieldPath"])
                            for i in s["downwardAPI"].get("items") or []
                            if "fieldRef" in i
                        },
                        None,
                        0o644,
                    )
        elif "downwardAPI" in vol:
            items = vol["downwardAPI"].get("items") or []
            self._write_items(
                base, {i["path"]: self.field(i["fieldRef"]["fieldPath"]) for i in items if "fieldRef" in i}, None, 0o644
            )
        elif "persistentVolumeClaim" in vol:
            base = await self.agent.pvc_path(self.namespace, vol["persistentVolumeClaim"]["claimName"])
        elif "hostPath" in vol:
            base = Path(vol["hostPath"]["path"])
            if vol["hostPath"].get("type") in ("DirectoryOrCreate", None, "") and not base.exists():
                base.mkdir(parents=True, exist_ok=True)
        else:
            base.mkdir(parents=True, exist_ok=True)  # unsupported volume type: empty dir
            log.warning("pod %s/%s: unsupported volume %s", self.namespace, self.name, list(vol))
        fs_group = (self.spec.get("securityContext") or {}).get("fsGroup")
        if fs_group is not None and ("emptyDir" in vol or "persistentVolumeClaim" in vol):
            await run("chgrp", "-R", str(fs_group), str(base), check=False)
            await run("chmod", "-R", "g+rwX", str(base), check=False)
        if "persistentVolumeClaim" not in vol:
            self.volumes[vol["name"]] = base
        return base

    @staticmethod
    def _write_items(base, data, items, mode):
        base.mkdir(parents=True, exist_ok=True)
        pairs = [(i["key"], i["path"], i.get("mode", mode)) for i in items] if items else [(k, k, mode) for k in data]
        for key, path, m in pairs:
            if key not in data:
                continue
            f = base / path
            f.parent.mkdir(parents=True, exist_ok=True)
            v = data[key]
            f.write_bytes(v if isinstance(v, bytes) else str(v).encode())
            os.chmod(f, m)

    # -- lifecycle
    async def run(self):
        for c in self.spec.get("initContainers") or []:
            kind = "sidecar" if c.get("restartPolicy") == "Always" else "init"
            self.init_containers.append(Container(self, c, kind))
        self.containers = [Container(self, c, "app") for c in self.spec["containers"]]
        # After a procnode restart (a "node reboot") keep counting restarts.
        prior = {
            s["name"]: s.get("restartCount", 0)
            for key in ("containerStatuses", "initContainerStatuses")
            for s in (self.obj.get("status") or {}).get(key) or []
        }
        for c in self.init_containers + self.containers:
            c.restart_count = prior.get(c.name, 0)
        asyncio.create_task(self.status_loop())
        self.mark_dirty()
        if self.virtual:
            self.ip = self.agent.node_ip
            for c in self.init_containers + self.containers:
                c.state = {"running": {"startedAt": now()}}
                c.started = c.ready = True
            self.phase = "Running"
            self.mark_dirty()
            return
        try:
            await self.setup_sandbox()
        except Exception as e:  # noqa: BLE001
            log.exception("sandbox %s/%s", self.namespace, self.name)
            self.event("Warning", "FailedCreatePodSandBox", f"Failed to create pod sandbox: {e}")
            return
        self.mark_dirty()
        restart = self.spec.get("restartPolicy", "Always")
        for c in self.init_containers:
            if c.kind == "sidecar":
                asyncio.create_task(self.keep_running(c, "Always"))
                while not c.started and not self.stopping:
                    await asyncio.sleep(0.2)
                continue
            backoff = 10
            while not self.stopping:
                await self.keep_running(c, "OnFailure" if restart == "Always" else restart, once=True)
                term = c.state.get("terminated") or {}
                if term.get("exitCode") == 0:
                    break
                if restart == "Never":
                    self.phase = "Failed"
                    self.mark_dirty()
                    return
                c.last_state = {"terminated": term}
                c.restart_count += 1
                c.state = {
                    "waiting": {
                        "reason": "CrashLoopBackOff",
                        "message": f"back-off {backoff}s restarting failed container={c.name} pod={self.name}_{self.namespace}({self.uid})",
                    }
                }
                self.event("Warning", "BackOff", f"Back-off restarting failed container {c.name}", c)
                self.mark_dirty()
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 300)
            if self.stopping:
                return
        self.mark_dirty()
        tasks = [asyncio.create_task(self.keep_running(c, restart)) for c in self.containers]
        self.phase = "Running"
        self.mark_dirty()
        await asyncio.gather(*tasks)
        if not self.stopping:
            codes = [(c.state.get("terminated") or {}).get("exitCode", 1) for c in self.containers]
            self.phase = "Succeeded" if all(x == 0 for x in codes) else "Failed"
            self.terminal = True
            for c in self.init_containers:
                if c.kind == "sidecar":
                    await c.kill(2)
            self.mark_dirty()

    async def keep_running(self, c, policy, once=False):
        """Run a container with kubelet restart/backoff semantics."""
        backoff = 10
        while not self.stopping:
            try:
                await c.prepare_and_start(self.agent.images)
            except PodError as e:
                c.state = {"waiting": {"reason": e.reason, "message": e.message}}
                self.mark_dirty()
                etype = "Warning"
                if e.reason == "ErrImagePull":
                    self.event(etype, "Failed", e.message, c)
                    await asyncio.sleep(2)
                    c.state = {
                        "waiting": {
                            "reason": "ImagePullBackOff",
                            "message": f'Back-off pulling image "{c.spec["image"]}"',
                        }
                    }
                    self.event("Normal", "BackOff", f'Back-off pulling image "{c.spec["image"]}"', c)
                elif e.reason == "ContainerCreating":
                    self.event(etype, "FailedMount", e.message)
                else:
                    self.event(etype, "Failed", f"Error: {e.message}", c)
                self.mark_dirty()
                await asyncio.sleep(min(backoff, 60))
                backoff = min(backoff * 2, 300)
                continue
            except Exception as e:  # noqa: BLE001
                log.exception("start %s/%s/%s", self.namespace, self.name, c.name)
                c.state = {"waiting": {"reason": "RunContainerError", "message": str(e)[:300]}}
                self.mark_dirty()
                await asyncio.sleep(min(backoff, 60))
                backoff = min(backoff * 2, 300)
                continue
            probes = asyncio.create_task(self.run_probes(c))
            ran_from = time.time()
            await c.exited.wait()
            probes.cancel()
            if self.stopping or self.terminal:
                return
            term = c.state["terminated"]
            if once or policy == "Never" or (policy == "OnFailure" and term["exitCode"] == 0):
                return
            c.last_state = {"terminated": term}
            c.restart_count += 1
            if time.time() - ran_from > 600:
                backoff = 10
            c.state = {
                "waiting": {
                    "reason": "CrashLoopBackOff",
                    "message": f"back-off {backoff}s restarting failed container={c.name} pod={self.name}_{self.namespace}({self.uid})",
                }
            }
            self.event(
                "Warning",
                "BackOff",
                "Back-off restarting failed container " + c.name + f" in pod {self.name}_{self.namespace}({self.uid})",
                c,
            )
            self.mark_dirty()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 300)

    async def run_probes(self, c):
        spec = c.spec
        sp, lp, rp = spec.get("startupProbe"), spec.get("livenessProbe"), spec.get("readinessProbe")
        t0 = time.time()
        if sp:
            ok = await self._probe_until(c, sp, "Startup", t0)
            if not ok:
                return
            c.startup_ok = c.started = True
            self.mark_dirty()
        else:
            c.started = True
        if not rp:
            c.ready = True
            self.mark_dirty()
        tasks = []
        if rp:
            tasks.append(asyncio.create_task(self._readiness(c, rp, t0)))
        if lp:
            tasks.append(asyncio.create_task(self._liveness(c, lp, t0)))
        try:
            await asyncio.gather(*tasks)
        finally:
            for t in tasks:
                t.cancel()

    async def _probe_until(self, c, probe, kind, t0):
        await asyncio.sleep(max(0, probe.get("initialDelaySeconds", 0) - (time.time() - t0)))
        fails = 0
        while True:
            ok, msg = await c.probe(probe)
            if ok:
                return True
            fails += 1
            self.event("Warning", "Unhealthy", f"{kind} probe failed: {msg}", c)
            if fails >= probe.get("failureThreshold", 3):
                self.event("Normal", "Killing", f"Container {c.name} failed {kind.lower()} probe, will be restarted", c)
                await c.kill(self.grace())
                return False
            await asyncio.sleep(probe.get("periodSeconds", 10))

    async def _readiness(self, c, probe, t0):
        await asyncio.sleep(max(0, probe.get("initialDelaySeconds", 0) - (time.time() - t0)))
        succ = fails = 0
        while True:
            ok, msg = await c.probe(probe)
            if ok:
                succ, fails = succ + 1, 0
                if not c.ready and succ >= probe.get("successThreshold", 1):
                    c.ready = True
                    self.mark_dirty()
            else:
                succ, fails = 0, fails + 1
                self.event("Warning", "Unhealthy", f"Readiness probe failed: {msg}", c)
                if c.ready and fails >= probe.get("failureThreshold", 3):
                    c.ready = False
                    self.mark_dirty()
            await asyncio.sleep(probe.get("periodSeconds", 10))

    async def _liveness(self, c, probe, t0):
        await asyncio.sleep(max(0, probe.get("initialDelaySeconds", 0) - (time.time() - t0)))
        fails = 0
        while True:
            ok, msg = await c.probe(probe)
            if ok:
                fails = 0
            else:
                fails += 1
                self.event("Warning", "Unhealthy", f"Liveness probe failed: {msg}", c)
                if fails >= probe.get("failureThreshold", 3):
                    self.event("Normal", "Killing", f"Container {c.name} failed liveness probe, will be restarted", c)
                    await c.kill(self.grace())
                    return
            await asyncio.sleep(probe.get("periodSeconds", 10))

    def grace(self):
        md = self.obj["metadata"]
        g = md.get("deletionGracePeriodSeconds")
        return g if g is not None else self.spec.get("terminationGracePeriodSeconds", 30)

    async def stop(self):
        if self.stopping:
            return
        self.stopping = True
        grace = self.grace()
        for c in self.containers:
            if c.proc is not None and c.proc.returncode is None:
                self.event("Normal", "Killing", f"Stopping container {c.name}", c)
        await asyncio.gather(*(c.kill(grace) for c in self.containers))
        await asyncio.gather(*(c.kill(2) for c in self.init_containers))
        await self.teardown_sandbox()


# --------------------------------------------------------------------------- agent


class IPAM:
    def __init__(self, cidr):
        self.net = ipaddress.ip_network(cidr)
        self.used = {}
        self.next = 2

    def allocate(self, key, preferred=None):
        if key in self.used:
            return self.used[key]
        taken = set(self.used.values())
        if preferred and ipaddress.ip_address(preferred) in self.net and preferred not in taken:
            self.used[key] = preferred
            return preferred
        hosts = self.net.num_addresses - 2
        for _ in range(hosts):
            ip = str(self.net.network_address + self.next)
            self.next = self.next % hosts + 2
            if ip not in taken:
                self.used[key] = ip
                return ip
        raise RuntimeError("pod CIDR exhausted")

    def release(self, key):
        self.used.pop(key, None)


class Agent:
    def __init__(self, args):
        if args.kubeconfig:
            config.load_kube_config(args.kubeconfig)
        else:
            config.load_incluster_config()
        self.core = client.CoreV1Api()
        self.custom = client.CustomObjectsApi()
        self.root = Path(args.root)
        self.logroot = Path(args.log_root)
        self.nsdir = args.ns_dir
        self.dns_ip, self.domain = args.dns_ip, args.cluster_domain
        self.node_ip = args.node_ip
        self.primary_node = args.primary_node
        self.serializer = client.ApiClient()
        self.node_cpu, self.node_mem = 32, 256 * 2**30
        self.ipam = IPAM(args.pod_cidr)
        self.images = ImageStore(self.root)
        self.pods = {}
        self.nodes = set()
        self.deleted = set()
        self.exec_helper = self.root / "bin" / "pn-exec"
        self.loop = None
        self.cache = {}
        # NetworkPolicy inputs: every pod with an IP (any node), policies, namespace labels.
        self.np_pods, self.policies, self.ns_labels = {}, {}, {}
        self.np_dirty = asyncio.Event()

    # -- setup
    async def init_host(self):
        for d in (self.root / "bin", self.root / "pods", self.root / "pv", self.logroot, Path(self.nsdir)):
            d.mkdir(parents=True, exist_ok=True)
        p = await asyncio.create_subprocess_exec("mountpoint", "-q", self.nsdir)
        if await p.wait() != 0:
            await run("mount", "--bind", self.nsdir, self.nsdir)
            await run("mount", "--make-private", self.nsdir)
        Path("/proc/sys/net/ipv4/ip_forward").write_text("1")
        for c in ("memory", "cpu", "cpuacct"):
            Path(f"/sys/fs/cgroup/{c}/procnode").mkdir(exist_ok=True)
        await self.reap_stale()
        self.exec_helper.write_text(EXEC_HELPER.format(root=self.root))
        self.exec_helper.chmod(0o755)

    async def reap_stale(self):
        """Like a node reboot: drop every sandbox a previous procnode left behind."""
        for cg in Path("/sys/fs/cgroup/memory/procnode").iterdir():
            if cg.is_dir():
                for pid in (cg / "cgroup.procs").read_text().split():
                    with contextlib.suppress(ProcessLookupError):
                        os.kill(int(pid), signal.SIGKILL)
        await asyncio.sleep(0.5)
        mounts = [line.split()[1] for line in Path("/proc/mounts").read_text().splitlines()]
        for m in sorted((m for m in mounts if m.startswith(str(self.root / "pods"))), reverse=True):
            await run("umount", "-l", m, check=False)
        for m in mounts:
            if m.startswith(self.nsdir + "/"):
                await run("umount", "-l", m, check=False)
        for line in (await run("ip", "netns", "list", check=False)).splitlines():
            if line.startswith("pn-"):
                await run("ip", "netns", "del", line.split()[0], check=False)
        for c in ("memory", "cpu", "cpuacct"):
            for d in Path(f"/sys/fs/cgroup/{c}/procnode").iterdir():
                if d.is_dir():
                    with contextlib.suppress(OSError):
                        d.rmdir()
        shutil.rmtree(self.root / "pods", ignore_errors=True)
        (self.root / "pods").mkdir()
        for f in Path(self.nsdir).iterdir():
            f.unlink(missing_ok=True)

    # -- kubernetes plumbing
    def watch_thread(self, fn, handler, **kw):
        def loop():
            while True:
                try:
                    for ev in watch.Watch().stream(fn, timeout_seconds=300, **kw):
                        obj = self.serializer.sanitize_for_serialization(ev["object"])
                        self.loop.call_soon_threadsafe(handler, ev["type"], obj)
                except Exception as e:  # noqa: BLE001
                    log.warning("watch %s: %s", fn.__name__, e)
                    time.sleep(1)

        threading.Thread(target=loop, daemon=True).start()

    async def api(self, fn, *a, **kw):
        return await asyncio.to_thread(fn, *a, **kw)

    def on_node(self, etype, obj):
        name = obj["metadata"]["name"]
        if etype == "DELETED" or (obj["metadata"].get("labels") or {}).get(MANAGED_LABEL) != "true":
            self.nodes.discard(name)
        else:
            self.nodes.add(name)

    def on_pod(self, etype, obj):
        md, spec = obj["metadata"], obj["spec"]
        uid = md["uid"]
        ip = (obj.get("status") or {}).get("podIP")
        if etype == "DELETED" or not ip or spec.get("hostNetwork"):
            if self.np_pods.pop(uid, None):
                self.np_dirty.set()
        else:
            entry = {
                "namespace": md["namespace"],
                "labels": md.get("labels") or {},
                "ip": ip,
                "containers": spec["containers"],
            }
            if self.np_pods.get(uid) != entry:
                self.np_pods[uid] = entry
                self.np_dirty.set()
        if spec.get("nodeName") not in self.nodes:
            return
        pod = self.pods.get(uid)
        if etype == "DELETED":
            self.deleted.add(uid)
            if pod:
                asyncio.create_task(self._finish(pod, delete=False))
            return
        if pod is None:
            if (obj.get("status") or {}).get("phase") in ("Succeeded", "Failed") or uid in self.deleted:
                return
            if md.get("deletionTimestamp"):
                asyncio.create_task(self._force_delete(md))
                return
            pod = Pod(self, obj)
            self.pods[uid] = pod
            pod.task = asyncio.create_task(pod.run())
            return
        pod.obj = obj
        if md.get("deletionTimestamp") and not pod.stopping:
            asyncio.create_task(self._finish(pod, delete=True))

    async def _finish(self, pod, delete):
        await pod.stop()
        if pod.task:
            pod.task.cancel()
        self.pods.pop(pod.uid, None)
        self.deleted.add(pod.uid)
        if delete:
            await self._force_delete(pod.obj["metadata"])

    async def _force_delete(self, md):
        try:
            await self.api(
                self.core.delete_namespaced_pod,
                md["name"],
                md["namespace"],
                body=client.V1DeleteOptions(
                    grace_period_seconds=0, preconditions=client.V1Preconditions(uid=md["uid"])
                ),
            )
        except ApiException as e:
            if e.status not in (404, 409):
                log.warning("delete %s/%s: %s", md["namespace"], md["name"], e.reason)

    def on_netpol(self, etype, obj):
        key = (obj["metadata"]["namespace"], obj["metadata"]["name"])
        if etype == "DELETED":
            self.policies.pop(key, None)
        else:
            self.policies[key] = obj
        self.np_dirty.set()

    def on_namespace(self, etype, obj):
        name = obj["metadata"]["name"]
        labels = obj["metadata"].get("labels") or {}
        if etype == "DELETED":
            self.ns_labels.pop(name, None)
        elif self.ns_labels.get(name) == labels:
            return
        else:
            self.ns_labels[name] = labels
        self.np_dirty.set()

    async def netpol_loop(self):
        enforcer = await asyncio.to_thread(Enforcer)
        while True:
            await self.np_dirty.wait()
            await asyncio.sleep(0.3)  # coalesce bursts
            self.np_dirty.clear()
            try:
                n = await asyncio.to_thread(
                    enforcer.apply, list(self.policies.values()), dict(self.np_pods), dict(self.ns_labels)
                )
                log.debug("networkpolicy: %d isolated pod chains", n)
            except Exception:  # noqa: BLE001
                log.exception("networkpolicy sync")

    def gone(self, uid):
        return uid in self.deleted

    async def patch_status(self, pod):
        # A dict body is sent as a strategic merge patch; containerStatuses has
        # no merge key, so each list is replaced wholesale.
        body = {"status": pod.build_status()}
        await self.api(self.core.patch_namespaced_pod_status, pod.name, pod.namespace, body)

    def emit_event(self, pod, etype, reason, message, container=None):
        key = (pod.uid, reason, message)
        ts = dt.datetime.now(dt.UTC)
        prev = pod.events.get(key)
        ref = {"kind": "Pod", "namespace": pod.namespace, "name": pod.name, "uid": pod.uid, "apiVersion": "v1"}
        if container is not None:
            group = "initContainers" if container.kind != "app" else "containers"
            ref["fieldPath"] = f"spec.{group}{{{container.name}}}"

        async def send():
            try:
                if prev:
                    prev["count"] += 1
                    await self.api(
                        self.core.patch_namespaced_event,
                        prev["name"],
                        pod.namespace,
                        {"count": prev["count"], "lastTimestamp": ts.strftime("%Y-%m-%dT%H:%M:%SZ")},
                    )
                else:
                    name = f"{pod.name}.{time.time_ns():x}"
                    ev = client.CoreV1Event(
                        metadata=client.V1ObjectMeta(name=name, namespace=pod.namespace),
                        involved_object=ref,
                        reason=reason,
                        message=message,
                        type=etype,
                        count=1,
                        first_timestamp=ts,
                        last_timestamp=ts,
                        source=client.V1EventSource(component="kubelet", host=pod.node),
                        reporting_component="kubelet",
                        reporting_instance=pod.node,
                    )
                    pod.events[key] = {"name": name, "count": 1}
                    await self.api(self.core.create_namespaced_event, pod.namespace, ev)
            except ApiException as e:
                log.debug("event: %s", e.reason)

        asyncio.create_task(send())

    async def get_cm(self, ns, name, optional):
        try:
            cm = await self.api(self.core.read_namespaced_config_map, name, ns)
        except ApiException as e:
            if e.status == 404:
                if optional:
                    return None
                raise PodError("CreateContainerConfigError", f'configmap "{name}" not found') from e
            raise
        data = dict(cm.data or {})
        data.update({k: base64.b64decode(v) for k, v in (cm.binary_data or {}).items()})
        return data

    async def get_secret(self, ns, name, optional):
        try:
            s = await self.api(self.core.read_namespaced_secret, name, ns)
        except ApiException as e:
            if e.status == 404:
                if optional:
                    return None
                raise PodError("CreateContainerConfigError", f'secret "{name}" not found') from e
            raise
        return {k: base64.b64decode(v) for k, v in (s.data or {}).items()}

    async def sa_token(self, pod, audience, expiry):
        sa = pod.spec.get("serviceAccountName") or "default"
        body = client.AuthenticationV1TokenRequest(
            spec=client.V1TokenRequestSpec(
                audiences=[audience] if audience else [],
                expiration_seconds=max(600, expiry),
                bound_object_ref=client.V1BoundObjectReference(
                    kind="Pod", api_version="v1", name=pod.name, uid=pod.uid
                ),
            )
        )
        r = await self.api(self.core.create_namespaced_service_account_token, sa, pod.namespace, body)
        return r.status.token

    async def service_env(self, ns, links):
        """KUBERNETES_* plus docker-link style variables for services (enableServiceLinks)."""
        env = {}
        svcs = (await self.api(self.core.list_namespaced_service, ns)).items if links else []
        kube = await self.api(self.core.read_namespaced_service, "kubernetes", "default")
        for s in [kube] + [
            s for s in svcs if not (s.metadata.namespace == "default" and s.metadata.name == "kubernetes")
        ]:
            ip = s.spec.cluster_ip
            if not ip or ip == "None" or not s.spec.ports:
                continue
            n = s.metadata.name.upper().replace("-", "_")
            p0 = s.spec.ports[0]
            env[f"{n}_SERVICE_HOST"] = ip
            env[f"{n}_SERVICE_PORT"] = str(p0.port)
            proto = (p0.protocol or "TCP").lower()
            env[f"{n}_PORT"] = f"{proto}://{ip}:{p0.port}"
            for p in s.spec.ports:
                pr = p.protocol or "TCP"
                if p.name:
                    env[f"{n}_SERVICE_PORT_{p.name.upper().replace('-', '_')}"] = str(p.port)
                pre = f"{n}_PORT_{p.port}_{pr}"
                env[pre] = f"{pr.lower()}://{ip}:{p.port}"
                env[pre + "_PROTO"] = pr.lower()
                env[pre + "_PORT"] = str(p.port)
                env[pre + "_ADDR"] = ip
        return env

    # -- storage
    async def pvc_path(self, ns, claim):
        try:
            pvc = await self.api(self.core.read_namespaced_persistent_volume_claim, claim, ns)
        except ApiException as e:
            raise PodError("ContainerCreating", f'persistentvolumeclaim "{claim}" not found') from e
        if not pvc.spec.volume_name:
            raise PodError("ContainerCreating", f'persistentvolumeclaim "{claim}" is not bound')
        pv = await self.api(self.core.read_persistent_volume, pvc.spec.volume_name)
        path = Path(
            pv.spec.host_path.path
            if pv.spec.host_path
            else (pv.spec.local.path if pv.spec.local else self.root / "pv" / pvc.spec.volume_name)
        )
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, 0o777)
        return path

    def on_pvc(self, etype, obj):
        if etype == "DELETED" or (obj.get("status") or {}).get("phase") != "Pending" or obj["spec"].get("volumeName"):
            return
        asyncio.create_task(self.provision(obj))

    async def provision(self, pvc):
        md, spec = pvc["metadata"], pvc["spec"]
        sc = spec.get("storageClassName")
        if sc is None:
            sc = self.default_sc
        if sc is None:
            return
        try:
            cls = await self.api(client.StorageV1Api().read_storage_class, sc)
        except ApiException:
            return
        if cls.provisioner != PROVISIONER:
            return
        name = f"pvc-{md['uid']}"
        path = self.root / "pv" / md["namespace"] / md["name"]
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, 0o777)
        pv = client.V1PersistentVolume(
            metadata=client.V1ObjectMeta(name=name, annotations={"pv.kubernetes.io/provisioned-by": PROVISIONER}),
            spec=client.V1PersistentVolumeSpec(
                capacity={"storage": spec["resources"]["requests"]["storage"]},
                access_modes=spec.get("accessModes") or ["ReadWriteOnce"],
                persistent_volume_reclaim_policy="Delete",
                storage_class_name=sc,
                host_path=client.V1HostPathVolumeSource(path=str(path), type="DirectoryOrCreate"),
                claim_ref=client.V1ObjectReference(
                    kind="PersistentVolumeClaim",
                    namespace=md["namespace"],
                    name=md["name"],
                    uid=md["uid"],
                    api_version="v1",
                ),
            ),
        )
        try:
            await self.api(self.core.create_persistent_volume, pv)
        except ApiException as e:
            if e.status != 409:
                log.warning("provision %s: %s", name, e.reason)

    def on_pv(self, etype, obj):
        st = (obj.get("status") or {}).get("phase")
        ann = obj["metadata"].get("annotations") or {}
        if ann.get("pv.kubernetes.io/provisioned-by") == PROVISIONER and st == "Released":

            async def reclaim():
                hp = (obj["spec"].get("hostPath") or {}).get("path")
                if hp and hp.startswith(str(self.root / "pv")):
                    shutil.rmtree(hp, ignore_errors=True)
                with contextlib.suppress(ApiException):
                    await self.api(self.core.delete_persistent_volume, obj["metadata"]["name"])

            asyncio.create_task(reclaim())

    # -- kwok debugging objects (logs / exec / port-forward)
    async def create_debug_objects(self, pod):
        conts = (pod.spec.get("initContainers") or []) + pod.spec["containers"]
        owner = [{"apiVersion": "v1", "kind": "Pod", "name": pod.name, "uid": pod.uid}]
        md = {"name": pod.name, "namespace": pod.namespace, "ownerReferences": owner}
        logs = {
            "apiVersion": f"{KWOK_GROUP}/{KWOK_VERSION}",
            "kind": "Logs",
            "metadata": md,
            "spec": {
                "logs": [
                    {
                        "containers": [c["name"]],
                        "logsFile": str(pod.logdir / c["name"] / "current"),
                        "previousLogsFile": str(pod.logdir / c["name"] / "previous"),
                        "follow": True,
                    }
                    for c in conts
                ]
            },
        }
        execs = {
            "apiVersion": f"{KWOK_GROUP}/{KWOK_VERSION}",
            "kind": "Exec",
            "metadata": md,
            "spec": {
                "execs": [
                    {
                        "containers": [c["name"]],
                        "local": {
                            "envs": [{"name": "KWOK_EXEC_PREFIX", "value": f"{self.exec_helper} {pod.uid} {c['name']}"}]
                        },
                    }
                    for c in conts
                ]
            },
        }
        # Port 0 means "the requested port" (needs the kwok port-forward patch).
        pf = {
            "apiVersion": f"{KWOK_GROUP}/{KWOK_VERSION}",
            "kind": "PortForward",
            "metadata": md,
            "spec": {"forwards": [{"target": {"address": pod.ip, "port": 0}}]},
        }
        for obj, plural in ((logs, "logs"), (execs, "execs"), (pf, "portforwards")):
            try:
                await self.api(
                    self.custom.create_namespaced_custom_object, KWOK_GROUP, KWOK_VERSION, pod.namespace, plural, obj
                )
            except ApiException as e:
                if e.status == 409:
                    await self.api(
                        self.custom.delete_namespaced_custom_object,
                        KWOK_GROUP,
                        KWOK_VERSION,
                        pod.namespace,
                        plural,
                        pod.name,
                    )
                    await self.api(
                        self.custom.create_namespaced_custom_object,
                        KWOK_GROUP,
                        KWOK_VERSION,
                        pod.namespace,
                        plural,
                        obj,
                    )
                else:
                    log.warning("kwok %s for %s/%s: %s", plural, pod.namespace, pod.name, e.reason)

    async def main(self):
        self.loop = asyncio.get_running_loop()
        await self.init_host()
        self.default_sc = None
        for sc in (await self.api(client.StorageV1Api().list_storage_class)).items:
            if (sc.metadata.annotations or {}).get("storageclass.kubernetes.io/is-default-class") == "true":
                self.default_sc = sc.metadata.name
        for n in (await self.api(self.core.list_node)).items:
            self.on_node("ADDED", client.ApiClient().sanitize_for_serialization(n))
        log.info("managing nodes: %s", sorted(self.nodes))
        self.watch_thread(self.core.list_node, self.on_node)
        self.watch_thread(self.core.list_pod_for_all_namespaces, self.on_pod)
        self.watch_thread(self.core.list_persistent_volume_claim_for_all_namespaces, self.on_pvc)
        self.watch_thread(self.core.list_persistent_volume, self.on_pv)
        self.watch_thread(client.NetworkingV1Api().list_network_policy_for_all_namespaces, self.on_netpol)
        self.watch_thread(self.core.list_namespace, self.on_namespace)
        asyncio.create_task(self.netpol_loop())
        await asyncio.Event().wait()


EXEC_HELPER = """#!/usr/bin/env python3
# kwok-controller runs: pn-exec <pod-uid> <container> <cmd...>
import json, os, sys
base = "{root}/pods/" + sys.argv[1] + "/containers/" + sys.argv[2]
try:
    pid = open(base + "/pid").read().strip()
    env = json.load(open(base + "/env.json"))
    uid, gid = json.load(open(base + "/user.json"))
except OSError:
    sys.stderr.write("container not running\\n"); sys.exit(126)
args = ["nsenter", "-t", pid, "-m", "-u", "-i", "-n", "-p", "-r", "-w"]
if uid:
    args += ["-S", str(uid), "-G", str(gid)]
os.execvpe("nsenter", args + ["--"] + sys.argv[3:], env)
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--kubeconfig")
    ap.add_argument("--root", default="/var/lib/procnode")
    ap.add_argument("--log-root", default="/var/log/pods")
    ap.add_argument("--ns-dir", default="/run/procnode/ns")
    ap.add_argument("--pod-cidr", default="10.88.0.0/16")
    ap.add_argument("--dns-ip", default="10.96.0.10")
    ap.add_argument("--cluster-domain", default="cluster.local")
    ap.add_argument("--node-ip", required=True, help="host IP reported as status.hostIP")
    ap.add_argument("--primary-node", required=True, help="node whose hostNetwork pods really run")
    ap.add_argument("-v", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.v else logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("kubernetes").setLevel(logging.WARNING)
    asyncio.run(Agent(a).main())


if __name__ == "__main__":
    main()
