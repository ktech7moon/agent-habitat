"""agent-habitat CheckpointSystem (Slice 5).

Human-in-the-loop approval primitive: a workflow can `request_checkpoint`
before a flagged action; an operator can `approve_checkpoint` (workflow
resumes — RUNNING) or `reject_checkpoint` (workflow cancelled — terminal).
Pending state lives additively on ADR-002's events table; no schema change.

The orchestrator (Phase 2) consults `is_workflow_paused_for_checkpoint`
before scheduling further work — Slice 5 just establishes the fact.
"""

from .system import (
    CHECKPOINT_APPROVED_MESSAGE_PREFIX,
    CHECKPOINT_REJECTED_MESSAGE_PREFIX,
    CHECKPOINT_REQUESTED_MESSAGE_PREFIX,
    Checkpoint,
    CheckpointError,
    CheckpointResolution,
    approve_checkpoint,
    get_checkpoint,
    is_workflow_paused_for_checkpoint,
    list_pending_checkpoints,
    reject_checkpoint,
    request_checkpoint,
)

__all__ = [
    "CHECKPOINT_APPROVED_MESSAGE_PREFIX",
    "CHECKPOINT_REJECTED_MESSAGE_PREFIX",
    "CHECKPOINT_REQUESTED_MESSAGE_PREFIX",
    "Checkpoint",
    "CheckpointError",
    "CheckpointResolution",
    "approve_checkpoint",
    "get_checkpoint",
    "is_workflow_paused_for_checkpoint",
    "list_pending_checkpoints",
    "reject_checkpoint",
    "request_checkpoint",
]
