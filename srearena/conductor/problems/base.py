"""Problem base class"""

from abc import ABC, abstractmethod


class Problem(ABC):
    def __init__(self, app, namespace: str):
        self.app = app
        self.namespace = namespace
        self.fault_injected = False
        self.results = {}

        # Optional: attach oracles in subclass
        self.localization_oracle = None
        self.mitigation_oracle = None

    def requires_khaos(self) -> bool:
        """Override this method to return True if the problem requires Khaos for fault injection."""
        return False

    @abstractmethod
    def inject_fault(self):
        pass

    @abstractmethod
    def recover_fault(self):
        pass
