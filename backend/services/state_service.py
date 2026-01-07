from __future__ import annotations

from threading import Lock
from typing import Any, Dict, List, Optional

from backend.api.schemas import CubeFilter, SelectionPayload, StateContext, StateContextResponse


class StateService:
    """In-memory session context store for filters, selections, and chat history."""

    def __init__(self) -> None:
        self._store: Dict[str, Dict[str, Any]] = {}
        self._lock = Lock()

    def _ensure_session(self, session_id: str) -> Dict[str, Any]:
        with self._lock:
            return self._store.setdefault(
                session_id,
                {
                    "active_filters": [],
                    "last_selection": None,
                    "conversation": [],
                },
            )

    def get_context(self, session_id: str) -> StateContextResponse:
        session = self._ensure_session(session_id)
        return StateContextResponse(
            session_id=session_id,
            active_filters=list(session.get("active_filters", [])),
            last_selection=session.get("last_selection"),
            conversation=list(session.get("conversation", [])),
        )

    def update_context(
        self,
        session_id: str,
        active_filters: Optional[List[CubeFilter]] = None,
        last_selection: Optional[SelectionPayload] = None,
        conversation: Optional[List[Dict[str, Any]]] = None,
    ) -> StateContextResponse:
        session = self._ensure_session(session_id)
        with self._lock:
            if active_filters is not None:
                session["active_filters"] = active_filters
            if last_selection is not None:
                session["last_selection"] = last_selection
            if conversation is not None:
                session["conversation"] = conversation
        return self.get_context(session_id)

    def append_message(self, session_id: str, role: str, content: str) -> None:
        session = self._ensure_session(session_id)
        with self._lock:
            history = session.setdefault("conversation", [])
            history.append({"role": role, "content": content})

    def add_filter(self, session_id: str, cube_filter: CubeFilter) -> StateContextResponse:
        session = self._ensure_session(session_id)
        with self._lock:
            filters: List[CubeFilter] = session.setdefault("active_filters", [])
            filters.append(cube_filter)
        return self.get_context(session_id)

    def reset(self, session_id: str) -> StateContextResponse:
        with self._lock:
            self._store[session_id] = {
                "active_filters": [],
                "last_selection": None,
                "conversation": [],
            }
        return self.get_context(session_id)


state_service = StateService()

