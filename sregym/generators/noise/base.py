from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

class BaseNoise(ABC):
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.enabled = config.get("enabled", True)
        # Default to all stages if not specified
        self.stages = config.get("stages", None) 

    @abstractmethod
    def inject(self, context: Optional[Dict[str, Any]] = None):
        """
        Inject the noise.
        :param context: Contextual information (e.g., current tool call, session ID).
        """
        pass

    @abstractmethod
    def clean(self):
        """
        Clean up the noise (if applicable).
        """
        pass

    def set_context(self, context: Dict[str, Any]):
        """
        Update the noise with problem context.
        """
        self.context = context

    def modify_result(self, context: Dict[str, Any], result: str) -> str:
        """
        Modify the result of a tool call.
        """
        return result

    def is_active(self, current_stage: str = None) -> bool:
        if not self.enabled:
            return False
        
        # If stages are configured, check if current_stage is in the list
        if self.stages is not None and current_stage is not None:
            return current_stage in self.stages
            
        return True
