"""In-memory registry for attached app agent sessions.

External agent sessions (e.g. a Claude Code session) can register
themselves via the REST API so they appear as contacts on the AsPocket
app.  This registry is purely in-memory — sessions are lost on restart.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone


def _utc_now_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class AppAgentSessionRegistry:
    """Thread/async-safe registry of attached agent sessions.

    Each session is a plain dict with the keys expected by the REST API
    and the invitation builder.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        # sessionId -> session dict
        self._sessions: dict[str, dict] = {}

    # ── public API ───────────────────────────────────────────────────

    async def register(
        self,
        *,
        session_id: str,
        remote_agent_id: str,
        display_name: str,
        description: str = "Attached agent session",
        callback_url: str | None = None,
        source_label: str | None = None,
        kind: str = "attached_session",
    ) -> dict:
        """Register or update an attached agent session.

        Returns the session dict.  Raises ``ValueError`` with code
        ``"remote_agent_id_conflict"`` when *remote_agent_id* is already
        claimed by a different *session_id*.

        *kind* selects the session target type:

        * ``"attached_session"`` (default) — Router forwards messages to
          ``callbackUrl`` via HTTP POST.
        * ``"claude_channel"`` — Router stores messages in an in-memory
          pending inbox; a channel server polls them.  ``callbackUrl``
          is ignored.
        """
        # Normalise kind: unknown values fall back to "attached_session".
        if kind != "claude_channel":
            kind = "attached_session"
        now = _utc_now_z()
        async with self._lock:
            existing = self._sessions.get(session_id)

            # Check remoteAgentId conflict
            for sid, sess in self._sessions.items():
                if sid != session_id and sess["remoteAgentId"] == remote_agent_id:
                    raise ValueError(
                        f"remoteAgentId {remote_agent_id!r} is already registered "
                        f"by session {sid!r}",
                    )

            if existing is not None:
                # Update existing
                existing["displayName"] = display_name
                existing["description"] = description
                existing["callbackUrl"] = callback_url
                existing["sourceLabel"] = source_label
                existing["kind"] = kind
                existing["status"] = "online"
                existing["lastSeenAt"] = now
            else:
                existing = {
                    "sessionId": session_id,
                    "remoteAgentId": remote_agent_id,
                    "displayName": display_name,
                    "description": description,
                    "sourceLabel": source_label,
                    "callbackUrl": callback_url,
                    "kind": kind,
                    "status": "online",
                    "createdAt": now,
                    "lastSeenAt": now,
                    "expiresAt": None,
                }
                self._sessions[session_id] = existing

            return dict(existing)

    async def heartbeat(self, session_id: str) -> dict | None:
        """Update lastSeenAt and set status to online.  Returns None if unknown."""
        now = _utc_now_z()
        async with self._lock:
            sess = self._sessions.get(session_id)
            if sess is None:
                return None
            sess["status"] = "online"
            sess["lastSeenAt"] = now
            return dict(sess)

    async def offline(self, session_id: str) -> dict | None:
        """Mark session offline.  Returns None if unknown."""
        now = _utc_now_z()
        async with self._lock:
            sess = self._sessions.get(session_id)
            if sess is None:
                return None
            sess["status"] = "offline"
            sess["lastSeenAt"] = now
            return dict(sess)

    async def get(self, session_id: str) -> dict | None:
        """Return a copy of the session or None."""
        async with self._lock:
            sess = self._sessions.get(session_id)
            return dict(sess) if sess is not None else None

    async def list_all(self) -> list[dict]:
        """Return copies of all registered sessions."""
        async with self._lock:
            return [dict(s) for s in self._sessions.values()]

    async def list_online(self) -> list[dict]:
        """Return copies of sessions with status 'online'."""
        async with self._lock:
            return [dict(s) for s in self._sessions.values() if s["status"] == "online"]

    def known_remote_agent_ids(self) -> set[str]:
        """Return all remoteAgentId values known to the registry.

        This is NOT async because the resolver needs a synchronous call.
        The snapshot may be slightly stale which is acceptable for the
        resolver's "is this id known?" check.
        """
        return {s["remoteAgentId"] for s in self._sessions.values()}

    def find_by_remote_agent_id(self, remote_agent_id: str) -> dict | None:
        """Synchronous lookup by remoteAgentId.  Returns a copy or None.

        NOT async — used by the synchronous ``resolve_app_remote_agent``
        helper.  The snapshot may be slightly stale.
        """
        for sess in self._sessions.values():
            if sess["remoteAgentId"] == remote_agent_id:
                return dict(sess)
        return None
