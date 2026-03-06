"""Problem base class"""

from abc import ABC, abstractmethod


class Problem(ABC):
    def __init__(self, app, namespace: str):
        self.app = app
        self.namespace = namespace
        self.fault_injected = False
        self.results = {}
        self.root_cause = None  # root cause of the problem in natural language

        # Optional: attach oracles in subclass
        self.diagnosis_oracle = None
        self.mitigation_oracle = None

    def requires_khaos(self) -> bool:
        """Override this method to return True if the problem requires Khaos for fault injection."""
        return False

    def setup_infrastructure(self):  # noqa: B027
        """Called before app deployment to set up any problem-specific infrastructure.

        Override this in subclasses that need to modify the cluster environment
        before the application is deployed (e.g., setting up dm-flakey devices
        so that PVs are created on the correct backing store).

        This is a no-op by default.
        """

    def teardown_infrastructure(self):  # noqa: B027
        """Called during cleanup to tear down problem-specific infrastructure.

        Override this in subclasses that set up infrastructure in setup_infrastructure().
        Also called as a fallback during fix_kubernetes() to clean up leftovers
        from previous problem runs.

        This is a no-op by default.
        """

    @abstractmethod
    def inject_fault(self):
        pass

    @abstractmethod
    def recover_fault(self):
        pass
