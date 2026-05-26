"""Bot profile management — V2.1 bot identity and prompt pack boundaries.

Per V2.1 design:
- Each bot has independent persona, relationship state, recap, and memory
- Prompt is split into small, explicit components
- Shared user facts live outside bot-specific state
- current_bot_id routes a single WeChat entry to one active bot
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Prompt pack component file names
PROMPT_PACK_COMPONENTS = [
    "base_protocol",        # Stable output format, tool rules, safety
    "bot_identity",         # Bot identity, voice, style
    "relationship_context", # Relationship atmosphere, boundaries
    "memory_policy",        # Memory search/lookup rules
    "tool_policy",          # Tool constraints
    "trigger_context",      # Turn source templates
]

# Default bot_id used when no bot is explicitly configured
DEFAULT_BOT_ID = "default_bot"


@dataclass
class BotProfile:
    """A single bot profile with its prompt pack and configuration."""

    bot_id: str
    display_name: str = ""
    identity_file: str = ""           # Path to bot identity .md
    enabled_tools: list[str] = field(default_factory=list)
    proactive_enabled: bool = True
    prompt_pack: dict[str, str] = field(default_factory=dict)
    # Backward compat
    persona_file: str = ""

    @property
    def name(self) -> str:
        return self.display_name or self.bot_id


@dataclass
class BotState:
    """Bot-specific runtime state — scoped by bot_id."""

    bot_id: str
    relationship_state: dict[str, Any] = field(default_factory=dict)
    current_recap: str = ""
    recent_turns: int = 0


class BotProfileManager:
    """Load, resolve, and manage bot profiles from config and prompt files.

    V2.1: keeps one WeChat entry and routes to one current_bot_id.
    The default_bot preserves all existing behavior.
    """

    def __init__(
        self,
        config: dict[str, Any],
        prompts_dir: str = "./prompts",
        persona_file: str = "",
    ):
        self._config = config
        self._prompts_dir = Path(prompts_dir)
        self._persona_file = persona_file
        self._bots: dict[str, BotProfile] = {}
        self._current_bot_id = DEFAULT_BOT_ID
        self._states: dict[str, BotState] = {}
        self._load()

    # ---- public API -------------------------------------------------

    @property
    def current_bot_id(self) -> str:
        return self._current_bot_id

    @current_bot_id.setter
    def current_bot_id(self, bot_id: str):
        if bot_id not in self._bots:
            raise KeyError(f"bot_id '{bot_id}' not found in configured bots")
        self._current_bot_id = bot_id
        logger.info(f"Switched current bot to '{bot_id}'")

    @property
    def current_bot(self) -> BotProfile:
        return self._bots[self._current_bot_id]

    @property
    def bots(self) -> dict[str, BotProfile]:
        return dict(self._bots)

    def get_bot(self, bot_id: str) -> BotProfile | None:
        return self._bots.get(bot_id)

    def get_state(self, bot_id: str) -> BotState:
        if bot_id not in self._states:
            self._states[bot_id] = BotState(bot_id=bot_id)
        return self._states[bot_id]

    def set_current_bot(self, bot_id: str):
        """Switch active bot. Creates state entry if needed."""
        if bot_id not in self._bots:
            raise KeyError(
                f"Bot '{bot_id}' is not configured. "
                f"Available: {list(self._bots.keys())}"
            )
        self._current_bot_id = bot_id
        if bot_id not in self._states:
            self._states[bot_id] = BotState(bot_id=bot_id)

    def list_bot_ids(self) -> list[str]:
        return list(self._bots.keys())

    # ---- prompt pack assembly ---------------------------------------

    def invalidate_prompt_pack(self, bot_id: str | None = None):
        """Clear cached prompt pack so next load re-reads .md files.

        Call after persona.md or any pack component file changes at runtime.
        Pass None to invalidate all bots.
        """
        if bot_id:
            bot = self._bots.get(bot_id)
            if bot:
                bot.prompt_pack = {}
        else:
            for bot in self._bots.values():
                bot.prompt_pack = {}

    def load_prompt_pack(self, bot_id: str) -> dict[str, str]:
        """Load or return cached prompt pack for a bot."""
        bot = self._bots.get(bot_id)
        if not bot:
            raise KeyError(f"Bot '{bot_id}' not found")

        if bot.prompt_pack:
            return dict(bot.prompt_pack)

        pack = self._assemble_prompt_pack(bot)
        bot.prompt_pack = pack
        return pack

    def build_system_prompt(
        self,
        bot_id: str,
        relationship: dict[str, Any],
        memories: list[dict[str, Any]],
        tool_names: list[str],
        trigger: str = "user",
        trigger_content: str = "",
        defaults: dict[str, Any] | None = None,
        history_summary: str = "",
        recap: str = "",
    ) -> str:
        """Assemble the full system prompt from the bot's prompt pack.

        V2.1: components are loaded from individual .md files and combined
        with runtime context (relationship, memories, time, etc.).
        """
        pack = self.load_prompt_pack(bot_id)
        defaults = defaults or {}

        # Current time
        from datetime import datetime
        now = datetime.now().strftime("%Y年%m月%d日 %H:%M 周%u")

        # Tool list
        tool_list = ", ".join(tool_names) if tool_names else "无"
        default_city = str(defaults.get("default_city", "") or "").strip()
        defaults_text = "无"
        if default_city:
            defaults_text = (
                f"用户默认天气城市：{default_city}。"
                + "用户只问'天气怎么样/今天天气怎么样'时，直接按这个城市查天气，不要先反问城市。"
            )

        # Trigger context — resolve template for current trigger
        trigger_text = self._resolve_trigger(pack, trigger, trigger_content)

        # Relationship context
        rel_text = self._resolve_relationship(pack, relationship)

        # Memory context
        mem_text = self._resolve_memory(pack, memories)

        # Tool context
        tool_text = self._resolve_tools(pack, tool_list)

        # Assemble sections
        sections = [
            pack.get("bot_identity", ""),
            pack.get("base_protocol", ""),
            f"\n## 本轮来源\n{trigger_text}",
            f"\n## 当前时间\n{now}",
        ]

        if defaults_text and defaults_text != "无":
            sections.append(f"\n## 默认事实\n{defaults_text}")

        sections.append(f"\n## 关系状态\n{rel_text}")

        if recap:
            sections.append(f"\n## 对话阶段摘要(recap)\n{recap.strip()[-800:]}")
        elif history_summary:
            sections.append(f"\n## 更早对话摘要\n{history_summary.strip()[-3000:]}")

        sections.append(f"\n## 记忆\n{mem_text}")
        sections.append(f"\n## 工具\n{tool_text}")

        return "\n\n".join(s.strip() for s in sections if s.strip())

    # ---- internal ---------------------------------------------------

    def _load(self):
        """Load bots from config, creating default_bot from legacy config."""
        bots_cfg = self._config.get("bots", {})

        # Load or create default_bot from legacy agent config
        default_bot_cfg = bots_cfg.get(DEFAULT_BOT_ID, {})
        if not isinstance(default_bot_cfg, dict):
            default_bot_cfg = {}
        default_bot = self._create_default_bot(default_bot_cfg)
        self._bots[DEFAULT_BOT_ID] = default_bot

        # Load additional bots (skip non-dict entries like _current_bot_id)
        for bot_id, bot_cfg in bots_cfg.items():
            if bot_id == DEFAULT_BOT_ID:
                continue
            if not isinstance(bot_cfg, dict):
                continue
            self._bots[bot_id] = self._parse_bot_config(bot_id, bot_cfg)

        # Resolve current_bot_id
        configured = str(bots_cfg.get("_current_bot_id") or "").strip()
        if configured and configured in self._bots:
            self._current_bot_id = configured
        else:
            self._current_bot_id = DEFAULT_BOT_ID

    def _create_default_bot(self, bot_cfg: dict[str, Any]) -> BotProfile:
        """Create default_bot profile from legacy agent config + any bot config overrides.

        V2.1.1: default bot identity is now sourced from persona.md.
        bot_identity_default.md is kept as a compatibility fallback only.
        """
        agent_cfg = self._config.get("agent", {})

        persona_file = str(
            bot_cfg.get("persona_file")
            or agent_cfg.get("persona_file")
            or "./prompts/persona.md"
        )

        # V2.1.1: Use persona.md as the primary identity source for default_bot.
        # bot_identity_default.md is a compatibility fallback.
        identity_file = str(
            bot_cfg.get("identity_file")
            or persona_file  # default to persona.md, not bot_identity_default.md
        )

        display_name = str(
            bot_cfg.get("display_name")
            or agent_cfg.get("name")
            or "bot"
        )

        enabled_tools = list(bot_cfg.get(
            "enabled_tools",
            [
                "wechat_reply", "schedule_wechat_message",
                "get_time", "get_weather",
                "search_memory", "read_persona_file",
                "propose_persona_change", "send_email",
            ],
        ))

        proactive = bool(bot_cfg.get("proactive_enabled", True))

        # Build initial prompt pack from files
        pack = self._assemble_prompt_pack_from_files(identity_file)

        return BotProfile(
            bot_id=DEFAULT_BOT_ID,
            display_name=display_name,
            identity_file=identity_file,
            persona_file=persona_file,
            enabled_tools=enabled_tools,
            proactive_enabled=proactive,
            prompt_pack=pack,
        )

    def _parse_bot_config(self, bot_id: str, bot_cfg: dict[str, Any]) -> BotProfile:
        """Parse a bot profile from config dictionary."""
        identity_file = str(
            bot_cfg.get("identity_file")
            or bot_cfg.get("persona_file")
            or f"./prompts/bot_identity_{bot_id}.md"
        )
        persona_file = str(bot_cfg.get("persona_file", identity_file))
        display_name = str(bot_cfg.get("display_name", bot_id))
        enabled_tools = list(bot_cfg.get("enabled_tools", [
            "wechat_reply", "schedule_wechat_message",
            "get_time", "get_weather",
            "search_memory", "read_persona_file",
            "propose_persona_change", "send_email",
        ]))
        proactive = bool(bot_cfg.get("proactive_enabled", True))

        pack = self._assemble_prompt_pack_from_files(identity_file)

        return BotProfile(
            bot_id=bot_id,
            display_name=display_name,
            identity_file=identity_file,
            persona_file=persona_file,
            enabled_tools=enabled_tools,
            proactive_enabled=proactive,
            prompt_pack=pack,
        )

    def _assemble_prompt_pack_from_files(self, identity_file: str) -> dict[str, str]:
        """Load individual prompt pack components from .md files.

        Falls back gracefully: missing files are skipped with empty strings.
        """
        pack: dict[str, str] = {}

        # Load bot_identity from the specified identity file
        pack["bot_identity"] = self._read_prompt_file(identity_file)

        # Load shared protocol components from prompts/ directory
        for component in PROMPT_PACK_COMPONENTS:
            if component == "bot_identity":
                continue  # Already loaded above
            path = self._prompts_dir / f"{component}.md"
            if path.exists():
                pack[component] = path.read_text(encoding="utf-8").strip()
            else:
                pack[component] = ""

        return pack

    def _assemble_prompt_pack(self, bot: BotProfile) -> dict[str, str]:
        """Reload prompt pack from files (called when files may have changed)."""
        return self._assemble_prompt_pack_from_files(bot.identity_file)

    @staticmethod
    def _read_prompt_file(path: str | Path) -> str:
        """Read a prompt file, return empty string if missing."""
        p = Path(path)
        root = Path.cwd().resolve()
        resolved = (root / p).resolve() if not p.is_absolute() else p.resolve()
        try:
            return resolved.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            logger.debug(f"Prompt file not found: {resolved}, using empty component")
            return ""
        except Exception:
            logger.warning(f"Error reading prompt file: {resolved}", exc_info=True)
            return ""

    def _resolve_trigger(
        self,
        pack: dict[str, str],
        trigger: str,
        trigger_content: str,
    ) -> str:
        """Resolve trigger context from template."""
        trigger_template = pack.get("trigger_context", "")
        if not trigger_template:
            return self._legacy_trigger_text(trigger)

        # Find the matching section
        marker = f"## trigger: {trigger}"
        lines = trigger_template.split("\n")
        result_lines: list[str] = []
        in_section = False
        for line in lines:
            if line.startswith("## trigger:") and line.strip() == marker:
                in_section = True
                continue
            if in_section and line.startswith("## trigger:"):
                break
            if in_section:
                result_lines.append(line)

        result = "\n".join(result_lines).strip()
        if result and "{trigger_content}" in result and trigger_content:
            result = result.replace("{trigger_content}", trigger_content)
        return result or self._legacy_trigger_text(trigger)

    @staticmethod
    def _legacy_trigger_text(trigger: str) -> str:
        """Fallback trigger text when trigger_context.md is missing."""
        if trigger == "proactive":
            return (
                "本轮是系统唤醒，不是用户发来的消息。"
                + "你是在主动开口，不能把唤醒指令当成用户说过的话。"
                + "不要说'你刚才说/你问我/你不是要'等把系统任务误认成用户消息的话。"
                + "主动消息要像微信里自然冒出来的一句，短，少解释。"
            )
        return (
            "本轮是用户发来的微信消息。"
            "历史消息里如果带有时间间隔，要先判断上一句话是不是已经过去一段时间。"
            "隔了十几分钟以上时，不要默认上一句话还在同一个瞬间里。"
        )

    @staticmethod
    def _resolve_relationship(
        pack: dict[str, str],
        relationship: dict[str, Any],
    ) -> str:
        """Inject relationship state into relationship_context template."""
        template = pack.get("relationship_context", "")
        if not template:
            return _legacy_rel_text(relationship)

        familiarity = relationship.get("familiarity", 5)
        atmosphere = relationship.get("current_atmosphere", "neutral")
        boundary = relationship.get("chat_boundary", "未设定")
        style = relationship.get("interaction_style", "casual")

        text = template.replace("{familiarity}", str(familiarity))
        text = text.replace("{atmosphere}", str(atmosphere))
        text = text.replace("{boundary}", str(boundary))
        text = text.replace("{interaction_style}", str(style))
        return text

    @staticmethod
    def _resolve_memory(
        pack: dict[str, str],
        memories: list[dict[str, Any]],
    ) -> str:
        """Build memory section from memory_policy + top memories."""
        policy = pack.get("memory_policy", "")
        if memories:
            lines = ["可搜索记忆线索："]
            for i, m in enumerate(memories, 1):
                cat = m.get("category", "other")
                content = m.get("content", "")
                lines.append(f"{i}. [{cat}] {content[:80]}")
            memory_list = "\n".join(lines)
        else:
            memory_list = "长期记忆不会默认展开。需要过去事实时先调用 search_memory。"

        if policy:
            return f"{policy}\n\n{memory_list}"
        return memory_list

    @staticmethod
    def _resolve_tools(pack: dict[str, str], tool_list: str) -> str:
        """Build tool section from tool_policy + actual available tools.

        The static tool_policy.md is used as a base, but the tool listing
        at the end is replaced with the actual effective tool list so that
        disabled tools don't appear in the prompt.
        """
        policy = pack.get("tool_policy", "")
        if policy:
            # Append effective tool list, overriding any static listing
            effective_part = f"\n\n## 当前可用工具清单\n{tool_list}"
            return policy + effective_part
        return f"可用工具：{tool_list}"


def _legacy_rel_text(relationship: dict[str, Any]) -> str:
    """Fallback relationship text when relationship_context.md is missing."""
    familiarity = relationship.get("familiarity", 5)
    atmosphere = relationship.get("current_atmosphere", "neutral")
    boundary = relationship.get("chat_boundary", "未设定")
    rel_text = f"熟悉度: {familiarity}/10 | 氛围: {atmosphere}"
    if boundary:
        rel_text += f"\n边界: {boundary}"
    return rel_text
