"""In-memory registry for attached app agent sessions.

External agent sessions (e.g. a Claude Code session) can register
themselves via the REST API so they appear as contacts on the AsPocket
app.  This registry is purely in-memory — sessions are lost on restart.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone


# Sessions that have not sent a heartbeat (or any other signal) within this
# window are treated as offline on read — the channel process may have exited
# without a chance to call /offline.  90s tolerates one missed 30s heartbeat.
APP_AGENT_SESSION_STALE_SECONDS = 90


def _utc_now_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso_z(value: str | None) -> datetime | None:
    """Parse an ISO-8601 UTC ``...Z`` timestamp.  Returns None on failure."""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except (ValueError, TypeError):
        return None


class AppAgentSessionRegistry:
    """Thread/async-safe registry of attached agent sessions.

    Each session is a plain dict with the keys expected by the REST API
    and the invitation builder.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        # sessionId -> session dict
        self._sessions: dict[str, dict] = {}

    # ── internal: stale detection ────────────────────────────────────

    def _effective_status(self, sess: dict, now: datetime) -> str:
        """Return the effective status, treating stale sessions as offline.

        A session is stale when ``now - lastSeenAt`` exceeds
        :data:`APP_AGENT_SESSION_STALE_SECONDS`.  Stale sessions are reported
        as ``"offline"`` regardless of stored status; a non-stale session
        keeps its stored status.  This is the lazy offline detection — no
        background task is required, every read re-evaluates freshness.
        """
        status = sess.get("status") or "offline"
        if status != "online":
            return status
        last = _parse_iso_z(sess.get("lastSeenAt"))
        if last is None:
            return "offline"
        if (now - last).total_seconds() > APP_AGENT_SESSION_STALE_SECONDS:
            return "offline"
        return "online"

    def effective_status(self, sess: dict) -> str:
        """Synchronous effective-status helper for snapshot readers.

        Used by callers that read ``_sessions`` directly (e.g. the
        invitation builder) so stale sessions are excluded without an
        async registry call.
        """
        return self._effective_status(sess, datetime.now(timezone.utc))

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
        """Return a copy of the session or None.

        The returned ``status`` is the *effective* status: stale sessions
        (no heartbeat within the TTL) are reported as ``"offline"``.
        """
        now = datetime.now(timezone.utc)
        async with self._lock:
            sess = self._sessions.get(session_id)
            if sess is None:
                return None
            return dict(sess, status=self._effective_status(sess, now))

    async def list_all(self) -> list[dict]:
        """Return copies of all registered sessions with effective status."""
        now = datetime.now(timezone.utc)
        async with self._lock:
            return [
                dict(s, status=self._effective_status(s, now))
                for s in self._sessions.values()
            ]

    async def list_online(self) -> list[dict]:
        """Return copies of sessions whose effective status is 'online'."""
        now = datetime.now(timezone.utc)
        async with self._lock:
            return [
                dict(s, status=self._effective_status(s, now))
                for s in self._sessions.values()
                if self._effective_status(s, now) == "online"
            ]

    def known_remote_agent_ids(self) -> set[str]:
        """Return all remoteAgentId values known to the registry.

        This is NOT async because the resolver needs a synchronous call.
        Stale/offline sessions remain known — only their *status* flips,
        not their identity, so ``resolve_app_remote_agent`` never raises
        ``400 unknown`` for a session that was once registered.
        """
        return {s["remoteAgentId"] for s in self._sessions.values()}

    def find_by_remote_agent_id(self, remote_agent_id: str) -> dict | None:
        """Synchronous lookup by remoteAgentId.  Returns a copy or None.

        NOT async — used by the synchronous ``resolve_app_remote_agent``
        helper.  The returned ``status`` is the effective status, so stale
        sessions resolve as ``"offline"`` while remaining known.
        """
        now = datetime.now(timezone.utc)
        for sess in self._sessions.values():
            if sess["remoteAgentId"] == remote_agent_id:
                return dict(sess, status=self._effective_status(sess, now))
        return None
