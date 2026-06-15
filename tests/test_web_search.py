"""测试 web_search 工具。"""

from __future__ import annotations

from unittest.mock import MagicMock, patch


class TestWebSearch:
    """测试 web_search 工具。"""

    def test_no_api_key(self):
        """无 API key 时返回错误消息。"""
        from agent.search_provider import web_search

        with patch("agent.search_provider._get_tavily_config", return_value={"api_key": ""}):
            result = web_search("test")
            assert "未配置" in result
            assert "TAVILY_API_KEY" in result

    def test_successful_search_with_answer(self):
        """成功搜索并包含 AI 答案。"""
        from agent.search_provider import web_search

        mock_client = MagicMock()
        mock_client.search.return_value = {
            "answer": "测试答案",
            "results": [
                {"title": "标题1", "url": "https://a.com/1", "content": "内容1"},
                {"title": "标题2", "url": "https://a.com/2", "content": "内容2"},
            ],
        }

        with (
            patch("agent.search_provider._get_tavily_config",
                  return_value={"api_key": "k", "max_results": 5, "include_answer": True}),
            patch("agent.search_provider._get_client", return_value=mock_client),
        ):
            result = web_search("test")

        assert "摘要" in result
        assert "测试答案" in result
        assert "标题1" in result
        assert "https://a.com/1" in result

    def test_empty_results(self):
        """空结果。"""
        from agent.search_provider import web_search

        mock_client = MagicMock()
        mock_client.search.return_value = {"results": []}

        with (
            patch("agent.search_provider._get_tavily_config",
                  return_value={"api_key": "k", "max_results": 5, "include_answer": True}),
            patch("agent.search_provider._get_client", return_value=mock_client),
        ):
            result = web_search("nothing")
            assert "未找到" in result

    def test_retry_on_error(self):
        """失败时重试一次。"""
        from agent.search_provider import web_search

        mock_client = MagicMock()
        mock_client.search.side_effect = [
            Exception("网络错误"),
            {"answer": "重试成功", "results": [{"title": "结果", "url": "https://a.com", "content": "c"}]},
        ]

        with (
            patch("agent.search_provider._get_tavily_config",
                  return_value={"api_key": "k", "max_results": 5, "include_answer": True}),
            patch("agent.search_provider._get_client", return_value=mock_client),
        ):
            result = web_search("test")

        assert mock_client.search.call_count == 2
        assert "重试成功" in result

    def test_double_failure(self):
        """两次都失败。"""
        from agent.search_provider import web_search

        mock_client = MagicMock()
        mock_client.search.side_effect = Exception("持续失败")

        with (
            patch("agent.search_provider._get_tavily_config",
                  return_value={"api_key": "k", "max_results": 5, "include_answer": True}),
            patch("agent.search_provider._get_client", return_value=mock_client),
        ):
            result = web_search("test")

        assert mock_client.search.call_count == 2
        assert "搜索失败" in result

    def test_long_content_truncation(self):
        """长内容被截断。"""
        from agent.search_provider import web_search

        long_content = "x" * 500
        mock_client = MagicMock()
        mock_client.search.return_value = {
            "results": [{"title": "T", "url": "https://a.com", "content": long_content}],
        }

        with (
            patch("agent.search_provider._get_tavily_config",
                  return_value={"api_key": "k", "max_results": 5, "include_answer": False}),
            patch("agent.search_provider._get_client", return_value=mock_client),
        ):
            result = web_search("test")

        assert "..." in result
        # Content should be truncated
        displayed = long_content[:300] + "..."
        assert displayed in result

    def test_tool_executor_empty_query(self):
        """执行器空查询。"""
        from agent.tools import _execute_web_search

        result = _execute_web_search({"query": ""})
        assert "不能为空" in result

    def test_tool_executor_normal(self):
        """执行器正常调用。"""
        from agent.tools import _execute_web_search

        with patch("agent.search_provider.web_search", return_value="搜索成功结果"):
            result = _execute_web_search({"query": "hello"})
            assert result == "搜索成功结果"
