"""测试 web_search 工具（同步函数）。"""

from __future__ import annotations

from unittest.mock import MagicMock, patch


class TestWebSearch:
    def test_no_api_key(self):
        from agent.search_provider import web_search
        with patch("agent.search_provider._get_tavily_config", return_value={"api_key": ""}):
            result = web_search("test")
            assert "未配置" in result

    def test_successful_search(self):
        from agent.search_provider import web_search
        mock = MagicMock()
        mock.search.return_value = {
            "answer": "答案", "results": [{"title": "T", "url": "https://a.com", "content": "C"}],
        }
        with (
            patch("agent.search_provider._get_tavily_config", return_value={"api_key": "k", "max_results": 5, "include_answer": True}),
            patch("agent.search_provider._get_client", return_value=mock),
        ):
            result = web_search("test")
        assert "答案" in result
        assert "T" in result

    def test_empty_results(self):
        from agent.search_provider import web_search
        mock = MagicMock()
        mock.search.return_value = {"results": []}
        with (
            patch("agent.search_provider._get_tavily_config", return_value={"api_key": "k", "max_results": 5, "include_answer": True}),
            patch("agent.search_provider._get_client", return_value=mock),
        ):
            result = web_search("nothing")
        assert "未找到" in result

    def test_retry(self):
        from agent.search_provider import web_search
        mock = MagicMock()
        mock.search.side_effect = [Exception("err"), {"answer": "ok", "results": [{"title": "R", "url": "https://a.com", "content": "c"}]}]
        with (
            patch("agent.search_provider._get_tavily_config", return_value={"api_key": "k", "max_results": 5, "include_answer": True}),
            patch("agent.search_provider._get_client", return_value=mock),
        ):
            result = web_search("test")
        assert mock.search.call_count == 2
        assert "ok" in result

    def test_double_failure(self):
        from agent.search_provider import web_search
        mock = MagicMock()
        mock.search.side_effect = Exception("fail")
        with (
            patch("agent.search_provider._get_tavily_config", return_value={"api_key": "k", "max_results": 5, "include_answer": True}),
            patch("agent.search_provider._get_client", return_value=mock),
        ):
            result = web_search("test")
        assert mock.search.call_count == 2
        assert "搜索失败" in result

    def test_truncation(self):
        from agent.search_provider import web_search
        long_c = "x" * 500
        mock = MagicMock()
        mock.search.return_value = {"results": [{"title": "T", "url": "https://a.com", "content": long_c}]}
        with (
            patch("agent.search_provider._get_tavily_config", return_value={"api_key": "k", "max_results": 5, "include_answer": False}),
            patch("agent.search_provider._get_client", return_value=mock),
        ):
            result = web_search("test")
        assert (long_c[:300] + "...") in result

    def test_executor_empty_query(self):
        from agent.tools import _execute_web_search
        result = _execute_web_search({"query": ""})
        assert "不能为空" in result

    def test_executor_normal(self):
        from agent.tools import _execute_web_search
        with patch("agent.search_provider.web_search", return_value="ok"):
            result = _execute_web_search({"query": "hello"})
            assert result == "ok"
