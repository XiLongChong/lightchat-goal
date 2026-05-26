"""Fire-and-forget memory extraction worker — Safeguard #7.

Runs asynchronously after Chat Agent completes.
Does NOT block the user response — user gets their reply immediately.
Memory extraction happens in background with its own error handling.
"""

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from memory.store import MemoryStore
from memory.dedup import is_duplicate, compute_importance

logger = logging.getLogger(__name__)


PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

MINIMAL_MEMORY_EXTRACTION_PROMPT = """提取值得长期记住的用户信息。
只输出 JSON:
{"memories":[{"content":"...", "category":"other", "importance":0.5, "keywords":""}]}"""


class MemoryWorker:
    """Async memory extraction worker.

    Usage:
        worker = MemoryWorker(store, client, model)
        asyncio.create_task(worker.extract(user_msg, assistant_reply))
        # Returns immediately, extraction happens in background
    """

    def __init__(
        self,
        store: MemoryStore,
        client: Any,  # AsyncOpenAI
        model: str,
        min_importance: float = 0.4,
        prompts_dir: str | Path | None = None,
    ):
        self.store = store
        self.client = client
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
        All errors are caught and logged — never raises to caller.

        V2.1: bot_id scopes memories to a specific bot.
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
        # Build the extraction prompt
        conversation = f"用户：{user_message}\n助手：{assistant_reply}"

        if existing_memories is None:
            existing_memories = await self.store.list_all()

        existing_text = ""
        if existing_memories:
            lines = []
            for m in existing_memories[:10]:  # Only show top 10
                lines.append(f"- [{m.get('category', 'other')}] {m.get('content', '')}")
            existing_text = "\n".join(lines)

        full_prompt = f"""{self.extraction_prompt}

## 已有记忆：
{existing_text}

## 对话：
{conversation}

提取值得保存的新记忆。如果已有记忆中有需要更新的，也写出来。"""

        # Call LLM for extraction
        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": "只输出JSON。"},
                    {"role": "user", "content": full_prompt},
                ],
                temperature=0.3,
                max_tokens=1024,
            )

            raw = response.choices[0].message.content or ""

            # Parse the JSON
            from utils.json_parser import extract_json
            json_str = extract_json(raw)
            parsed = json.loads(json_str)
        except Exception as e:
            logger.warning(f"Memory extraction LLM call failed: {e}")
            return

        # Process each extracted memory
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

            # Safeguard #5: don't save low-importance memories
            if importance < self.min_importance:
                continue

            # Dedup check
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
