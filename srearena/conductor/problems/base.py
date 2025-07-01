"""Problem base class"""

from abc import ABC, abstractmethod


class Problem(ABC):
    def __init__(self, app, namespace: str):
        self.app = app
        self.namespace = namespace
        self.fault_injected = False
        self.results = {}
        self.faulty_service = None

        # Optional: attach oracles in subclass
        self.localization_oracle = None
        self.mitigation_oracle = None

    @abstractmethod
    def decide_targeted_service(self):
        pass

    @abstractmethod
    def inject_fault(self):
        pass

    @abstractmethod
    def recover_fault(self):
        pass
