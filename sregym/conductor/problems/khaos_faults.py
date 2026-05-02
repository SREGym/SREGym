from __future__ import annotations

import threading
from collections.abc import Sequence
from enum import StrEnum

from pydantic import BaseModel, Field

from sregym.conductor.oracles.alert_oracle import AlertOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_hw import HWFaultInjector
from sregym.generators.fault.inject_kernel import KernelInjector
from sregym.paths import TARGET_MICROSERVICES
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.utils.decorators import mark_fault_injected


class KhaosFaultName(StrEnum):
    # kprobe faults
    read_error = "read_error"
    pread_error = "pread_error"
    write_error = "write_error"
    pwrite_error = "pwrite_error"
    fsync_error = "fsync_error"
    open_error = "open_error"
    close_fail = "close_fail"
    dup_fail = "dup_fail"
    getrandom_fail = "getrandom_fail"
    gettimeofday_fail = "gettimeofday_fail"
    ioctl_fail = "ioctl_fail"
    cuda_malloc_fail = "cuda_malloc_fail"
    getaddrinfo_fail = "getaddrinfo_fail"
    nanosleep_throttle = "nanosleep_throttle"
    nanosleep_interrupt = "nanosleep_interrupt"
    fork_fail = "fork_fail"
    clock_drift = "clock_drift"
    setns_fail = "setns_fail"
    prlimit_fail = "prlimit_fail"
    socket_block = "socket_block"
    mmap_fail = "mmap_fail"
    mmap_oom = "mmap_oom"
    brk_fail = "brk_fail"
    mlock_fail = "mlock_fail"
    bind_enetdown = "bind_enetdown"
    mount_io_error = "mount_io_error"
    # kretprobe faults
    force_close_ret_err = "force_close_ret_err"
    force_read_ret_ok = "force_read_ret_ok"
    force_open_ret_eperm = "force_open_ret_eperm"
    force_mmap_eagain = "force_mmap_eagain"
    force_brk_eagain = "force_brk_eagain"
    force_mlock_eperm = "force_mlock_eperm"
    force_mprotect_eacces = "force_mprotect_eacces"
    force_swapon_einval = "force_swapon_einval"
    # memory corruption faults
    oom_memchunk = "oom_memchunk"
    oom_heapspace = "oom_heapspace"
    oom_nonswap = "oom_nonswap"
    hfrag_memchunk = "hfrag_memchunk"
    hfrag_heapspace = "hfrag_heapspace"
    ptable_permit = "ptable_permit"
    stack_rndsegfault = "stack_rndsegfault"
    thrash_swapon = "thrash_swapon"
    thrash_swapoff = "thrash_swapoff"
    memleak_munmap = "memleak_munmap"
    # network packet loss
    packet_loss_sendto = "packet_loss_sendto"
    packet_loss_recvfrom = "packet_loss_recvfrom"
    # disk faults
    latent_sector_error = "latent_sector_error"


# Disk faults that intercept read/pread syscalls need the page cache dropped
# so the application is forced to issue new reads that hit the eBPF probes.
_NEEDS_CACHE_DROP: frozenset[str] = frozenset(
    {
        KhaosFaultName.latent_sector_error,
        KhaosFaultName.read_error,
        KhaosFaultName.pread_error,
        KhaosFaultName.force_read_ret_ok,
    }
)

# Backwards-compatible alias; older code (and the reinjection monitor) reads this.
_DISK_FAULTS: frozenset[str] = _NEEDS_CACHE_DROP


class _FaultReinjectionMonitor:
    """Background thread that detects pod restarts and re-injects the eBPF fault
    into the new host PID.  Follows the daemon-thread pattern used by NoiseManager."""

    def __init__(
        self,
        injector: HWFaultInjector,
        namespace: str,
        node: str,
        fault_type: str,
        params: list[int | str] | None,
    ):
        self._injector = injector
        self._namespace = namespace
        self._node = node
        self._fault_type = fault_type
        self._params = params
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        # pod_name -> container_id (last known)
        self._container_ids: dict[str, str] = {}

    # ------------------------------------------------------------------

    def start(self) -> None:
        """Snapshot current container IDs and spawn the monitor thread."""
        for pod_ref in self._injector._get_pods_on_node(self._namespace, self._node):
            ns, pod = self._injector._split_ns_pod(pod_ref)
            try:
                cid = self._injector._get_container_id(ns, pod)
                self._container_ids[pod_ref] = cid
            except Exception:
                print(f"[reinjection-monitor] Could not snapshot container ID for {pod_ref}")

        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()
        print(
            f"[reinjection-monitor] Started for {self._fault_type} on node {self._node} "
            f"(tracking {len(self._container_ids)} pods)"
        )

    def stop(self) -> None:
        """Signal the loop to stop and wait for the thread to finish."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
        print("[reinjection-monitor] Stopped")

    # ------------------------------------------------------------------

    def _monitor_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._check_pods()
            except Exception as exc:
                print(f"[reinjection-monitor] Error in monitor loop: {exc}")
            # Sleep ~5 s, but wake early if stop_event is set
            if self._stop_event.wait(timeout=5):
                break

    def _check_pods(self) -> None:
        pods = self._injector._get_pods_on_node(self._namespace, self._node)
        for pod_ref in pods:
            # Bail quickly if recovery has started. Without this check, the
            # monitor could be mid-pod-iteration when stop() is called and
            # would still pin a fresh probe via _exec_khaos_fault_on_node,
            # producing a stale BPF pin that survives the subsequent
            # `khaos --recover` call (which only detaches one probe at a time).
            if self._stop_event.is_set():
                return
            try:
                ns, pod = self._injector._split_ns_pod(pod_ref)

                # Pod may be terminating or not yet running — skip quietly.
                try:
                    cid = self._injector._get_container_id(ns, pod)
                except RuntimeError:
                    continue

                prev_cid = self._container_ids.get(pod_ref)

                if prev_cid is not None and prev_cid == cid:
                    continue  # no change

                # New pod or restarted container — re-inject. Re-check the
                # stop flag right before the kubectl exec that would actually
                # pin a probe, since _get_host_pid_on_node can take a few
                # seconds and stop() may have fired in the meantime.
                host_pid = self._injector._get_host_pid_on_node(self._node, cid)
                if self._stop_event.is_set():
                    return
                print(
                    f"[reinjection-monitor] Re-injecting {self._fault_type} into "
                    f"PID {host_pid} (pod {pod_ref}, container {cid[:12]})"
                )
                self._injector._exec_khaos_fault_on_node(self._node, self._fault_type, host_pid, self._params)

                if self._fault_type in _NEEDS_CACHE_DROP:
                    kernel_injector = KernelInjector(self._injector.kubectl)
                    kernel_injector.drop_caches(self._node, show_log=False)
                    print(f"[reinjection-monitor] Dropped caches on {self._node} after re-injection")

                self._container_ids[pod_ref] = cid
            except Exception as exc:
                print(f"[reinjection-monitor] Failed to re-inject fault for pod {pod_ref}: {exc}")


class KhaosFaultConfig(BaseModel):
    name: KhaosFaultName
    description: str
    default_args: list[int | str] = Field(default_factory=list)


class KhaosFaultProblem(Problem):
    def __init__(
        self,
        fault_name: KhaosFaultName | str,
        target_node: str | None = None,
        inject_args: list[int | str] | None = None,
    ):
        self.app = HotelReservation()
        super().__init__(app=self.app, namespace=self.app.namespace)
        self.kubectl = self.app.kubectl if hasattr(self.app, "kubectl") else None
        self.namespace = self.app.namespace
        self.injector = HWFaultInjector()
        self.target_node = target_node

        try:
            self.fault_name = KhaosFaultName(fault_name)
            cfg = KHAOS_FAULT_CONFIGS[self.fault_name]
        except Exception as e:
            raise ValueError(f"Fault name or config is missing for fault_name '{fault_name}'. Error: {e}") from e

        # Pick default args if none provided; caller can override via inject_args
        self.inject_args = inject_args if inject_args is not None else list(cfg.default_args)

        # (Optional) pick a request mix payload
        self.app.payload_script = (
            TARGET_MICROSERVICES / "hotelReservation/wrk2/scripts/hotel-reservation/mixed-workload_type_1.lua"
        )

        self._reinjection_monitor: _FaultReinjectionMonitor | None = None

        self.root_cause = cfg.description

        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.mitigation_oracle = AlertOracle(problem=self)

        self.app.create_workload()

    def requires_khaos(self) -> bool:
        return True

    @mark_fault_injected
    def inject_fault(self):
        print(f"== Fault Injection: {self.fault_name.value} ==")
        self.target_node = self.injector.inject_node(
            self.namespace,
            self.fault_name.value,
            self.target_node,
            params=self.inject_args,
        )
        print(f"Injected {self.fault_name.value} into pods on node {self.target_node}\n")

        # Disk faults intercept read/pread syscalls via eBPF. Data already in
        # the page cache will be served without issuing those syscalls, so we
        # must drop caches to force the application to re-read from disk.
        if self.fault_name in _NEEDS_CACHE_DROP and self.target_node:
            print("Dropping page caches to force disk reads through eBPF probes...")
            kernel_injector = KernelInjector(self.injector.kubectl)
            kernel_injector.drop_caches(self.target_node)

        # eBPF probes are pinned to host PIDs. When Kubernetes restarts a
        # crashed pod, the new container gets a new PID and the fault
        # disappears. Start a background monitor that re-injects on restart.
        if self.target_node:
            self._reinjection_monitor = _FaultReinjectionMonitor(
                injector=self.injector,
                namespace=self.namespace,
                node=self.target_node,
                fault_type=self.fault_name.value,
                params=self.inject_args,
            )
            self._reinjection_monitor.start()

    @mark_fault_injected
    def recover_fault(self):
        print(f"== Fault Recovery: {self.fault_name.value} on node {self.target_node} ==")
        # Stop the re-injection monitor first so it doesn't race with recovery.
        if self._reinjection_monitor is not None:
            self._reinjection_monitor.stop()
            self._reinjection_monitor = None
        if self.target_node:
            self.injector.recover_node(self.namespace, self.fault_name.value, self.target_node)
        else:
            print("[warn] No target node recorded; attempting best-effort recovery.")
        print("Recovery request sent.\n")


_FAULT_CONFIG_ENTRIES: Sequence[tuple[KhaosFaultName, str, list[int | str]]] = [
    # kprobe faults
    (KhaosFaultName.read_error, "read() returns EIO, leading to application I/O failures.", []),
    (KhaosFaultName.pread_error, "pread64() returns EIO, breaking file reads.", []),
    (KhaosFaultName.write_error, "write() returns ENOSPC-like errors, as if the disk is full.", []),
    (KhaosFaultName.pwrite_error, "pwrite64() fails as if the target storage is full.", []),
    (KhaosFaultName.fsync_error, "fsync() fails, so writes are not persisted.", []),
    (KhaosFaultName.open_error, "openat() is denied, preventing files from opening.", []),
    (KhaosFaultName.close_fail, "close() returns EBADF-like errors, leaving FDs open.", []),
    (KhaosFaultName.dup_fail, "dup() fails as if file descriptor limits are hit.", []),
    (KhaosFaultName.getrandom_fail, "getrandom() returns errors (e.g., EAGAIN), breaking randomness consumers.", []),
    (KhaosFaultName.gettimeofday_fail, "gettimeofday() returns errors, disrupting time reads.", []),
    (KhaosFaultName.ioctl_fail, "ioctl() returns errors (e.g., ENOTTY), blocking control calls.", []),
    (KhaosFaultName.cuda_malloc_fail, "ioctl()-based GPU alloc requests behave as ENOMEM.", []),
    (KhaosFaultName.getaddrinfo_fail, "DNS name resolution fails, causing address lookup errors.", []),
    (KhaosFaultName.nanosleep_throttle, "nanosleep() errors cause sleeps to be throttled.", []),
    (KhaosFaultName.nanosleep_interrupt, "nanosleep() returns EINTR-like interruptions.", []),
    (KhaosFaultName.fork_fail, "fork() fails as under EAGAIN/ENOMEM pressure.", []),
    (KhaosFaultName.clock_drift, "clock_gettime() errors manifest as time drift symptoms.", []),
    (KhaosFaultName.setns_fail, "setns() fails, preventing namespace switches.", []),
    (KhaosFaultName.prlimit_fail, "prlimit64() errors prevent limit changes from applying.", []),
    (KhaosFaultName.socket_block, "socket() creation fails with generic errors.", []),
    (KhaosFaultName.mmap_fail, "mmap() returns ENOMEM, blocking new mappings.", []),
    (KhaosFaultName.mmap_oom, "mmap() behaves as OOM, rejecting new mappings.", []),
    (KhaosFaultName.brk_fail, "brk() cannot grow the heap, as if memory is exhausted.", []),
    (KhaosFaultName.mlock_fail, "mlock() returns ENOMEM/EPERM, blocking page pinning.", []),
    (KhaosFaultName.bind_enetdown, "bind() fails with ENETDOWN, as if the interface is down.", []),
    (KhaosFaultName.mount_io_error, "mount() returns I/O errors similar to EIO.", []),
    # kretprobe faults
    (KhaosFaultName.force_close_ret_err, "close() exits with -1 regardless of outcome.", []),
    (KhaosFaultName.force_read_ret_ok, "read() reports EOF (0 bytes) even when data exists.", []),
    (KhaosFaultName.force_open_ret_eperm, "openat() returns EPERM, denying access.", []),
    (KhaosFaultName.force_mmap_eagain, "mmap() returns EAGAIN, indicating temporary failure.", []),
    (KhaosFaultName.force_brk_eagain, "brk() returns EAGAIN, blocking heap growth.", []),
    (KhaosFaultName.force_mlock_eperm, "mlock() returns EPERM, disallowing memory pinning.", []),
    (KhaosFaultName.force_mprotect_eacces, "mprotect() returns EACCES, blocking permission changes.", []),
    (KhaosFaultName.force_swapon_einval, "swapon() returns EINVAL, blocking swap activation.", []),
    # memory corruption faults
    (KhaosFaultName.oom_memchunk, "Memory chunk allocation fails with ENOMEM, causing out-of-memory pressure.", []),
    (KhaosFaultName.oom_heapspace, "brk() returns ENOMEM, exhausting heap space.", []),
    (KhaosFaultName.oom_nonswap, "mlock() returns ENOMEM, preventing swap-backed growth.", []),
    (
        KhaosFaultName.hfrag_memchunk,
        "Memory chunk allocation fails with EAGAIN under heavy fragmentation pressure.",
        [],
    ),
    (KhaosFaultName.hfrag_heapspace, "Heap growth fails with EAGAIN under heap fragmentation pressure.", []),
    (KhaosFaultName.ptable_permit, "mlock() returns EPERM, blocking page table pinning.", []),
    (KhaosFaultName.stack_rndsegfault, "mprotect() returns EACCES, leading to stack faults.", []),
    (KhaosFaultName.thrash_swapon, "swapon() returns EINVAL/EPERM, preventing swap use.", []),
    (KhaosFaultName.thrash_swapoff, "swapoff() returns EPERM-like errors, blocking swap disable.", []),
    (KhaosFaultName.memleak_munmap, "munmap() returns EINVAL, leaking mappings.", []),
    # network packet loss
    (
        KhaosFaultName.packet_loss_sendto,
        "Outbound network packets are dropped at the specified rate, causing transmission failures.",
        [30],
    ),
    (
        KhaosFaultName.packet_loss_recvfrom,
        "Inbound network packets are dropped at the specified rate, causing receive failures.",
        [30],
    ),
    # disk faults
    (
        KhaosFaultName.latent_sector_error,
        "Disk read/write operations hit latent sector errors at the specified failure rate, causing I/O failures on affected storage regions.",
        [30],
    ),
]


KHAOS_FAULT_CONFIGS: dict[KhaosFaultName, KhaosFaultConfig] = {
    name: KhaosFaultConfig(name=name, description=desc, default_args=defaults)
    for name, desc, defaults in _FAULT_CONFIG_ENTRIES
}


# ---------------------------------------------------------------------------
# Compound hardware-failure problems
# ---------------------------------------------------------------------------
# A real hardware fault rarely manifests as a single failed syscall. A failing
# DRAM module rejects mmap *and* malloc; a faulty NIC drops sendto *and*
# recvfrom; a dying storage controller produces both read and write errors.
# KhaosCompoundFaultProblem injects several khaos faults onto the same node so
# the application sees the syscall-error signature of one underlying hardware
# component failing — and exposes a single hardware-level root_cause to the
# diagnosis judge instead of the per-syscall description.


class KhaosCompoundFaultProblem(Problem):
    """Inject multiple khaos faults onto a single node, with a hardware-failure
    narrative as the root cause."""

    def __init__(
        self,
        fault_specs: list[tuple[KhaosFaultName | str, list[int | str] | None]],
        root_cause: str,
        target_node: str | None = None,
    ):
        if not fault_specs:
            raise ValueError("KhaosCompoundFaultProblem requires at least one fault_spec")

        self.app = HotelReservation()
        super().__init__(app=self.app, namespace=self.app.namespace)
        self.kubectl = self.app.kubectl if hasattr(self.app, "kubectl") else None
        self.namespace = self.app.namespace
        self.injector = HWFaultInjector()
        self.target_node = target_node

        # Resolve fault names against the configs and apply default args when
        # the caller didn't override them.
        self.fault_specs: list[tuple[KhaosFaultName, list[int | str]]] = []
        for raw_name, raw_args in fault_specs:
            try:
                name = KhaosFaultName(raw_name)
            except Exception as e:
                raise ValueError(f"Unknown fault name '{raw_name}'") from e
            cfg = KHAOS_FAULT_CONFIGS[name]
            args = list(raw_args) if raw_args is not None else list(cfg.default_args)
            self.fault_specs.append((name, args))

        self.app.payload_script = (
            TARGET_MICROSERVICES / "hotelReservation/wrk2/scripts/hotel-reservation/mixed-workload_type_1.lua"
        )

        self._reinjection_monitors: list[_FaultReinjectionMonitor] = []

        self.root_cause = root_cause
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.mitigation_oracle = AlertOracle(problem=self)

        self.app.create_workload()

    def requires_khaos(self) -> bool:
        return True

    @mark_fault_injected
    def inject_fault(self):
        names = ", ".join(name.value for name, _ in self.fault_specs)
        print(f"== Compound Fault Injection: {names} ==")

        # First fault chooses the node (or honours the caller's preference).
        # Subsequent faults reuse that node so all faults compose on one host.
        first_name, first_args = self.fault_specs[0]
        self.target_node = self.injector.inject_node(
            self.namespace,
            first_name.value,
            self.target_node,
            params=first_args,
        )
        print(f"Injected {first_name.value} on node {self.target_node}")

        for name, args in self.fault_specs[1:]:
            self.injector.inject_node(
                self.namespace,
                name.value,
                self.target_node,
                params=args,
            )
            print(f"Injected {name.value} on node {self.target_node}")

        if self.target_node and any(name in _NEEDS_CACHE_DROP for name, _ in self.fault_specs):
            print("Dropping page caches to force disk reads through eBPF probes...")
            kernel_injector = KernelInjector(self.injector.kubectl)
            kernel_injector.drop_caches(self.target_node)

        if self.target_node:
            for name, args in self.fault_specs:
                monitor = _FaultReinjectionMonitor(
                    injector=self.injector,
                    namespace=self.namespace,
                    node=self.target_node,
                    fault_type=name.value,
                    params=args,
                )
                monitor.start()
                self._reinjection_monitors.append(monitor)
        print(f"Compound fault injection complete on node {self.target_node}\n")

    @mark_fault_injected
    def recover_fault(self):
        names = ", ".join(name.value for name, _ in self.fault_specs)
        print(f"== Compound Fault Recovery: {names} on node {self.target_node} ==")

        for monitor in self._reinjection_monitors:
            monitor.stop()
        self._reinjection_monitors = []

        if not self.target_node:
            print("[warn] No target node recorded; attempting best-effort recovery.")
            return

        # Recover in reverse order so that whichever fault was injected last
        # is the first to be torn down — minimises the window where a half-
        # recovered host can re-fault a still-bound probe.
        for name, _ in reversed(self.fault_specs):
            try:
                self.injector.recover_node(self.namespace, name.value, self.target_node)
            except Exception as exc:
                print(f"[recover] {name.value} on {self.target_node}: {exc}")
        print("Recovery requests sent.\n")


# Hardware-failure narratives. Each entry composes one or more khaos faults
# under a single hardware story so the LLM judge sees a hardware-level root
# cause instead of a syscall-level one.

_HW_NIC_PACKET_CORRUPTION = (
    "[fault_spec] component=node-nic; namespace=hotel-reservation || "
    "The host node's network interface is corrupting and dropping a substantial fraction of "
    "packets in both directions, consistent with a failing NIC, dirty fiber-optic transceiver, "
    "or top-of-rack switch port flapping. gRPC calls between hotel-reservation microservices "
    "experience high error rates and elevated tail latency from retransmissions; cross-pod "
    "communication on the affected node is degraded but not fully partitioned."
)

_HW_STORAGE_READ_FAILURE = (
    "[fault_spec] component=node-storage-controller; namespace=hotel-reservation || "
    "The storage controller / disk on the host node is returning hard read errors, consistent "
    "with a SATA controller failure, head-read-recovery exhaustion on a failing HDD, or an SSD "
    "exhausting its spare blocks. Pods on this node — especially MongoDB instances reading "
    "WiredTiger pages and Go services reading config files — surface I/O errors and crash on "
    "page faults backed by failing storage."
)

_HW_STORAGE_WRITE_FAILURE = (
    "[fault_spec] component=node-storage; namespace=hotel-reservation || "
    "Writes to the host node's persistent storage are failing, consistent with a write-head "
    "failure, firmware-bug write rejection, or full disk hardware fault. MongoDB cannot persist "
    "WiredTiger journal records or fsync the WAL, and write-heavy services on the node fail "
    "their durability paths and crash."
)

_HW_DRAM_MODULE_FAILURE = (
    "[fault_spec] component=node-dram; namespace=hotel-reservation || "
    "The host node's DRAM is degraded — consistent with a defective memory module surfacing "
    "uncorrectable ECC errors and the kernel offlining bad pages. Memory allocation paths "
    "(mmap of new mappings, heap-chunk allocation) on this node return ENOMEM. Go services "
    "cannot grow goroutine stacks; MongoDB cannot mmap WiredTiger storage files; pods on the "
    "node fail to allocate and crash or fail to start."
)

_HW_CPU_CLOCKSOURCE_FAILURE = (
    "[fault_spec] component=node-clocksource; namespace=hotel-reservation || "
    "The host node's clocksource is unreliable — consistent with TSC instability, hardware RTC "
    "failure, or cross-CPU clock desynchronisation. Time-reading syscalls (clock_gettime, "
    "gettimeofday) return errors, so gRPC deadlines and timeouts behave unpredictably, span "
    "metrics latency math is wrong, and dependent retry/backoff loops on this node misbehave."
)

_HW_MMU_PAGE_PROTECTION_FAILURE = (
    "[fault_spec] component=node-mmu; namespace=hotel-reservation || "
    "Page-protection operations on the host node are failing, consistent with MMU/TLB "
    "corruption (e.g., a cosmic-ray-induced bit flip in page-table state, or a defective CPU "
    "page-table walker). Calls into mprotect return EACCES across pods on this node. The Go "
    "runtime uses mprotect when growing goroutine stacks and managing the heap, so services "
    "fault on memory operations and the kernel sends SIGSEGV — pods crash and restart."
)

_HW_NETWORK_INTERFACE_LINK_DOWN = (
    "[fault_spec] component=node-nic; namespace=hotel-reservation || "
    "The host node's primary network interface has lost link — consistent with a fully-down "
    "NIC, an unplugged cable, or a switchport admin-down event. New socket creation and bind "
    "operations fail (ENETDOWN). Already-established gRPC connections initially survive, but "
    "any pod restart on this node fails to come back up because services cannot bind their "
    "listening ports, and any service trying to open new outbound connections is blocked."
)

_HW_DNS_RESOLVER_FAILURE = (
    "[fault_spec] component=node-network-resolver; namespace=hotel-reservation || "
    "Name resolution is failing on the host node, consistent with a node-local DNS resolver / "
    "stub-resolver hardware-path failure (e.g., a corrupted resolver cache, broken libnss "
    "shared memory, or the per-node DNS device path going read-error). getaddrinfo() returns "
    "errors for service-name lookups, breaking services that resolve dependencies at request "
    "time on this node."
)
