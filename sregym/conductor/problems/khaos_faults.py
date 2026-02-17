from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum

from pydantic import BaseModel, Field

from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_hw import HWFaultInjector
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

        self.root_cause = cfg.description

        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)

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

    @mark_fault_injected
    def recover_fault(self):
        print(f"== Fault Recovery: {self.fault_name.value} on node {self.target_node} ==")
        if self.target_node:
            self.injector.recover_node(self.namespace, self.fault_name.value, self.target_node)
        else:
            print("[warn] No target node recorded; attempting best-effort recovery.")
        print("Recovery request sent.\n")


_FAULT_CONFIG_ENTRIES: Sequence[tuple[KhaosFaultName, str, list[int | str]]] = [
    # kprobe faults
    (KhaosFaultName.read_error, "read() returns EIO, leading to application I/O failures.", []),
    (KhaosFaultName.pread_error, "pread64() returns EIO, breaking file reads.", []),
    (KhaosFaultName.write_error, "write() returns ENOSPC-like errors, simulating full disk.", []),
    (KhaosFaultName.pwrite_error, "pwrite64() fails as if the target storage is full.", []),
    (KhaosFaultName.fsync_error, "fsync() fails, so writes are not persisted.", []),
    (KhaosFaultName.open_error, "openat() is denied, preventing files from opening.", []),
    (KhaosFaultName.close_fail, "close() returns EBADF-like errors, leaving FDs open.", []),
    (KhaosFaultName.dup_fail, "dup() fails as if file descriptor limits are hit.", []),
    (KhaosFaultName.getrandom_fail, "getrandom() returns errors (e.g., EAGAIN), breaking randomness consumers.", []),
    (KhaosFaultName.gettimeofday_fail, "gettimeofday() returns errors, disrupting time reads.", []),
    (KhaosFaultName.ioctl_fail, "ioctl() returns errors (e.g., ENOTTY), blocking control calls.", []),
    (KhaosFaultName.cuda_malloc_fail, "ioctl()-based GPU alloc requests behave as ENOMEM.", []),
    (KhaosFaultName.getaddrinfo_fail, "recvfrom() path fails, emulating getaddrinfo resolution issues.", []),
    (KhaosFaultName.nanosleep_throttle, "nanosleep() errors cause sleeps to be throttled.", []),
    (KhaosFaultName.nanosleep_interrupt, "nanosleep() returns EINTR-like interruptions.", []),
    (KhaosFaultName.fork_fail, "fork() fails as under EAGAIN/ENOMEM pressure.", []),
    (KhaosFaultName.clock_drift, "clock_gettime() errors manifest as time drift symptoms.", []),
    (KhaosFaultName.setns_fail, "setns() fails, preventing namespace switches.", []),
    (KhaosFaultName.prlimit_fail, "prlimit64() errors prevent limit changes from applying.", []),
    (KhaosFaultName.socket_block, "socket() creation fails with generic errors.", []),
    (KhaosFaultName.mmap_fail, "mmap() returns ENOMEM, blocking new mappings.", []),
    (KhaosFaultName.mmap_oom, "mmap() behaves as OOM, rejecting new mappings.", []),
    (KhaosFaultName.brk_fail, "brk() cannot grow the heap, simulating ENOMEM.", []),
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
    (KhaosFaultName.oom_memchunk, "mmap() returns ENOMEM, simulating chunk allocation OOM.", []),
    (KhaosFaultName.oom_heapspace, "brk() returns ENOMEM, exhausting heap space.", []),
    (KhaosFaultName.oom_nonswap, "mlock() returns ENOMEM, preventing swap-backed growth.", []),
    (KhaosFaultName.hfrag_memchunk, "mmap() returns EAGAIN, emulating heavy fragmentation.", []),
    (KhaosFaultName.hfrag_heapspace, "brk() returns EAGAIN, emulating fragmented heap.", []),
    (KhaosFaultName.ptable_permit, "mlock() returns EPERM, blocking page table pinning.", []),
    (KhaosFaultName.stack_rndsegfault, "mprotect() returns EACCES, leading to stack faults.", []),
    (KhaosFaultName.thrash_swapon, "swapon() returns EINVAL/EPERM, preventing swap use.", []),
    (KhaosFaultName.thrash_swapoff, "swapoff() returns EPERM-like errors, blocking swap disable.", []),
    (KhaosFaultName.memleak_munmap, "munmap() returns EINVAL, leaking mappings.", []),
    # network packet loss
    (
        KhaosFaultName.packet_loss_sendto,
        "Outbound sendto() calls drop packets based on drop rate and return errno.",
        [30],
    ),
    (
        KhaosFaultName.packet_loss_recvfrom,
        "Inbound recvfrom() calls drop packets based on drop rate and return errno.",
        [30],
    ),
    # disk faults
    (
        KhaosFaultName.latent_sector_error,
        "Disk read/write operations hit latent sector errors at the specified failure rate, simulating bad disk sectors.",
        [30],
    ),
]


KHAOS_FAULT_CONFIGS: dict[KhaosFaultName, KhaosFaultConfig] = {
    name: KhaosFaultConfig(name=name, description=desc, default_args=defaults)
    for name, desc, defaults in _FAULT_CONFIG_ENTRIES
}
