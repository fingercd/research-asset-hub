"""Built-in adapter registry.

Importing this module performs only in-process class registration. External
plugins remain opt-in through ``discover_entry_points``.
"""

from __future__ import annotations

from .agent import AgentAdapter
from .base import AdapterRegistry
from .fine_tuning import FineTuningAdapter
from .generation import GenerationAdapter
from .generic import GenericManifestAdapter
from .infra import InfraAdapter
from .reinforcement_learning import ReinforcementLearningAdapter
from .world_model import WorldModelAdapter


def build_default_registry() -> AdapterRegistry:
    registry = AdapterRegistry()
    registry.register("generic_manifest", GenericManifestAdapter)
    registry.register("agent", AgentAdapter)
    registry.register("infra", InfraAdapter)
    registry.register("world_model", WorldModelAdapter)
    registry.register("reinforcement_learning", ReinforcementLearningAdapter)
    registry.register("fine_tuning", FineTuningAdapter)
    registry.register("generation", GenerationAdapter)
    return registry


DEFAULT_REGISTRY = build_default_registry()
