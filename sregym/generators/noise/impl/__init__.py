from typing import Type, Optional
from sregym.generators.noise.base import BaseNoise

# Registry for noise implementations
_NOISE_REGISTRY = {}

def register_noise(name: str):
    def decorator(cls: Type[BaseNoise]):
        _NOISE_REGISTRY[name] = cls
        return cls
    return decorator

def get_noise_class(name: str) -> Optional[Type[BaseNoise]]:
    return _NOISE_REGISTRY.get(name)

# Import implementations to ensure they are registered
from . import chaos_mesh
from . import kubectl_noise
from . import ghost_metrics
from . import jaeger_noise
from . import zombie_resources
from . import cicd_noise
from . import node_maintenance


