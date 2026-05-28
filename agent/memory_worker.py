"""Fire-and-forget memory extraction worker."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from agent.llm_types import LLMRequest
from memory.dedup import is_duplicate
from memory.store import MemoryStore

logger = logging.getLogger(__name__)


PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

MINIMAL_MEMORY_EXTRACTION_PROMPT = """提取值得长期记住的用户信息。
只输出 JSON:
{"memories":[{"content":"...", "category":"other", "importance":0.5, "keywords":""}]}"""


class MemoryWorker:
    """Async memory extraction worker.

    Usage:
        worker = MemoryWorker(store, model, llm_gateway=gateway)
        asyncio.create_task(worker.extract(user_msg, assistant_reply))
        # Returns immediately, extraction happens in background.
    """

    def __init__(
        self,
        store: MemoryStore,
        model: str,
        llm_gateway: Any,
        min_importance: float = 0.4,
        prompts_dir: str | Path | None = None,
    ):
        self.store = store
        self.llm_gateway = llm_gateway
        self.model = model
        self.min_importance = min_importance
        self.prompts_dir = Path(prompts_dir) if prompts_dir else PROMPTS_DIR
        self.extraction_prompt = self._load_prompt_template(
            "memory_extract.md",
            MINIMAL_MEMORY_EXTRACTION_PROMPT,
        )

    def _load_prompt_template(self, filename: str, fallback: str) -> str:
        path = self.prompts_dir / filename
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            logger.warning("Memory extraction prompt missing, using fallback: %s", path)
            return fallback

    async def extract(
        self,
        user_message: str,
        assistant_reply: str,
        existing_memories: list[dict[str, Any]] | None = None,
        bot_id: str = "",
    ):
        """Extract and save memories from a conversation turn.

        Fire-and-forget: caller should use asyncio.create_task().
        All errors are caught and logged, never raised to caller.
        """
        try:
            await self._do_extract(user_message, assistant_reply, existing_memories, bot_id)
        except Exception as e:
            logger.error(f"MemoryWorker extraction failed: {e}", exc_info=True)

    async def _do_extract(
        self,
        user_message: str,
        assistant_reply: str,
        existing_memories: list[dict[str, Any]] | None,
        bot_id: str = "",
    ):
        conversation = f"用户：{user_message}\n助手：{assistant_reply}"

        if existing_memories is None:
            existing_memories = await self.store.list_all()

        existing_text = ""
        if existing_memories:
            lines = []
            for m in existing_memories[:10]:
                lines.append(f"- [{m.get('category', 'other')}] {m.get('content', '')}")
            existing_text = "\n".join(lines)

        full_prompt = f"""{self.extraction_prompt}

## 已有记忆：
{existing_text}

## 对话：
{conversation}

提取值得保存的新记忆。如果已有记忆中有需要更新的，也写出来。"""

        try:
            messages = [
                {"role": "system", "content": "只输出JSON。"},
                {"role": "user", "content": full_prompt},
            ]
            llm_response = await self.llm_gateway.complete(
                LLMRequest(
                    purpose="memory_extract",
                    model=self.model,
                    messages=messages,
                    temperature=0.3,
                    max_tokens=1024,
                    response_mode="json_object",
                )
            )
            raw = llm_response.content

            from utils.json_parser import extract_json

            parsed = json.loads(extract_json(raw))
        except Exception as e:
            logger.warning(f"Memory extraction LLM call failed: {e}")
            return

        memories_to_save = parsed.get("memories", [])
        if not isinstance(memories_to_save, list):
            return

        saved_count = 0
        for mem in memories_to_save:
            if not isinstance(mem, dict):
                continue

            content = str(mem.get("content", "")).strip()
            if not content or len(content) < 4:
                continue

            category = mem.get("category", "other")
            if category not in [
                "preference", "identity", "relationship",
                "experience", "goal", "concern", "other",
            ]:
                category = "other"

            importance = float(mem.get("importance", 0.5))
            keywords = str(mem.get("keywords", ""))

            if importance < self.min_importance:
                continue

            existing_contents = [{"content": e.get("content", "")} for e in existing_memories]
            if is_duplicate(content, existing_contents):
                continue

            mem_id = await self.store.save_memory_bot(
                content=content,
                bot_id=bot_id,
                category=category,
                keywords=keywords,
                min_importance=self.min_importance,
            )
            if mem_id:
                saved_count += 1

        if saved_count > 0:
            logger.info(f"MemoryWorker saved {saved_count} new memories")
