"""Hooks an agent registration attaches around fault injection.

An agents.yaml entry may name ``hooks: module:attribute``. The object may define
either or both of::

    def before_fault_injection(ctx: FaultContext) -> None
    def after_fault_injection(ctx: FaultContext) -> None

The first runs right before the fault is injected, the second after the
propagation wait and before the first stage opens. Each runs in the conductor's
process, in a worker thread, inside a ``hook:<name>`` phase. A hook must bound
its own waits and must not touch the conductor's state; it gets what it needs
in ``ctx``. Raising fails the attempt without the deploy retries main.py applies
to a failed start_problem.
"""

from dataclasses import dataclass
from importlib import import_module


@dataclass(frozen=True)
class FaultContext:
    problem_id: str
    namespace: str | None
    # Wall-clock time of injection. None for the before-hook.
    fault_injected_at: float | None = None


class AttemptHookError(RuntimeError):
    """A hook refused the attempt; redeploying would not change its answer."""


def load_hooks(spec: str | None):
    """Import the object an agents.yaml ``hooks: module:attribute`` entry names."""
    if not spec:
        return None
    module_name, sep, attribute = spec.partition(":")
    if not sep or not module_name or not attribute:
        raise ValueError(f"hooks must be 'module:attribute', got {spec!r}")
    return getattr(import_module(module_name), attribute)
