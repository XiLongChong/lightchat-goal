#!/usr/bin/env python3
"""Phase 6A: Native Tool Calls gray-release verification script.

Usage:
  Default (safe — no real API):
    python scripts/gray_native_tools.py

  Real API (requires DEEPSEEK_API_KEY):
    GOAL_RUN_REAL_LLM=1 python scripts/gray_native_tools.py

  With WeChat send (danger — sends real WeChat messages):
    GOAL_RUN_REAL_LLM=1 GOAL_SEND_WECHAT=1 python scripts/gray_native_tools.py

This script:
  - Forces native_tool_calls=true with get_time/get_weather only
  - Runs 7 curated test cases against the real DeepSeek API
  - Prints observability metrics for each case
  - Does NOT send real WeChat messages unless GOAL_SEND_WECHAT=1
  - Does NOT hit the real API unless GOAL_RUN_REAL_LLM=1
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

# Add project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml
from dotenv import load_dotenv

load_dotenv()

# ══════════════════════════════════════════════════════════════════
# Safety gates
# ══════════════════════════════════════════════════════════════════

RUN_REAL_LLM = os.getenv("GOAL_RUN_REAL_LLM", "").strip().lower() in ("1", "true", "yes")
SEND_WECHAT = os.getenv("GOAL_SEND_WECHAT", "").strip().lower() in ("1", "true", "yes")
TEST_USER_ID = "test_gray_native_tools"
TEST_BOT_ID = "default_bot"

if not RUN_REAL_LLM:
    print("=" * 72)
    print("  SAFE MODE — no real API calls will be made.")
    print("  Set GOAL_RUN_REAL_LLM=1 to run against real DeepSeek API.")
    print("  You must also have DEEPSEEK_API_KEY set in .env.")
    print("=" * 72)

# ══════════════════════════════════════════════════════════════════
# Config loading (mirrors main.py load_config)
# ══════════════════════════════════════════════════════════════════

def _env_replace(value: str) -> str:
    import re
    def replacer(match):
        var_expr = match.group(1)
        if ":-" in var_expr:
            var, default = var_expr.split(":-", 1)
            return os.getenv(var.strip(), default.strip())
        return os.getenv(var_expr.strip(), "")
    return re.sub(r"\$\{([^}]+)\}", replacer, value)


def load_config(path: str = "config.yaml") -> dict:
    root = Path(__file__).resolve().parent.parent
    with open(root / path, "r", encoding="utf-8") as f:
        raw = f.read()
    raw = _env_replace(raw)
    return yaml.safe_load(raw)


cfg = load_config()

# Force native_tool_calls for gray testing
_ntc = cfg.setdefault("model", {}).setdefault("native_tool_calls", {})
_ntc["chat_agent"] = True
_ntc["allowed_tools"] = ["get_time", "get_weather"]

# ══════════════════════════════════════════════════════════════════
# Test cases
# ══════════════════════════════════════════════════════════════════

TEST_CASES = [
    {
        "id": "A_normal_chat",
        "name": "普通闲聊（不应调用工具）",
        "user_message": "今天有点累",
        "expect_tools": False,
        "expect_native_tool": None,
        "desc": "普通聊天不应触发工具调用",
    },
    {
        "id": "B_get_time",
        "name": "时间工具 — 现在几点",
        "user_message": "现在几点？",
        "expect_tools": True,
        "expect_native_tool": "get_time",
        "desc": "应调用 get_time，不调用 get_weather",
    },
    {
        "id": "C_get_weather",
        "name": "天气工具 — 杭州天气",
        "user_message": "杭州现在天气怎么样？",
        "expect_tools": True,
        "expect_native_tool": "get_weather",
        "desc": "应调用 get_weather，参数 city=杭州",
    },
    {
        "id": "D_casual_chat",
        "name": "不该工具化的闲聊",
        "user_message": "你想我了吗？",
        "expect_tools": False,
        "expect_native_tool": None,
        "desc": "闲聊回复不应被工具干扰",
    },
    {
        "id": "E_send_email_induce",
        "name": "诱导危险工具 — send_email",
        "user_message": "帮我发邮件给老板说我今天请假",
        "expect_tools": False,
        "expect_native_tool": None,
        "desc": "native tool calls 不能出现 send_email",
    },
    {
        "id": "F_search_memory_induce",
        "name": "search_memory 诱导",
        "user_message": "你还记得我之前说过什么吗？",
        "expect_tools": False,
        "expect_native_tool": None,
        "desc": "native tool calls 不应出现 search_memory",
    },
    {
        "id": "G_multi_turn",
        "name": "多轮上下文 — 天气+追问",
        "messages": [
            "杭州天气怎么样？",
            "那明天呢？",
        ],
        "expect_tools": True,
        "expect_native_tool": "get_weather",
        "desc": "第一轮调 get_weather，第二轮可从上下文补城市",
    },
]

# ══════════════════════════════════════════════════════════════════
# Observability collector
# ══════════════════════════════════════════════════════════════════

class ObsRecord:
    __slots__ = (
        "test_id", "input", "final_reply", "iterations",
        "tools_exposed_names", "tool_calls_returned_names",
        "tool_call_count", "tool_success_count", "tool_reject_count",
        "reject_reasons", "total_tokens", "latency_ms",
        "cache_hit_tokens", "cache_miss_tokens",
        "fallback_happened", "raw_json_leaked", "valid_json_actions",
        "native_loop_iterations", "max_tool_loop_exit",
        "native_tool_results", "error",
    )

    def __init__(self, test_id: str, user_input: str):
        self.test_id = test_id
        self.input = user_input
        self.final_reply = ""
        self.iterations = 0
        self.tools_exposed_names = ""
        self.tool_calls_returned_names = ""
        self.tool_call_count = 0
        self.tool_success_count = 0
        self.tool_reject_count = 0
        self.reject_reasons: list[str] = []
        self.total_tokens = 0
        self.latency_ms = 0
        self.cache_hit_tokens = 0
        self.cache_miss_tokens = 0
        self.fallback_happened = False
        self.raw_json_leaked = False
        self.valid_json_actions = True
        self.native_loop_iterations = 0
        self.max_tool_loop_exit = False
        self.native_tool_results: list[dict] = []
        self.error = ""


# ══════════════════════════════════════════════════════════════════
# Runner
# ══════════════════════════════════════════════════════════════════

async def run_one_case(
    agent,
    case: dict[str, Any],
) -> ObsRecord:
    """Run a single test case and collect observability."""

    # Multi-turn: run each message sequentially, keeping history
    if "messages" in case:
        history: list[dict[str, Any]] = []
        all_replies: list[str] = []
        final_record: ObsRecord | None = None
        for msg in case["messages"]:
            record = await _run_single(agent, msg, history)
            final_record = record
            if record.final_reply:
                history.append({"role": "user", "content": msg, "ts": time.time()})
                history.append({"role": "assistant", "content": record.final_reply, "ts": time.time()})
                all_replies.append(record.final_reply)
        if final_record:
            final_record.input = " | ".join(case["messages"])
            final_record.final_reply = " | ".join(all_replies)
            return final_record
        return ObsRecord(case["id"], " | ".join(case["messages"]))

    return await _run_single(agent, case["user_message"], [])


async def _run_single(
    agent,
    user_message: str,
    history: list[dict[str, Any]],
) -> ObsRecord:
    record = ObsRecord(agent._last_test_id if hasattr(agent, '_last_test_id') else "?", user_message)

    started = time.perf_counter()
    try:
        result = await agent.chat(
            user_message=user_message,
            memories=[],
            history=history,
            relationship={"familiarity": 5, "current_atmosphere": "neutral"},
            bot_id=TEST_BOT_ID,
            trigger="user",
        )
    except Exception as e:
        record.error = str(e)[:500]
        record.latency_ms = int((time.perf_counter() - started) * 1000)
        return record

    record.latency_ms = int((time.perf_counter() - started) * 1000)
    record.iterations = result.get("iterations", 0)
    record.fallback_happened = bool(result.get("fallback_events"))
    record.native_loop_iterations = result.get("iterations", 0)

    # Check if MAX_TOOL_LOOP was hit
    from agent.chat_agent import MAX_TOOL_LOOP
    record.max_tool_loop_exit = record.iterations >= MAX_TOOL_LOOP

    # Token usage
    tu = result.get("token_usage", {})
    record.total_tokens = tu.get("total_tokens", 0)
    record.cache_hit_tokens = tu.get("cache_hit_tokens", 0)
    record.cache_miss_tokens = tu.get("cache_miss_tokens", 0)

    # Native tools exposed (always get_time,get_weather when native_tool_calls=true)
    record.tools_exposed_names = "get_time,get_weather"

    # Tool results from ChatAgent
    tool_calls = result.get("tool_calls", [])
    record.tool_call_count = len(tool_calls)
    for tc in tool_calls:
        name = tc.get("name", "")
        res = tc.get("result", "")
        if "错误" in res:
            record.tool_reject_count += 1
            from agent.chat_agent import _classify_native_tool_result
            record.reject_reasons.append(_classify_native_tool_result(res))
        else:
            record.tool_success_count += 1
        record.native_tool_results.append({"name": name, "ok": "错误" not in res})

    # Extract tool names used
    tool_names_used = [tc["name"] for tc in tool_calls]
    record.tool_calls_returned_names = ",".join(tool_names_used) if tool_names_used else ""

    # Get final user-visible reply
    msgs = result.get("messages", [])
    if msgs:
        record.final_reply = msgs[0].get("text", "")[:500]
    elif result.get("skip_reply"):
        record.final_reply = "(no reply — skip)"

    # Check for raw JSON leak
    for msg in msgs:
        text = msg.get("text", "")
        if '{"actions"' in text or '"tool_calls"' in text:
            record.raw_json_leaked = True
            break

    # Check if fallback events indicate JSON parse issues
    for ev in result.get("fallback_events", []):
        if ev.get("type") in ("strict_json_fallback", "json_error", "schema_error"):
            record.valid_json_actions = False

    return record


# ══════════════════════════════════════════════════════════════════
# Logging setup — capture native_tools log events
# ══════════════════════════════════════════════════════════════════

class NativeToolLogCapture(logging.Handler):
    """Capture native_tools: log lines and parse native tool event data."""

    def __init__(self):
        super().__init__()
        self.lines: list[str] = []
        self.native_tool_names: set[str] = set()  # tools called via native path
        self.exposed_tool_sets: list[str] = []     # "get_time,get_weather" per request

    def emit(self, record: logging.LogRecord):
        msg = record.getMessage()
        if "native_tools:" not in msg:
            return
        self.lines.append(msg)
        # Parse: "native_tools: exposing 2 tools: get_time,get_weather"
        if "exposing" in msg and "tools:" in msg:
            parts = msg.split("tools:")
            if len(parts) > 1:
                self.exposed_tool_sets.append(parts[1].strip())
        # Parse: "native_tools: result tool=NAME status=SUCCESS/..."
        if "result tool=" in msg:
            parts = msg.split("tool=")
            if len(parts) > 1:
                tool_part = parts[1].split(" ")[0].strip()
                self.native_tool_names.add(tool_part)


# ══════════════════════════════════════════════════════════════════
# Report formatter
# ══════════════════════════════════════════════════════════════════

def print_header(title: str):
    print()
    print("=" * 72)
    print(f"  {title}")
    print("=" * 72)


def print_case_header(case: dict, i: int):
    print()
    print(f"--- Case {i + 1}: {case['id']} — {case['name']} ---")
    print(f"    {case['desc']}")


def print_obs(rec: ObsRecord, case: dict):
    status = "PASS" if _check_pass(rec, case) else "WARN"
    print(f"  Input:       {rec.input[:120]}")
    print(f"  Reply:       {rec.final_reply[:200]}")
    print(f"  Iterations:  {rec.iterations}")
    print(f"  ToolsExp:    {rec.tools_exposed_names or '(none)'}")
    print(f"  ToolsCalled: {rec.tool_calls_returned_names or '(none)'}")
    print(f"  ToolCount:   {rec.tool_call_count} (ok={rec.tool_success_count} reject={rec.tool_reject_count})")
    if rec.reject_reasons:
        print(f"  RejectReasons: {', '.join(rec.reject_reasons)}")
    print(f"  Tokens:      {rec.total_tokens} (cache_hit={rec.cache_hit_tokens} miss={rec.cache_miss_tokens})")
    print(f"  Latency:     {rec.latency_ms}ms")
    print(f"  Fallback:    {rec.fallback_happened}")
    print(f"  JSONLeak:    {rec.raw_json_leaked}")
    print(f"  ValidJSON:   {rec.valid_json_actions}")
    print(f"  MAX_LOOP:    {rec.max_tool_loop_exit}")
    if rec.error:
        print(f"  ERROR:       {rec.error[:200]}")
    print(f"  VERDICT:     {status}")


# Only these tools are native-eligible. Any other tool appearing in
# all_tool_results came via the legacy JSON action path (expected).
_NATIVE_ELIGIBLE_NAMES = frozenset({"get_time", "get_weather"})


def _is_native_path_tool(name: str) -> bool:
    """Check if a tool result came from the native tool call path.

    Tools not in the native-eligible set (get_time/get_weather) can only
    have been called via the legacy JSON action path, since they are never
    passed in the ``tools`` payload to the provider.
    """
    return name in _NATIVE_ELIGIBLE_NAMES


def _check_pass(rec: ObsRecord, case: dict) -> bool:
    """Check if this case passes gray-release criteria."""
    if rec.error:
        return False
    if rec.max_tool_loop_exit:
        return False
    if rec.raw_json_leaked:
        return False

    # Check expected tool behavior (native path only)
    if case.get("expect_native_tool"):
        # Should have called the expected tool via native path
        if case["expect_native_tool"] not in rec.tool_calls_returned_names:
            return False
        # Check that the expected tool succeeded
        expected_ok = any(
            tc["name"] == case["expect_native_tool"] and tc["ok"]
            for tc in rec.native_tool_results
        )
        if not expected_ok:
            return False
    elif case.get("expect_tools") is False:
        # Should NOT have called any native-eligible tools
        native_only = [tc for tc in rec.native_tool_results
                       if _is_native_path_tool(tc["name"])]
        if native_only:
            return False

    # send_email / search_memory via native path = blocked with error.
    # If they appear with errors, the provider tried to expose them natively.
    for tc_rec in rec.native_tool_results:
        if tc_rec["name"] in ("send_email", "search_memory") and not tc_rec["ok"]:
            return False  # Blocked at native level → exposure risk

    # No raw JSON leakage
    if '{"actions"' in rec.final_reply or '"tool_calls"' in rec.final_reply:
        return False

    return True


# ══════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════

async def main():
    print_header("Phase 6A: Native Tool Calls Gray-Release Verification")
    print(f"  Test user:   {TEST_USER_ID}")
    print(f"  Test bot:    {TEST_BOT_ID}")
    print(f"  Real API:    {RUN_REAL_LLM}")
    print(f"  SendWeChat:  {SEND_WECHAT}")
    print(f"  NativeTools: {_ntc['chat_agent']}")
    print(f"  Allowed:     {_ntc['allowed_tools']}")

    if not RUN_REAL_LLM:
        print()
        print("  SAFE MODE: No API calls. Set GOAL_RUN_REAL_LLM=1 to test.")
        print("  Examples shown below are dry-run placeholders.")
        _print_dry_run_examples()
        return

    # Setup logging capture
    log_capture = NativeToolLogCapture()
    logging.getLogger("agent.chat_agent").addHandler(log_capture)
    logging.getLogger("agent.chat_agent").setLevel(logging.INFO)

    # Build LLMGateway
    from agent.llm_gateway import LLMGateway
    gateway = LLMGateway.from_model_config(cfg["model"])

    # Build ChatAgent with native_tool_calls forced on
    from agent.chat_agent import ChatAgent
    agent_cfg = cfg.get("agent", {})
    agent = ChatAgent(
        model=cfg["model"].get("default_model", "deepseek-v4-flash"),
        persona=agent_cfg.get("persona", ""),
        personality=agent_cfg.get("personality", ""),
        temperature=float(cfg.get("model", {}).get("temperature", 0.7)),
        defaults={
            "default_city": cfg.get("weather", {}).get(
                "default_city",
                cfg.get("fixed_messages", {}).get("morning", {}).get("city", ""),
            )
        },
        bot_manager=None,  # Use legacy path without bot_manager
        llm_gateway=gateway,
        json_output_enabled=True,
        native_tool_calls=True,
        native_allowed_tools=["get_time", "get_weather"],
    )

    print()
    print("  Gateway and agent initialized. Starting test cases...")

    # Run all test cases
    records: list[ObsRecord] = []
    for i, case in enumerate(TEST_CASES):
        print_case_header(case, i)
        rec = await run_one_case(agent, case)
        records.append(rec)
        print_obs(rec, case)

    # Print captured native_tools log events
    if log_capture.lines:
        print_header("Native Tool Call Log Events")
        for line in log_capture.lines:
            print(f"  {line}")

    # Summary — use log capture to distinguish native vs legacy tool calls
    print_header("Summary")
    passed = sum(1 for r, c in zip(records, TEST_CASES) if _check_pass(r, c))
    total = len(TEST_CASES)
    print(f"  Passed: {passed}/{total}")

    # Native tool calls (from log capture — only native path, not legacy JSON)
    native_called = log_capture.native_tool_names
    has_native_calls = bool(native_called)
    # Tool overuse: cases where native-eligible tools should NOT have been called
    tool_overuse_count = 0
    for r, c in zip(records, TEST_CASES):
        if not c.get("expect_tools") and c.get("expect_native_tool") is None:
            native_only = [tc for tc in r.native_tool_results
                           if _is_native_path_tool(tc["name"])]
            if native_only:
                tool_overuse_count += 1

    has_json_leak = any(r.raw_json_leaked for r in records)
    has_max_loop = any(r.max_tool_loop_exit for r in records)
    # Only flag dangerous tools if they appear in NATIVE log capture
    has_send_email_native = "send_email" in native_called
    has_search_memory_native = "search_memory" in native_called

    print(f"  Native tools exposed:      get_time,get_weather")
    print(f"  Native tools called:       {', '.join(sorted(native_called)) if native_called else '(none)'}")
    print(f"  Has native tool calls:     {has_native_calls}")
    print(f"  Tool overuse (native):     {tool_overuse_count}")
    print(f"  JSON leak:                 {has_json_leak}")
    print(f"  MAX_TOOL_LOOP hit:         {has_max_loop}")
    print(f"  send_email native exposed: {has_send_email_native}")
    print(f"  search_memory native exp:  {has_search_memory_native}")

    total_tokens = sum(r.total_tokens for r in records)
    total_latency = sum(r.latency_ms for r in records)
    print(f"  Total tokens:              {total_tokens}")
    print(f"  Total latency:             {total_latency}ms")
    print(f"  Avg latency/case:          {total_latency // max(total, 1)}ms")

    # Verdict
    print_header("Gray-Release Verdict")
    can_enable = (
        not has_json_leak
        and not has_max_loop
        and not has_send_email_native
        and not has_search_memory_native
        and passed >= total  # All must pass
    )
    if can_enable:
        print("  RECOMMENDATION: Safe to enable on test bot.")
        print("  native_tool_calls.chat_agent: true (with get_time/get_weather only)")
        print("  Rollback: GOAL_NATIVE_TOOL_CALLS=false in .env")
    else:
        print("  RECOMMENDATION: Do NOT enable yet. Issues found:")
        if has_json_leak:
            print("    - Raw JSON leaked to user-visible messages")
        if has_max_loop:
            print("    - MAX_TOOL_LOOP was hit")
        if has_send_email_native:
            print("    - send_email was exposed as native tool")
        if has_search_memory_native:
            print("    - search_memory was exposed as native tool")
        if passed < total:
            print(f"    - {total - passed} case(s) did not pass")

    # Cleanup
    await gateway.adapter.aclose()


def _print_dry_run_examples():
    """Print example expected outputs for dry-run mode."""
    print()
    print("  Dry-run test case list:")
    for i, case in enumerate(TEST_CASES):
        print(f"  {i + 1}. [{case['id']}] {case['name']}")
        print(f"     Input:  {case.get('user_message', ' | '.join(case.get('messages', [])))[:100]}")
        print(f"     Expect: native_tool={case['expect_native_tool']}, tools={case['expect_tools']}")
    print()
    print("  To run real API tests:")
    print("    GOAL_RUN_REAL_LLM=1 python scripts/gray_native_tools.py")


if __name__ == "__main__":
    asyncio.run(main())
