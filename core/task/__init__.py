"""Unified task execution engine.

Public API:
- ``core.task.types``     – TaskStatus, TaskResult, TaskEvent, RunPolicy, RetryPolicy
- ``core.task.task``      – Task (main think-act loop with retry & sub-task support)
- ``core.task.retry``     – retry_with_backoff, classify_error
- ``core.task.builder``   – build_run_policy (convenience for UI/CLI)
"""
