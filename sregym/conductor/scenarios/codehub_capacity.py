"""Private native allocation and kernel-resource observations for one owned run.

This is a monitored reserve with bounded cancellation, not a filesystem quota.
Nothing is read from mutable tools or processes inside a workload node.
"""

import json
import os
import re
import subprocess
import threading
import time
from contextlib import closing
from dataclasses import asdict
from pathlib import Path

from kubernetes.utils.quantity import parse_quantity

import docker
from sregym.conductor.scenarios.codehub_contracts import StorageFootprint
from sregym.conductor.scenarios.database_recovery import TIERS, HostCapacity

GIB = 1024**3

# The admin-side helper reads metadata only. Open directory components by FD,
# refusing symlinks/mount changes; never follow workload-created names outside
# captured roots. Retry a complete sample after ordinary subtree deletion;
# an incomplete sample is never published as a zero-sized observation.
ALLOCATION_PROGRAM = r"""
import json,os,stat,sys,time
roots=json.load(sys.stdin)
assert type(roots) is dict and 0<len(roots)<=256
deadline=time.monotonic()+30
entries=0
flags=os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW
def open_root(path):
 assert path.startswith('/') and '..' not in path.split('/')
 fd=os.open('/',flags)
 try:
  for component in filter(None,path.split('/')):
   child=os.open(component,flags,dir_fd=fd)
   os.close(fd);fd=child
  return fd
 except BaseException:
  os.close(fd);raise
def walk(fd,device,depth,seen):
 global entries
 if depth>64 or time.monotonic()>=deadline: raise RuntimeError('Allocation observation deadline/depth exceeded')
 info=os.fstat(fd)
 if info.st_dev!=device: raise RuntimeError('Allocation root crossed a filesystem')
 allocated,logical,logs,temporary=info.st_blocks*512,info.st_size,0,0
 with os.scandir(fd) as children:
  for entry in children:
   name=entry.name
   entries+=1
   if entries>2000000 or time.monotonic()>=deadline: raise RuntimeError('Allocation observation entry/deadline exceeded')
   info=entry.stat(follow_symlinks=False)
   if stat.S_ISLNK(info.st_mode):
    allocated+=info.st_blocks*512;logical+=info.st_size
    continue
   if info.st_dev!=device: raise RuntimeError('Allocation observation crossed a mount')
   identity=(info.st_dev,info.st_ino)
   if identity in seen: continue
   seen.add(identity)
   if stat.S_ISDIR(info.st_mode):
    child=os.open(name,flags,dir_fd=fd)
    try:
     current=os.fstat(child)
     if (current.st_dev,current.st_ino)!=identity: raise RuntimeError('Allocation directory changed')
     values=walk(child,device,depth+1,seen)
    finally: os.close(child)
    allocated+=values[0];logical+=values[1];logs+=values[2];temporary+=values[3]
   elif stat.S_ISREG(info.st_mode):
    allocated+=info.st_blocks*512;logical+=info.st_size
    if name.endswith('.log') or name.startswith(('mysql-bin.','binlog.')): logs+=info.st_blocks*512
    if name.startswith('recovered.'): temporary+=info.st_blocks*512
 return allocated,logical,logs,temporary
captured={}
try:
 for label,path in roots.items():
  fd=open_root(path)
  info=os.fstat(fd)
  captured[label]=(fd,info.st_dev,info.st_ino)
 for attempt in range(8):
  result={}
  try:
   for label,(fd,device,inode) in captured.items():
    values=(0,0,0,0) if label.startswith('filesystem:') else walk(fd,device,0,{(device,inode)})
    filesystem=os.fstatvfs(fd)
    result[label]={'allocated':values[0],'logical':values[1],'logs':values[2],'temporary':values[3],
                   'free_inodes':filesystem.f_favail,'free_bytes':filesystem.f_bavail*filesystem.f_frsize,
                   'device':device,'inode':inode}
  except FileNotFoundError:
   if attempt==7 or time.monotonic()>=deadline: raise
   time.sleep(min(.025, max(0,deadline-time.monotonic())))
   continue
  # Revalidate the original native root paths, including metadata-only roots.
  # Root replacement, a new symlink or a mount change is not subtree churn.
  for label,path in roots.items():
   current=open_root(path)
   try:
    info=os.fstat(current)
    if (info.st_dev,info.st_ino)!=captured[label][1:]: raise RuntimeError('Captured allocation root changed')
   finally: os.close(current)
  break
finally:
 for fd,device,inode in captured.values(): os.close(fd)
print(json.dumps(result,separators=(',',':')))
"""


KERNEL_PROGRAM = r"""
import json,os,re,sys
nodes=json.load(sys.stdin)
assert type(nodes) is dict and 0<len(nodes)<=64
result={}
for identity,pid in nodes.items():
 assert re.fullmatch('[a-f0-9]{64}',identity) and type(pid) is int and pid>0
 process='/proc/'+str(pid)
 uid=os.stat(process).st_uid
 assert uid!=0, 'Owned workload process must remain non-root on the host'
 before=open(process+'/cgroup').read()
 assert len(before)<8192 and before.startswith('0::/') and before.count('\n')==1
 parts=before.strip()[3:].split('/')
 expected='docker-'+identity+'.scope'
 assert expected in parts and 'user-'+str(uid)+'.slice' in parts
 selected=parts[:parts.index(expected)+1]
 assert all(part not in ('.','..') for part in selected)
 fd=os.open('/sys/fs/cgroup',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
 try:
  ancestors=[]
  parent=[]
  for part in filter(None,selected):
   if parent:
    limit_fd=os.open('memory.max',os.O_RDONLY|os.O_NOFOLLOW,dir_fd=fd)
    try: ancestor_limit=os.read(limit_fd,8193).decode().strip()
    finally: os.close(limit_fd)
    assert ancestor_limit=='max' or (ancestor_limit.isdigit() and int(ancestor_limit)>0)
    ancestor_info=os.fstat(fd)
    ancestors.append({'path':'/'+ '/'.join(parent),'device':ancestor_info.st_dev,'inode':ancestor_info.st_ino,
                      'memory_limit':None if ancestor_limit=='max' else int(ancestor_limit)})
   child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
   os.close(fd);fd=child;parent.append(part)
  def read(name):
   file=os.open(name,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=fd)
   try:
    data=os.read(file,8193).decode()
    assert len(data)<=8192
    return data.strip()
   finally: os.close(file)
  events={name:int(value) for name,value in (line.split() for line in read('memory.events').splitlines())}
  current,peak,limit=(int(read(name)) for name in ('memory.current','memory.peak','memory.max'))
  cpu=tuple(int(value) for value in read('cpu.max').split())
  pids=int(read('pids.max'))
  swap=int(read('memory.swap.max'))
  assert 0<=current<=peak and limit>0 and len(cpu)==2 and min(cpu)>0 and pids>0
  info=os.fstat(fd)
  assert open(process+'/cgroup').read()==before and os.stat(process).st_uid==uid
  result[identity]={'device':info.st_dev,'inode':info.st_ino,'uid':uid,'memory_peak':peak,'memory_current':current,
                    'memory_limit':limit,'memory_swap_limit':swap,'cpu_quota':cpu[0],'cpu_period':cpu[1],
                    'pids_limit':pids,'memory_events':events,'ancestors':ancestors}
 finally: os.close(fd)
print(json.dumps(result,separators=(',',':')))
"""


def _native_observation(program, inputs, *, deadline=None, cancel=None):
    if os.name != "posix":
        raise RuntimeError("Native storage measurement requires Linux")
    deadline = min(deadline or time.monotonic() + 35, time.monotonic() + 35)
    process = subprocess.Popen(
        ["sudo", "-n", "python3", "-c", program],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        process.stdin.write(json.dumps(inputs))
        process.stdin.close()
        process.stdin = None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or (cancel is not None and cancel.is_set()):
                raise RuntimeError("Native allocation observation deadline or cancellation")
            try:
                output, _diagnostics = process.communicate(timeout=min(0.1, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
        if process.returncode:
            raise RuntimeError("Native allocation observation could not read complete captured storage")
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        for stream in (process.stdout, process.stderr):
            stream.close()
    if len(output) > 256 * 1024:
        raise RuntimeError("Native allocation observation exceeds bounded output")
    measured = json.loads(output)
    if type(measured) is not dict or set(measured) != set(inputs):
        raise RuntimeError("Native allocation observation omits a captured root")
    return measured


def native_allocations(roots, *, deadline=None, cancel=None):
    measured = _native_observation(ALLOCATION_PROGRAM, roots, deadline=deadline, cancel=cancel)
    for value in measured.values():
        if (
            type(value) is not dict
            or set(value)
            != {"allocated", "logical", "logs", "temporary", "free_inodes", "free_bytes", "device", "inode"}
            or any(type(count) is not int or count < 0 for count in value.values())
        ):
            raise RuntimeError("Malformed native allocation counters")
    return measured


def native_kernel(nodes, *, deadline=None, cancel=None):
    measured = _native_observation(KERNEL_PROGRAM, nodes, deadline=deadline, cancel=cancel)
    for facts in measured.values():
        if (
            type(facts) is not dict
            or set(facts)
            != {
                "device",
                "inode",
                "uid",
                "memory_peak",
                "memory_current",
                "memory_limit",
                "memory_swap_limit",
                "cpu_quota",
                "cpu_period",
                "pids_limit",
                "memory_events",
                "ancestors",
            }
            or any(
                type(count) is not int or count < 0
                for name, count in facts.items()
                if name not in {"memory_events", "ancestors"}
            )
            or type(facts["memory_events"]) is not dict
            or not {"oom", "oom_kill"} <= facts["memory_events"].keys()
            or any(type(count) is not int or count < 0 for count in facts["memory_events"].values())
            or type(facts["ancestors"]) is not list
            or any(
                type(row) is not dict
                or set(row) != {"path", "device", "inode", "memory_limit"}
                or type(row["path"]) is not str
                or not row["path"].startswith("/")
                or any(type(row[key]) is not int or row[key] < 0 for key in ("device", "inode"))
                or (row["memory_limit"] is not None and (type(row["memory_limit"]) is not int or row["memory_limit"] <= 0))
                for row in facts["ancestors"]
            )
        ):
            raise RuntimeError("Malformed owned native kernel observation")
    return measured


def _validate_node_limits(facts, declared, *, baseline=None):
    keys = ("memory_limit", "cpu_quota", "cpu_period", "pids_limit")
    if (
        facts["memory_limit"] != declared["Memory"]
        or facts["pids_limit"] != declared["PidsLimit"]
        or facts["cpu_period"] <= 0
        or facts["cpu_quota"] * 10**9 != declared["NanoCpus"] * facts["cpu_period"]
        or any(declared[name] <= 0 for name in ("Memory", "NanoCpus", "PidsLimit"))
        or declared.get("MemorySwap") != declared["Memory"]
        or facts.get("memory_swap_limit") != 0
        or (baseline is not None and any(facts[key] != baseline[key] for key in keys))
        or (baseline is not None and facts.get("ancestors") != baseline.get("ancestors"))
        or (baseline is not None and declared != baseline["declared_limits"])
    ):
        raise RuntimeError("Captured native node CPU/memory/process limits changed or are unenforced")


def capture_kernel_baseline(boundary):
    """Capture event counters before application deployment, never reset them."""
    deadline = time.monotonic() + 40
    with closing(docker.DockerClient(base_url=os.environ["DOCKER_HOST"], timeout=5)) as client:
        if client.info()["ID"] != boundary["workload_engine"]:
            raise RuntimeError("Native kernel baseline engine identity changed")
        nodes, declared = {}, {}
        for name in boundary["nodes"]:
            node = client.containers.get(name)
            if not node.attrs["State"]["Running"] or not node.attrs["Config"]["Labels"].get("io.x-k8s.kind.cluster"):
                raise RuntimeError("Native kernel baseline requires captured live Kind nodes")
            nodes[node.id] = node.attrs["State"]["Pid"]
            declared[node.id] = {
                key: node.attrs["HostConfig"][key] for key in ("Memory", "MemorySwap", "NanoCpus", "PidsLimit")
            }
        measured = native_kernel(nodes, deadline=deadline)
        if any(facts["uid"] != boundary["workload_uid"] for facts in measured.values()):
            raise RuntimeError("Native kernel baseline owner differs from captured workload UID")
        for identity, facts in measured.items():
            _validate_node_limits(facts, declared[identity])
            facts["declared_limits"] = declared[identity]
        for name in boundary["nodes"]:
            node = client.containers.get(name)
            if node.id not in measured or node.attrs["State"]["Pid"] != nodes[node.id]:
                raise RuntimeError("Native kernel baseline node identity changed")
            measured[node.id]["node_name"] = name
        return measured


def admit_native_memory(capacity, tier, *, foreign_reserve_gib, baseline=None, boundary=None):
    """Reserve full frozen node ceilings, with no credit for current/cache usage.

    Without a baseline this is prospective provisioning admission only. Actual
    deployment also requires independent Docker/kernel and ancestor evidence.
    """
    if tier != TIERS["large"]:
        raise ValueError("Native memory admission requires the complete frozen Large tier")
    if type(foreign_reserve_gib) is not int or not 0 <= foreign_reserve_gib <= 1024:
        raise ValueError("Foreign memory reservation requires a bounded nonnegative integer")
    if tier.cpu_limit > capacity.physical_cores * 3 // 4:
        raise ValueError("Workload exceeds the physical CPU headroom budget")
    if tier.disk_gib_limit > capacity.available_disk_gib * 3 // 4 - 20:
        raise ValueError("Workload exceeds the disk headroom budget")
    node_count = 1 + tier.regions * tier.worker_nodes_per_region
    outer_gib = 6 + (node_count - 1) * 20
    required_gib = outer_gib + 8 + 16 + foreign_reserve_gib
    if capacity.available_memory_gib < required_gib:
        raise ValueError("Native node, verifier, host and foreign memory reserves exceed available RAM")
    if baseline is not None:
        if boundary is None or len(baseline) != node_count or len(boundary["nodes"]) != node_count:
            raise RuntimeError("Native Large admission lacks the exact captured ten-node inventory")
        names = {facts["node_name"] for facts in baseline.values()}
        if len(names) != node_count or names != set(boundary["nodes"]):
            raise RuntimeError("Native Large admission node names differ from the captured boundary")
        ancestors = {}
        ancestor_paths = {}
        controls = 0
        for facts in baseline.values():
            control = facts["node_name"].endswith("control-plane")
            controls += int(control)
            declared = facts["declared_limits"]
            _validate_node_limits(facts, declared)
            if (
                facts["uid"] != boundary["workload_uid"]
                or declared["Memory"] != (6 if control else 20) * GIB
                or declared["NanoCpus"] != (3 if control else 5) * 10**9
                or declared["PidsLimit"] != 8192
                or not facts["ancestors"]
            ):
                raise RuntimeError("Native Large admission differs from the frozen resource profile")
            for ancestor in facts["ancestors"]:
                identity = (ancestor["path"], ancestor["device"], ancestor["inode"])
                if ancestor_paths.setdefault(ancestor["path"], identity) != identity:
                    raise RuntimeError("Native memory ancestor identity changed during capture")
                previous, reserved = ancestors.get(identity, (ancestor["memory_limit"], 0))
                if previous != ancestor["memory_limit"]:
                    raise RuntimeError("Native memory ancestor limits changed during capture")
                ancestors[identity] = (previous, reserved + declared["Memory"])
        if controls != 1:
            raise RuntimeError("Native Large admission requires one captured control plane")
        if any(limit is not None and limit < reserved + GIB for limit, reserved in ancestors.values()):
            raise ValueError("Native ancestor lacks node ceilings and runtime overhead reserve")
    return {
        "policy": "native-node-ceilings-v1",
        "phase": "actual" if baseline is not None else "prospective",
        "node_count": node_count,
        "node_memory_ceiling_gib": outer_gib,
        "verifier_memory_ceiling_gib": 8,
        "host_guard_gib": 16,
        "foreign_reserve_gib": foreign_reserve_gib,
        "required_memory_gib": required_gib,
        "available_memory_gib": capacity.available_memory_gib,
        "current_usage_credit_gib": 0,
    }


class NativeStorageObserver:
    def __init__(self, app, private_dir, *, collector=native_allocations, cancel=None):
        self.app, self.private_dir, self.collector = app, Path(private_dir).resolve(strict=True), collector
        self.admission = app.capacity_observation
        if not self.admission or not self.admission.get("boundary"):
            raise RuntimeError("Native monitor requires captured deployment capacity ownership")
        self.boundary = self.admission["boundary"]
        self.cancel = cancel if cancel is not None else threading.Event()
        self.client = docker.DockerClient(base_url=os.environ["DOCKER_HOST"], timeout=10)
        self.nodes, self.roots, self.store_roles = (
            {},
            {
                "owner": str(self.private_dir),
                "filesystem:trusted": str(self.admission["trusted_storage_root"]),
            },
            {},
        )
        self.root_identities = None
        self.store_names = {}
        self.quotas = {}
        try:
            self._capture()
        except BaseException:
            self.client.close()
            raise

    def _budget(self, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0 or self.cancel.is_set():
            raise RuntimeError("Native resource observation deadline or cancellation")
        self.client.api.timeout = min(3, remaining)
        return min(5, remaining)

    def _owned(self, node, deadline):
        self._budget(deadline)
        node.reload()
        record = node.attrs
        labels = record["Config"]["Labels"]
        if labels.get("io.x-k8s.kind.cluster") != self.cluster or record["Id"] != self.nodes.get(
            node.name, record["Id"]
        ):
            raise RuntimeError("Captured workload node identity changed")
        return record

    def _capture(self):
        deadline = time.monotonic() + 120
        self._budget(deadline)
        info = self.client.info()
        if info["ID"] != self.boundary["workload_engine"]:
            raise RuntimeError("Native monitor workload engine identity changed")
        root = Path(self.admission["workload_storage_root"])
        self.cluster = None
        for name in self.boundary["nodes"]:
            self._budget(deadline)
            node = self.client.containers.get(name)
            labels = node.attrs["Config"]["Labels"]
            cluster = labels.get("io.x-k8s.kind.cluster")
            if not cluster or (self.cluster is not None and cluster != self.cluster):
                raise RuntimeError("Captured workload nodes span different clusters")
            self.cluster = cluster
            self.nodes[name] = node.id
            record = self._owned(node, deadline)
            mounts = [entry for entry in record["Mounts"] if entry["Destination"] == "/var"]
            if (
                len(mounts) != 1
                or mounts[0]["Type"] != "volume"
                or not re.fullmatch(r"[a-f0-9]{64}", mounts[0]["Name"])
            ):
                raise RuntimeError("Owned Kind node lacks an exact native data volume")
            expected = root / "volumes" / mounts[0]["Name"] / "_data"
            if Path(mounts[0]["Source"]) != expected:
                raise RuntimeError("Owned node volume differs from the captured Docker root")
            self.roots["node:" + name] = str(expected)
            upper = record.get("GraphDriver", {}).get("Data", {}).get("UpperDir")
            if not upper or not Path(upper).is_relative_to(root) or ".." in Path(upper).parts:
                raise RuntimeError("Native node writable-layer allocation is unavailable")
            self.roots["layer:" + name] = upper
        core = self.app._client().core_v1_api
        for namespace in self.app.namespaces:
            quota = core.read_namespaced_resource_quota(
                "service-resources", namespace, _request_timeout=self._budget(deadline)
            )
            hard = quota.spec.hard or {}
            expected = {
                "limits.cpu": self.app.tier.cpu_limit // self.app.tier.regions,
                "limits.memory": self.app.tier.memory_gib_limit // self.app.tier.regions * GIB,
                "requests.storage": self.app.tier.disk_gib_limit // self.app.tier.regions * GIB,
                "pods": 120,
            }
            if not quota.metadata.uid or any(
                parse_quantity(hard.get(name, "0")) != value for name, value in expected.items()
            ):
                raise RuntimeError("Deployed resource quota differs from frozen application capacity")
            self.quotas[namespace] = (quota.metadata.uid, dict(hard))
        for resource in self.app.inventory.resources:
            if resource.kind != "PersistentVolumeClaim":
                continue
            claim = core.read_namespaced_persistent_volume_claim(
                resource.name, resource.namespace, _request_timeout=self._budget(deadline)
            )
            if claim.metadata.uid != resource.uid or not claim.spec.volume_name:
                raise RuntimeError("Captured workload storage claim identity changed")
            volume = core.read_persistent_volume(claim.spec.volume_name, _request_timeout=self._budget(deadline))
            if (
                volume.spec.claim_ref.uid != resource.uid
                or volume.spec.claim_ref.namespace != resource.namespace
                or volume.spec.claim_ref.name != resource.name
                or volume.spec.host_path is None
            ):
                raise RuntimeError("Native storage claim does not own its actual host path")
            terms = volume.spec.node_affinity.required.node_selector_terms
            hosts = [
                value
                for term in terms
                for expression in term.match_expressions
                if expression.key == "kubernetes.io/hostname" and expression.operator == "In"
                for value in expression.values
            ]
            if len(hosts) != 1 or hosts[0] not in self.nodes:
                raise RuntimeError("Native storage is not bound to exactly one captured node")
            path = Path(volume.spec.host_path.path)
            if (
                not any(path.is_relative_to(base) for base in ("/var/local-path-provisioner", "/var/openebs"))
                or ".." in path.parts
                or resource.uid not in path.name
            ):
                raise RuntimeError("Native storage host path differs from its captured claim")
            label = "store:" + resource.uid
            self.roots[label] = str(Path(self.roots["node:" + hosts[0]]) / path.relative_to("/var"))
            self.store_roles[label] = self._store_role(resource.name)
            self.store_names[label] = resource.namespace + "/" + resource.name

    @staticmethod
    def _store_role(name):
        for prefix, role in (
            ("mysql-", "sql"),
            ("search-", "index"),
            ("repository-", "git"),
            ("artifact-", "artifact"),
            ("artifacts-", "artifact"),
            ("queue-", "queue"),
            ("delivery-", "delivery"),
        ):
            if prefix in name:
                return role
        return "other"

    def _application_resources(self, deadline):
        core = self.app._client().core_v1_api
        observed = {}
        for namespace, (identity, hard) in self.quotas.items():
            quota = core.read_namespaced_resource_quota(
                "service-resources", namespace, _request_timeout=self._budget(deadline)
            )
            if quota.metadata.uid != identity or quota.spec.hard != hard:
                raise RuntimeError("Captured application resource quota changed")
            pods = core.list_namespaced_pod(namespace, _request_timeout=self._budget(deadline)).items
            if len(pods) > 120:
                raise RuntimeError("Application pod inventory exceeds its enforced quota")
            cpu, memory, captured = 0, 0, []
            for pod in pods:
                if pod.status.phase in {"Succeeded", "Failed"}:
                    continue
                if pod.spec.node_name and pod.spec.node_name not in self.nodes:
                    raise RuntimeError("Application resources escaped captured workload nodes")
                regular, initial = [], []
                for containers, target in ((pod.spec.containers, regular), (pod.spec.init_containers or (), initial)):
                    for container in containers:
                        resources = container.resources
                        limits, requests = resources.limits or {}, resources.requests or {}
                        values = {}
                        for name in ("cpu", "memory"):
                            limit, request = (
                                parse_quantity(limits.get(name, "0")),
                                parse_quantity(requests.get(name, "0")),
                            )
                            if limit <= 0 or request <= 0 or request > limit:
                                raise RuntimeError(
                                    "Every application container requires bounded CPU/memory requests and limits"
                                )
                            values[name] = limit
                        target.append(values)
                        captured.append(
                            {
                                "pod": pod.metadata.uid,
                                "container": container.name,
                                "limits": limits,
                                "requests": requests,
                            }
                        )
                overhead = pod.spec.overhead or {}
                for name in ("cpu", "memory"):
                    sidecars, init_peak = 0, 0
                    for container, item in zip(pod.spec.init_containers or (), initial, strict=True):
                        if container.restart_policy == "Always":
                            sidecars += item[name]
                            init_peak = max(init_peak, sidecars)
                        else:
                            init_peak = max(init_peak, sidecars + item[name])
                    requested = max(sum(item[name] for item in regular) + sidecars, init_peak)
                    requested += parse_quantity(overhead.get(name, "0"))
                    if name == "cpu":
                        cpu += requested
                    else:
                        memory += requested
            if cpu > parse_quantity(hard["limits.cpu"]) or memory > parse_quantity(hard["limits.memory"]):
                raise RuntimeError("Live application container limits exceed captured quota")
            observed[namespace] = {
                "quota_uid": identity,
                "hard": hard,
                "cpu_limit_nanocores": int(cpu * 10**9),
                "memory_limit_bytes": int(memory),
                "containers": captured,
            }
        return observed

    def sample(self):
        deadline = time.monotonic() + 40
        self._budget(deadline)
        if self.client.info()["ID"] != self.boundary["workload_engine"]:
            raise RuntimeError("Native monitor engine identity changed")
        memory, cpu, oom, memory_limit, cpu_limit = 0, 0, False, 0, 0
        kernel_nodes, declared_limits = {}, {}
        for name, identity in self.nodes.items():
            self._budget(deadline)
            node = self.client.containers.get(identity)
            record = self._owned(node, deadline)
            if node.name != name or not record["State"]["Running"]:
                raise RuntimeError("Native monitor lost a captured workload node")
            current_roots = {
                "node:" + name: next(entry["Source"] for entry in record["Mounts"] if entry["Destination"] == "/var"),
                "layer:" + name: record["GraphDriver"]["Data"]["UpperDir"],
            }
            if any(self.roots[label] != value for label, value in current_roots.items()):
                raise RuntimeError("Captured native storage changed")
            self._budget(deadline)
            stats = node.stats(stream=False)
            self._budget(deadline)
            memory += stats["memory_stats"]["usage"]
            cpu += stats["cpu_stats"]["cpu_usage"]["total_usage"]
            oom |= record["State"]["OOMKilled"]
            limits = record["HostConfig"]
            if limits["Memory"] <= 0 or limits["NanoCpus"] <= 0 or limits["PidsLimit"] <= 0:
                raise RuntimeError("Owned workload nodes require independently enforced CPU, memory and process limits")
            memory_limit += limits["Memory"]
            cpu_limit += limits["NanoCpus"]
            kernel_nodes[identity] = record["State"]["Pid"]
            declared_limits[identity] = {key: limits[key] for key in ("Memory", "MemorySwap", "NanoCpus", "PidsLimit")}
        kernel = native_kernel(kernel_nodes, deadline=deadline, cancel=self.cancel)
        baseline = self.admission.get("kernel_baseline")
        if baseline is None or set(baseline) != set(kernel):
            raise RuntimeError("Owned native kernel events lack their pre-deployment baseline")
        for identity, facts in kernel.items():
            prior = baseline[identity]
            _validate_node_limits(facts, declared_limits[identity], baseline=prior)
            if (facts["device"], facts["inode"], facts["uid"]) != (prior["device"], prior["inode"], prior["uid"]):
                raise RuntimeError("Owned kernel cgroup identity changed")
            if facts["memory_peak"] < prior["memory_peak"] or any(
                facts["memory_events"].get(name, -1) < count for name, count in prior["memory_events"].items()
            ):
                raise RuntimeError("Owned kernel counters were reset")
            oom |= any(facts["memory_events"][name] > prior["memory_events"][name] for name in ("oom", "oom_kill"))
        application = self._application_resources(deadline)
        measured = self.collector(self.roots, deadline=deadline, cancel=self.cancel)
        self._budget(deadline)
        identities = {label: (facts["device"], facts["inode"]) for label, facts in measured.items()}
        if self.root_identities is not None and identities != self.root_identities:
            raise RuntimeError("Captured native allocation root identity changed")
        self.root_identities = identities
        categories = {
            role: sum(measured[label]["allocated"] for label, value in self.store_roles.items() if value == role)
            for role in ("sql", "index", "git", "artifact", "queue", "delivery", "other")
        }
        # Categorized PVs overlap the node data roots. Count node/layer roots
        # once for physical admission; store sizes are a separate attribution.
        total = sum(value["allocated"] for label, value in measured.items() if label.startswith(("node:", "layer:")))
        footprint = StorageFootprint(
            categories["sql"],
            categories["index"],
            categories["git"],
            categories["artifact"],
            sum(value["logs"] for label, value in measured.items() if label.startswith(("node:", "layer:"))),
            measured["owner"]["temporary"],
        )
        capacities = {
            name: asdict(HostCapacity.observe(Path(self.admission[name + "_storage_root"])))
            for name in ("workload", "trusted", "owner")
        }
        return {
            "at": time.monotonic(),
            "memory_bytes": memory,
            "cpu_nanoseconds": cpu,
            "oom": oom,
            "node_memory_limit_bytes": memory_limit,
            "node_cpu_limit_nanocores": cpu_limit,
            "kernel_nodes": kernel,
            "application_resources": application,
            "node_lifetime_peak_sum_upper_bound_bytes": sum(facts["memory_peak"] for facts in kernel.values()),
            "node_allocated_bytes": total,
            "owner_allocated_bytes": measured["owner"]["allocated"],
            "stores": categories,
            "store_allocations": {self.store_names[label]: measured[label]["allocated"] for label in self.store_roles},
            "storage": asdict(footprint),
            "native_roots": measured,
            "capacities": capacities,
        }

    def close(self):
        self.cancel.set()
        self.client.close()


class CapacityMonitor:
    def __init__(self, observer, path, *, cancel, disk_budget, reserve_gib, interval=30, verifier_reserve_bytes=0):
        if type(verifier_reserve_bytes) is not int or not 0 <= verifier_reserve_bytes <= 8 * GIB:
            raise ValueError("Verifier reserve requires a bounded integer byte count")
        self.observer, self.path, self.cancel = observer, Path(path), cancel
        self.disk_budget, self.reserve_gib, self.interval = disk_budget, reserve_gib, interval
        self.stop_event, self.thread, self.error = threading.Event(), None, None
        self.latest, self.initial, self.peak_memory_bytes = None, None, 0
        self.peak_allocated_and_reserved_bytes = 0
        self._lock = threading.Lock()
        self._sample_lock = threading.Lock()
        self.verifier_reserve_bytes = verifier_reserve_bytes

    def observe(self):
        with self._sample_lock:
            return self._observe()

    def _observe(self):
        sample = self.observer.sample()
        with self._lock:
            if self.initial is None:
                self.initial = sample
            # Include already allocated deployment/runtime storage and retained
            # owner evidence. Initial allocations are not a free disk allowance.
            allocated = sample["node_allocated_bytes"] + sample["owner_allocated_bytes"] + self.verifier_reserve_bytes
            sample["verifier_reserved_bytes"] = self.verifier_reserve_bytes
            sample["combined_allocated_and_reserved_bytes"] = allocated
            self.peak_allocated_and_reserved_bytes = max(self.peak_allocated_and_reserved_bytes, allocated)
            self.peak_memory_bytes = max(self.peak_memory_bytes, sample["memory_bytes"])
            reserve = max(self.reserve_gib, (2 * sample["stores"]["sql"] + sample["owner_allocated_bytes"]) // GIB + 8)
            sample["capacity_failure_reasons"] = [
                reason
                for reason, failed in (
                    ("unattributed_workload_oom", sample["oom"]),
                    ("combined_storage_ceiling", allocated > self.disk_budget),
                    (
                        "disk_reserve",
                        min(facts["available_disk_gib"] for facts in sample["capacities"].values()) < reserve,
                    ),
                    (
                        "host_memory_reserve",
                        min(facts["available_memory_gib"] for facts in sample["capacities"].values()) < 16,
                    ),
                    ("storage_inodes", min(facts["free_inodes"] for facts in sample["native_roots"].values()) < 8192),
                )
                if failed
            ]
            # Publish the fatal state before potentially blocking persistence.
            # Cleanup may cancel I/O, but cannot erase already observed facts.
            if sample["capacity_failure_reasons"] and self.error is None:
                self.error = RuntimeError("Native workload/control capacity reserve is exhausted")
            self.latest = sample
            sample["campaign_sampled_peak_memory_bytes"] = self.peak_memory_bytes
            sample["campaign_peak_allocated_and_reserved_bytes"] = self.peak_allocated_and_reserved_bytes
            if self.path.exists() and self.path.stat().st_size > 32 * 1024**2:
                previous = self.path.with_name(self.path.name + ".1")
                if self.path.is_symlink() or previous.is_symlink():
                    raise RuntimeError("Private capacity history ownership changed")
                # Retain bounded recent full samples. Every new sample carries
                # the campaign peak counters, including before log rotation.
                os.replace(self.path, previous)
            descriptor = os.open(self.path, os.O_CREAT | os.O_APPEND | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                file.write(json.dumps(sample, sort_keys=True) + "\n")
                file.flush()
                if sample["capacity_failure_reasons"]:
                    os.fsync(file.fileno())
            # Preserve the first fatal counters before cancellation/cleanup.
            # Native exhaustion alone is not evidence that a solver caused it.
            if sample["capacity_failure_reasons"]:
                raise self.error
        return sample

    def _run(self):
        while not self.stop_event.wait(self.interval):
            try:
                self.observe()
            except Exception as error:
                if self.stop_event.is_set() and self.error is None:
                    return
                if self.error is None:
                    self.error = error
                self.cancel()
                return

    def start(self):
        try:
            self.observe()
        except Exception as error:
            self.error = error
            self.cancel()
            raise
        self.thread = threading.Thread(target=self._run, name="private-capacity-monitor", daemon=True)
        self.thread.start()

    def assert_available(self):
        if self.error is not None or self.latest is None or (self.thread and not self.thread.is_alive()):
            raise RuntimeError("Private native resource observation is unavailable") from self.error
        if time.monotonic() - self.latest["at"] > self.interval + 60:
            raise RuntimeError("Private native resource observation is stale")

    def reserve_restore(self, *, additional_bytes=0):
        if type(additional_bytes) is not int or additional_bytes < 0:
            raise ValueError("Restore reservation requires a nonnegative byte count")
        self.assert_available()
        sample = self.observe()
        allocated = sample["node_allocated_bytes"] + sample["owner_allocated_bytes"] + self.verifier_reserve_bytes
        if allocated + additional_bytes > self.disk_budget:
            raise RuntimeError("Projected recovery allocation exceeds the owned disk budget")
        reserve = additional_bytes // GIB + self.reserve_gib
        if min(facts["available_disk_gib"] for facts in sample["capacities"].values()) < reserve:
            raise RuntimeError("Projected recovery allocation exceeds the native storage reserve")

    def assert_completed(self):
        """Check retained resource validity after a successful owner drain."""
        if not self.stop_event.is_set() or (self.thread and self.thread.is_alive()):
            raise RuntimeError("Private native resource observation has not drained")
        if self.error is not None:
            raise RuntimeError("Private native resource observation failed") from self.error

    def stop(self):
        self.stop_event.set()
        cancelled = getattr(self.observer, "cancel", None)
        if cancelled is not None:
            cancelled.set()
        try:
            if self.thread:
                self.thread.join(timeout=45)
                if self.thread.is_alive():
                    raise RuntimeError("Private native resource observer did not stop")
        finally:
            self.observer.close()
