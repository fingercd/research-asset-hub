"""Read-only adapters for legacy research collections."""

from .agent import AgentAdapter
from .base import (
    AdapterRegistry,
    BaseAdapter,
    get_in,
    http_urls,
    stable_uuid7,
    string_list,
    string_or_none,
)
from .fine_tuning import FineTuningAdapter
from .generation import GenerationAdapter
from .generic import GenericManifestAdapter
from .infra import InfraAdapter
from .registry import DEFAULT_REGISTRY, build_default_registry
from .reinforcement_learning import ReinforcementLearningAdapter
from .types import AdapterError, AdapterErrorCode, AdapterPreview, ScannedRecord
from .world_model import WorldModelAdapter

__all__ = [
    "AdapterError",
    "AdapterErrorCode",
    "AdapterPreview",
    "AdapterRegistry",
    "AgentAdapter",
    "BaseAdapter",
    "DEFAULT_REGISTRY",
    "FineTuningAdapter",
    "GenerationAdapter",
    "GenericManifestAdapter",
    "InfraAdapter",
    "ReinforcementLearningAdapter",
    "ScannedRecord",
    "WorldModelAdapter",
    "build_default_registry",
    "get_in",
    "http_urls",
    "stable_uuid7",
    "string_list",
    "string_or_none",
]
