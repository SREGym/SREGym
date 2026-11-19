"""Problem base class"""

from abc import ABC, abstractmethod


class Problem(ABC):
    run_default_workload = True
    # False for a problem whose environment is not the test cluster. The
    # conductor then builds no cluster clients for it and deploys with the
    # application's own deploy().
    uses_cluster = True
    # HTTP(S) URLs on the host that the problem's environment serves to the
    # agent. main.py adds them to the filtered-egress allowlist, as it does
    # for --allow-agent-endpoint.
    agent_endpoints: tuple[str, ...] = ()
    # Seconds the filtered-egress proxy keeps a silent connection open. None
    # keeps mitmproxy's own default.
    egress_idle_timeout: int | None = None

    def __init__(self, app, namespace: str | None = None):
        self.app = app
        self.namespace = app.namespace if namespace is None else namespace
        self.fault_injected = False
        self.results = {}
        self.root_cause = None  # root cause of the problem in natural language

        # Seconds of steady-state traffic to run before fault injection. Override in subclass.
        self.baseline_duration_s: int = 0

        # Seconds to wait after fault injection for it to reach telemetry. Override in subclass.
        self.propagation_duration_s: int = 0

        # Optional: attach oracles in subclass
        self.diagnosis_oracle = None
        self.mitigation_oracle = None

    @classmethod
    def build_structured_root_cause(
        cls,
        *,
        component: str,
        namespace: str,
        description: str,
    ) -> str:
        """Return canonical structured root_cause text for judge-side parsing.

        Format:
        [fault_spec] component=<...>; namespace=<...> || <human-readable-description>
        """
        kv = [("component", component), ("namespace", namespace)]
        meta = "; ".join(f"{k}={str(v).strip()}" for k, v in kv)

        return f"[fault_spec] {meta} || {description.strip()}"

    @abstractmethod
    def inject_fault(self) -> bool | None:
        pass

    @abstractmethod
    def recover_fault(self) -> bool | None:
        pass
