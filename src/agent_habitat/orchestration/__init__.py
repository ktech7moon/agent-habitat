"""agent-habitat orchestration layer.

Owns `run_step()` (Phase 2 ADR-006 §2 — the per-step audit-lifecycle
context manager) and, as of Phase 2 Slice 6, the LangGraph crew
orchestrator. The latter lives in `crew_graph.py` + `crew_state.py`;
import directly from those submodules:

    from agent_habitat.orchestration.crew_graph import run_crew, resume_crew
    from agent_habitat.orchestration.crew_state import CrewState

`crew_graph` is NOT re-exported here on purpose. The agent modules
(`agents/researcher.py`, `…/drafter.py`, …) import `run_step` via
`from ..orchestration.run_step import run_step`, which forces Python
to evaluate this `__init__.py`. Re-exporting `crew_graph` here would
re-enter the agents package mid-init and form a circular import; we
keep this file narrow to break that cycle.
"""

from .run_step import StepRecorder, run_step

__all__ = ["StepRecorder", "run_step"]
