"""Idempotent source ingestion and reconciliation."""

from .dry_run import run_dry_run

__all__ = ["run_dry_run"]
