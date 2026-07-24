"""Encrypted local backup and isolated restore helpers."""

from .manager import BackupManager, BackupResult, default_backup_repository
from .rehearsal import (
    IndexRebuildContext,
    IndexRebuilder,
    MetadataIndexRebuilder,
    RestorePlan,
    RestorePorts,
    RestoreSafetyError,
    compare_database_snapshots,
    discover_restored_layout,
    render_restore_compose,
    run_restore_rehearsal,
    verify_restored_manifest,
)
from .workflow import create_platform_backup, wsl_path

__all__ = [
    "BackupManager",
    "BackupResult",
    "IndexRebuildContext",
    "IndexRebuilder",
    "MetadataIndexRebuilder",
    "RestorePlan",
    "RestorePorts",
    "RestoreSafetyError",
    "compare_database_snapshots",
    "create_platform_backup",
    "default_backup_repository",
    "discover_restored_layout",
    "render_restore_compose",
    "run_restore_rehearsal",
    "verify_restored_manifest",
    "wsl_path",
]
