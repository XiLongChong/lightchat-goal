"""测试 web_search 工具（同步函数）。"""

from __future__ import annotations

from unittest.mock import MagicMock, patch


class TestWebSearch:
    def test_no_api_key(self):
        from agent.search_provider import web_search
        with patch("agent.search_provider._get_tavily_config", return_value={"api_key": ""}):
            result = web_search("test")
            assert "not_configured" in result

    def test_successful_search(self):
        from agent.search_provider import web_search
        response = {
            "answer": "答案", "results": [{"title": "T", "url": "https://a.com", "content": "C"}],
        }
        with (
            patch("agent.search_provider._get_tavily_config", return_value={"api_key": "k", "max_results": 5, "include_answer": True}),
            patch("agent.search_provider._request_tavily", return_value=response),
        ):
            result = web_search("test")
        assert "答案" in result
        assert "T" in result

    def test_empty_results(self):
        from agent.search_provider import web_search
        with (
            patch("agent.search_provider._get_tavily_config", return_value={"api_key": "k", "max_results": 5, "include_answer": True}),
            patch("agent.search_provider._request_tavily", return_value={"results": []}),
        ):
            result = web_search("nothing")
        assert "No results found" in result

    def test_cache(self):
        from agent.search_provider import _SEARCH_CACHE, web_search
        _SEARCH_CACHE.clear()
        response = {"answer": "ok", "results": [{"title": "R", "url": "https://a.com", "content": "c"}]}
        with (
            patch("agent.search_provider._get_tavily_config", return_value={"api_key": "k", "max_results": 5, "include_answer": True, "cache_ttl_seconds": 300}),
            patch("agent.search_provider._request_tavily", return_value=response) as request,
        ):
            result = web_search("test")
            result2 = web_search("test")
        assert request.call_count == 1
        assert result == result2

    def test_double_failure(self):
        from agent.search_provider import _SEARCH_CACHE, web_search
        _SEARCH_CACHE.clear()
        with (
            patch("agent.search_provider._get_tavily_config", return_value={"api_key": "k", "max_results": 5, "include_answer": True}),
            patch("agent.search_provider._request_tavily", return_value={"error": "provider_error", "message": "network error"}) as request,
        ):
            result = web_search("test")
        assert request.call_count == 1
        assert "provider_error" in result

    def test_truncation(self):
        from agent.search_provider import web_search
        long_c = "x" * 500
        with (
            patch("agent.search_provider._get_tavily_config", return_value={"api_key": "k", "max_results": 5, "include_answer": False}),
            patch("agent.search_provider._request_tavily", return_value={"results": [{"title": "T", "url": "https://a.com", "content": long_c}]}),
        ):
            result = web_search("test")
        assert (long_c[:300] + "...") in result

    def test_executor_empty_query(self):
        from agent.tools import _execute_web_search
        result = _execute_web_search({"query": ""})
        assert "invalid_query" in result

    def test_executor_normal(self):
        from agent.tools import _execute_web_search
        with patch("agent.search_provider.web_search", return_value="ok"):
            result = _execute_web_search({"query": "hello"})
            assert result == "ok"

    def test_web_fetch_invalid_url(self):
        from agent.search_provider import web_fetch
        assert "invalid_url" in web_fetch("file:///tmp/a")

    def test_web_fetch_truncates(self):
        from agent.search_provider import web_fetch
        with (
            patch("agent.search_provider._get_tavily_config", return_value={"api_key": "k", "max_extract_chars": 5}),
            patch("agent.search_provider._request_tavily", return_value={
                "results": [{"title": "Page", "raw_content": "123456789"}],
            }),
        ):
            result = web_fetch("https://example.com")
        assert "Page" in result
        assert "Content truncated" in result
