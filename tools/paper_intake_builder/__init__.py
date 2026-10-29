"""Build deterministic paper records and a verified copy plan for the 2026-08-31 intake."""

from .builder import (
    BATCH_GROUP_COUNTS,
    BuildResult,
    apply_copy_plan,
    build_intake,
    stable_paper_id,
    write_outputs,
)

__all__ = [
    "BATCH_GROUP_COUNTS",
    "BuildResult",
    "apply_copy_plan",
    "build_intake",
    "stable_paper_id",
    "write_outputs",
]
