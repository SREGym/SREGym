import logging
import threading
import time
from typing import Any, Dict, List, Optional

import yaml

from sregym.generators.noise.base import BaseNoise

# We will import implementations dynamically or register them

logger = logging.getLogger(__name__)


class NoiseManager:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(NoiseManager, cls).__new__(cls)
            cls._instance.noises: List[BaseNoise] = []
            cls._instance.config = {}
            cls._instance.running = False
            cls._instance._background_thread = None
            cls._instance.problem_context = {}
            cls._instance.current_stage = None
        return cls._instance

    def set_stage(self, stage: str):
        """
        Set the current problem stage (e.g. 'diagnosis', 'mitigation').
        """
        self.current_stage = stage
        logger.info(f"NoiseManager stage updated to: {stage}")

    def set_problem_context(self, context: Dict[str, Any]):
        """
        Set the problem context (namespace, services, etc.) for noises to use.
        """
        self.problem_context = context
        logger.info(f"NoiseManager context updated: {context.keys()}")
        # Update context for all existing noises
        for noise in self.noises:
            if hasattr(noise, "set_context"):
                noise.set_context(context)

    def load_config(self, config_path: Optional[str] = None):
        logger.info(f"Loading noise configuration from {config_path}")
        with open(config_path, "r") as f:
            self.config = yaml.safe_load(f)

        self.noises = []
        noise_configs = self.config.get("noises", [])

        from sregym.generators.noise.impl import get_noise_class

        for nc in noise_configs:
            if not nc.get("enabled", True):
                continue

            noise_type = nc.get("type")
            noise_class = get_noise_class(noise_type)
            if noise_class:
                try:
                    noise_instance = noise_class(nc.get("config", {}))
                    self.noises.append(noise_instance)
                    logger.info(f"Initialized noise: {noise_type}")
                except Exception as e:
                    logger.error(f"Failed to initialize noise {noise_type}: {e}")
            else:
                logger.warning(f"Unknown noise type: {noise_type}")

    def on_tool_call(self, tool_name: str, command: str, session_id: str):
        """
        Hook called when an agent executes a tool.
        """
        # Filter noises that are triggered by tool calls (Temporal injection)
        for noise in self.noises:
            if not noise.is_active(self.current_stage):
                continue

            # Check if this noise is configured to react to this tool/command
            # This logic depends on how we define the config for temporal injection
            # For now, we pass the context to the noise and let it decide
            try:
                noise.inject(
                    context={
                        "trigger": "tool_call",
                        "tool_name": tool_name,
                        "command": command,
                        "session_id": session_id,
                    }
                )
            except Exception as e:
                logger.error(f"Error injecting noise {noise}: {e}")

    def on_tool_result(self, tool_name: str, command: str, result: str, session_id: str) -> str:
        """
        Hook called after a tool execution to potentially modify the result.
        """
        for noise in self.noises:
            if not noise.is_active(self.current_stage):
                continue

            try:
                result = noise.modify_result(
                    context={
                        "trigger": "tool_result",
                        "tool_name": tool_name,
                        "command": command,
                        "session_id": session_id,
                    },
                    result=result,
                )
            except Exception as e:
                logger.error(f"Error modifying result in noise {noise}: {e}")
        return result

    def start_background_noises(self):
        """
        Start background loop for periodical noises.
        """
        if self.running:
            return
        self.running = True
        self._background_thread = threading.Thread(target=self._background_loop, daemon=True)
        self._background_thread.start()
        logger.info("Background noise generator started.")

    def stop(self):
        self.running = False
        if self._background_thread:
            self._background_thread.join(timeout=2)

        # Clean up all noises
        for noise in self.noises:
            try:
                noise.clean()
            except Exception as e:
                logger.error(f"Error cleaning up noise {noise}: {e}")

    def _background_loop(self):
        while self.running:
            for noise in self.noises:
                if not noise.is_active(self.current_stage):
                    continue

                try:
                    noise.inject(context={"trigger": "background"})
                except Exception as e:
                    logger.error(f"Error in background noise injection {noise}: {e}")
            time.sleep(1)  # Check every second, noises handle their own scheduling


# Global accessor
def get_noise_manager() -> NoiseManager:
    return NoiseManager()
