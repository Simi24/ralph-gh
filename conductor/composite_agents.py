"""Composite Agents adapter: routes sessions to specific engine adapters based on role.

- Writing roles (implementer, fix, merge-fix, integration-fix, final-fix, exploration) -> writer_engine
- Review roles (ticket-gate, final-review) -> reviewer_engine
- Applies writer_model / reviewer_model defaults when the request does not specify one.
"""
from collections.abc import Mapping
from dataclasses import replace

from conductor.ports import Agents, SessionRequest, SessionResult

REVIEW_ROLES = frozenset({"ticket-gate", "final-review"})


class CompositeAgents:
    def __init__(
        self,
        engines: Mapping[str, Agents],
        writer_engine: str = "claude",
        reviewer_engine: str = "claude",
        writer_model: str | None = None,
        reviewer_model: str | None = None,
    ) -> None:
        self._engines = dict(engines)
        self._writer_engine = writer_engine
        self._reviewer_engine = reviewer_engine
        self._writer_model = writer_model
        self._reviewer_model = reviewer_model

    def start(self, request: SessionRequest) -> SessionResult:
        is_review = request.role in REVIEW_ROLES
        engine_name = self._reviewer_engine if is_review else self._writer_engine
        adapter = self._engines.get(engine_name)
        if adapter is None:
            available = ", ".join(sorted(self._engines))
            raise ValueError(f"unknown engine '{engine_name}' for role '{request.role}' (available: {available})")

        model = self._reviewer_model if is_review else self._writer_model
        if model and request.model is None and not request.agent:
            request = replace(request, model=model)

        return adapter.start(request)
