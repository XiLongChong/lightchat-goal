"""Tavily 搜索 Provider — 联网搜索 + 网页抓取。

通过 Tavily API 实现 web_search 和 web_fetch 功能。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _get_tavily_config() -> dict[str, Any]:
    """获取 Tavily 配置。"""
    import main
    cfg = getattr(main, "cfg", {})
    return cfg.get("tavily", {})


def _get_client():
    """延迟创建 Tavily 客户端。"""
    from tavily import TavilyClient

    config = _get_tavily_config()
    api_key = config.get("api_key", "")
    if not api_key:
        return None
    return TavilyClient(api_key=api_key)


def web_search(
    query: str,
    max_results: int | None = None,
    include_answer: bool | None = None,
    include_domains: list[str] | None = None,
    exclude_domains: list[str] | None = None,
) -> str:
    """通过 Tavily API 搜索互联网。

    Args:
        query: 搜索查询词
        max_results: 最大结果数（默认 5）
        include_answer: 是否包含 AI 摘要（默认 true）
        include_domains: 限定域名列表
        exclude_domains: 排除域名列表

    Returns:
        格式化的搜索结果字符串
    """
    config = _get_tavily_config()
    api_key = config.get("api_key", "")

    if not api_key:
        return "❌ 联网搜索未配置（缺少 TAVILY_API_KEY）。"

    client = _get_client()
    if client is None:
        return "❌ 联网搜索初始化失败。"

    if max_results is None:
        max_results = int(config.get("max_results", 5))
    if include_answer is None:
        include_answer = bool(config.get("include_answer", True))

    params: dict[str, Any] = {
        "query": query,
        "max_results": max_results,
        "include_answer": include_answer,
        "search_depth": "basic",
    }
    if include_domains:
        params["include_domains"] = include_domains
    if exclude_domains:
        params["exclude_domains"] = exclude_domains

    # 调用 Tavily（失败时重试一次）
    for attempt in range(2):
        try:
            response = client.search(**params)
            break
        except Exception as e:
            if attempt == 0:
                logger.warning(f"Tavily search attempt 1 failed for '{query[:60]}': {e}")
            else:
                logger.error(f"Tavily search retry also failed: {e}")
                return f"❌ 搜索失败：{e}"

    # 格式化输出
    lines: list[str] = []
    answer = response.get("answer")
    if answer and include_answer:
        lines.append(f"📝 摘要：{answer}")
        lines.append("")

    results = response.get("results", [])
    if not results:
        return "🔍 未找到相关结果。"

    lines.append(f"🔍 搜索结果（共 {len(results)} 条）：")
    for i, r in enumerate(results, 1):
        title = r.get("title", "无标题")
        url = r.get("url", "")
        content = r.get("content", "")
        if len(content) > 300:
            content = content[:300] + "..."
        lines.append(f"{i}. {title}")
        lines.append(f"   {url}")
        lines.append(f"   {content}")
        lines.append("")

    return "\n".join(lines).strip()
