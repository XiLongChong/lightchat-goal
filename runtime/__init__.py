"""Runtime package — Phase 10 structural split.

Gradually extracts business orchestration from main.py into
focused runtime modules. Each runtime owns one domain boundary.
"""

from runtime.chat_runtime import ChatRuntime

__all__ = ["ChatRuntime"]
