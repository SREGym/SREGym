from enum import StrEnum


class KhaosCapability(StrEnum):
    """Host capabilities required by Khaos-backed problems."""

    EBPF_SYSCALL = "ebpf-syscall"
    DM_FLAKEY = "dm-flakey"
    DM_FLAKEY_RANDOM_CORRUPTION = "dm-flakey-random-corruption"
