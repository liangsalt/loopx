"""Small, host-independent vocabulary for the DV workflow.

These are engineering workflow stages, not UVM simulation phases.
"""

from enum import StrEnum


class Stage(StrEnum):
    IMPORT = "import"
    MODEL = "model"
    SPEC_CONTRACT = "spec-contract"
    CONTRACT_PLAN = "contract-plan"
    MICROARCHITECTURE = "microarchitecture"
    RTL = "rtl"
    RTL_CHECK = "rtl-check"
    PLAN = "plan"
    ARCHITECTURE = "architecture"
    IMPLEMENT = "implement"
    BUILD = "build"
    SMOKE = "smoke"
    REGRESS = "regress"
    ANALYZE = "analyze"
    WATCHDOG_REVIEW = "watchdog-review"


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    BLOCKED = "blocked"
    STALE = "stale"
    CANCELLED = "cancelled"


class StateError(RuntimeError):
    """A requested state transition is not permitted."""


class ConflictError(StateError):
    """Another attempt or a changed baseline superseded this operation."""


class ValidationError(StateError):
    """A completion has not established its required evidence."""


RUN_TRANSITIONS = {
    "queued": {"building", "running", "error", "cancelled"},
    "building": {"running", "passed", "failed", "error", "timeout", "cancelled"},
    "running": {"passed", "failed", "error", "timeout", "cancelled"},
    "passed": set(),
    "failed": set(),
    "error": set(),
    "timeout": set(),
    "cancelled": set(),
}
