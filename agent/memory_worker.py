"""Fire-and-forget memory extraction worker — Safeguard #7.

Runs asynchronously after Chat Agent completes.
Does NOT block the user response — user gets their reply immediately.
Memory extraction happens in background with its own error handling.
"""

import asyncio
import json
import logging
from typing import Any

from memory.store import MemoryStore
from memory.dedup import is_duplicate, compute_importance

logger = logging.getLogger(__name__)


MEMORY_EXTRACTION_PROMPT = """从以下对话中提取值得长期记住的信息。只提取关于用户的、持久的、重要的信息。

## 该提取的（YES）：
- 用户明确表达的偏好："我不喜欢香菜"
- 关于用户的持久事实："我在杭州工作"、"我养了只猫叫毛球"
- 用户的重要计划或目标："下个月要搬家去上海"
- 用户的情感模式："聊到工作就会烦"
- 用户与你的关系变化："你今天第一次跟我开玩笑"

## 不该提取的（NO）：
- 一次性闲聊："今天天气不错"
- 临时状态："好累啊现在"
- 对时事的讨论
- 功能性的请求："帮我查下天气"
- 你（助手）说的关于自己的话

## 重要性评分：
- 0.0-0.3：琐碎细节
- 0.4-0.6：一般偏好或事实
- 0.7-0.9：重要个人信息、强烈偏好
- 1.0：至关重要（如健康信息、重大人生事件）

## 输出格式：
{"memories":[{"content":"...", "category":"...", "importance":0.X, "keywords":"kw1,kw2"}]}

分类只能是：preference, identity, relationship, experience, goal, concern, other"""


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
    ):
        self.store = store
        self.client = client
        self.model = model
        self.min_importance = min_importance

    async def extract(
        self,
        user_message: str,
        assistant_reply: str,
        existing_memories: list[dict[str, Any]] | None = None,
    ):
        """Extract and save memories from a conversation turn.

        Fire-and-forget: caller should use asyncio.create_task().
        All errors are caught and logged — never raises to caller.
        """
        try:
            await self._do_extract(user_message, assistant_reply, existing_memories)
        except Exception as e:
            logger.error(f"MemoryWorker extraction failed: {e}", exc_info=True)

    async def _do_extract(
        self,
        user_message: str,
        assistant_reply: str,
        existing_memories: list[dict[str, Any]] | None,
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

        full_prompt = f"""{MEMORY_EXTRACTION_PROMPT}

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
                    {"role": "system", "content": "你是一个记忆提取器。只输出JSON。"},
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

            mem_id = await self.store.save_memory(
                content=content,
                category=category,
                keywords=keywords,
                min_importance=self.min_importance,
            )
            if mem_id:
                saved_count += 1

        if saved_count > 0:
            logger.info(f"MemoryWorker saved {saved_count} new memories")
