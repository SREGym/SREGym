"""Problem base class"""

from abc import ABC, abstractmethod


class Problem(ABC):
    def __init__(self, app, namespace: str):
        self.app = app
        self.namespace = namespace
        self.results = {}

        # Optional: attach oracles in subclass
        self.detection_oracle = None
        self.localization_oracle = None
        self.mitigation_oracle = None

    @abstractmethod
    def inject_fault(self):
        pass

    @abstractmethod
    def recover_fault(self):
        pass

    def start_workload(self):
        pass  # Optional, not all problems need traffic generation
