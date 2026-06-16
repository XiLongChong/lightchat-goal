"""Microsoft To Do Provider — 通过 Microsoft Graph API 管理任务。

使用 MSAL Device Code Flow + PersistedTokenCache 进行认证。

认证状态机：
  1. 无 token、无 pending flow → 发起 device flow，返回设备码
  2. 无 token、有 pending flow → 单次轮询；若仍 pending，复用同一 flow（不生成新码）
  3. 有 token → 直接调用 API
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"


class TodoError(Exception):
    """MS To Do 操作的已知错误。"""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


def _get_config() -> dict[str, Any]:
    from agent.tools import TOOL_RUNTIME_CONFIG

    return TOOL_RUNTIME_CONFIG.get("microsoft_todo", {})


def _build_token_cache() -> Any | None:
    """使用 msal-extensions 创建 PersistedTokenCache。"""
    try:
        from msal_extensions import PersistedTokenCache, build_encrypted_persistence
    except ImportError:
        logger.warning("msal-extensions not available, token cache disabled")
        return None

    config = _get_config()
    cache_path = config.get("token_cache_path", "data/ms_todo_cache.bin")
    try:
        persistence = build_encrypted_persistence(cache_path)
        return PersistedTokenCache(persistence, lock_location=cache_path + ".lockfile")
    except Exception as e:
        logger.warning(f"Failed to create PersistedTokenCache: {e}, falling back to in-memory")
        return None


class MSTodoProvider:
    """Microsoft To Do API 的异步封装。"""

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._app: Any = None
        self._account: dict | None = None
        self._current_token: str | None = None
        # 存储 MSAL initiate_device_flow 返回的完整 flow dict
        # （含 device_code / user_code / verification_uri / interval 等）
        self._device_flow_data: dict | None = None
        self._flow_expires_at: float = 0
        # 序列化认证操作，防止并发生成多个 device flow
        self._lock = asyncio.Lock()

    @property
    def configured(self) -> bool:
        config = _get_config()
        return bool(config.get("client_id", ""))

    @property
    def auth_pending(self) -> bool:
        """是否有待完成的设备码流程。"""
        return (
            self._device_flow_data is not None
            and time.time() < self._flow_expires_at
        )

    def _get_msal_app(self) -> Any:
        if self._app is not None:
            return self._app

        import msal

        config = _get_config()
        client_id = config.get("client_id", "")
        authority = f"https://login.microsoftonline.com/{config.get('tenant_id', 'common')}"
        token_cache = _build_token_cache()

        self._app = msal.PublicClientApplication(
            client_id=client_id,
            authority=authority,
            token_cache=token_cache,
        )
        return self._app

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            config = _get_config()
            timeout = int(config.get("timeout_seconds", 15))
            self._client = httpx.AsyncClient(timeout=timeout)
        return self._client

    # ── 认证 ──────────────────────────────────────────

    def _format_device_flow_message(self) -> str:
        """格式化设备码提示消息（复用已有 flow 的 user_code）。"""
        if not self._device_flow_data:
            return "❌ No pending device flow."
        flow = self._device_flow_data
        remaining = max(0, self._flow_expires_at - time.time())
        return (
            f"🔑 Microsoft To Do 登录\n\n"
            f"请打开 {flow.get('verification_uri', 'https://microsoft.com/devicelogin')} "
            f"并输入以下代码：\n\n"
            f"{flow.get('user_code', 'N/A')}\n\n"
            f"此代码在 {int(remaining) // 60} 分钟内有效。"
        )

    def initiate_device_flow(self) -> str:
        """启动设备码登录流程。如果已有未过期的 pending flow，返回同一消息而不覆盖。"""
        if not self.configured:
            return "❌ Microsoft To Do 未配置（缺少 MS_TODO_CLIENT_ID）。"

        # 已有未过期 pending flow → 复用，不生成新码
        if self._device_flow_data and time.time() < self._flow_expires_at:
            return self._format_device_flow_message()

        import msal

        app = self._get_msal_app()
        scopes = ["Tasks.ReadWrite"]

        flow = app.initiate_device_flow(scopes=scopes)
        if "error" in flow:
            return f"❌ 设备码登录启动失败：{flow.get('error_description', flow['error'])}"

        # 存储完整 flow dict（含 user_code 等字段）以便复用消息
        self._device_flow_data = flow
        self._flow_expires_at = time.time() + flow.get("expires_in", 900)

        return self._format_device_flow_message()

    async def acquire_token(self) -> str | None:
        """仅尝试静默获取 access token（从缓存/刷新），不触发 device flow。
        返回 None 表示无可用 token，需调用 ensure_auth()。
        """
        if not self.configured:
            return None

        app = self._get_msal_app()
        scopes = ["Tasks.ReadWrite"]

        accounts = app.get_accounts()
        if accounts:
            self._account = accounts[0]
            result = await asyncio.to_thread(
                app.acquire_token_silent, scopes, account=accounts[0]
            )
            if result and "access_token" in result:
                self._current_token = result["access_token"]
                return self._current_token

        return None

    async def ensure_auth(self) -> tuple[str | None, str | None]:
        """确保认证完成（线程安全）。返回 (token, auth_message)。

        - token 非 None：认证成功，可直接调用 API
        - auth_message 非 None：应返回给用户（设备码或错误消息）
        - 两者都 None：未配置
        """
        if not self.configured:
            return None, None

        async with self._lock:
            # 1. 尝试静默获取
            app = self._get_msal_app()
            scopes = ["Tasks.ReadWrite"]

            accounts = app.get_accounts()
            if accounts:
                self._account = accounts[0]
                result = await asyncio.to_thread(
                    app.acquire_token_silent, scopes, account=accounts[0]
                )
                if result and "access_token" in result:
                    self._current_token = result["access_token"]
                    return self._current_token, None

            # 2. 尝试完成已有 device flow（单次轮询，不阻塞）
            if self._device_flow_data:
                if time.time() >= self._flow_expires_at:
                    self._device_flow_data = None
                    self._flow_expires_at = 0
                else:
                    try:
                        # 传 flow 副本并设 expires_at=0 → MSAL 退出条件立即满足
                        # 效果：单次 HTTP 请求，不进入 MSAL 内部轮询循环
                        flow_copy = dict(self._device_flow_data)
                        flow_copy["expires_at"] = 0
                        result = await asyncio.to_thread(
                            app.acquire_token_by_device_flow,
                            flow_copy,
                        )
                    except Exception as e:
                        logger.warning(f"Device flow poll error: {e}")
                    else:
                        if "access_token" in result:
                            self._device_flow_data = None
                            self._flow_expires_at = 0
                            self._current_token = result["access_token"]
                            if app.get_accounts():
                                self._account = app.get_accounts()[0]
                            return self._current_token, None

                        if result.get("error") != "authorization_pending":
                            # 非等待中的错误（如 expired、denied），清除 flow
                            logger.warning(
                                f"Device flow error: {result.get('error')}: "
                                f"{result.get('error_description', '')}"
                            )
                            self._device_flow_data = None
                            self._flow_expires_at = 0
                        # authorization_pending → 保留 flow，复用消息

            # 3. 需要设备码 — 复用已有 pending flow 或创建新的
            if self._device_flow_data and time.time() < self._flow_expires_at:
                return None, self._format_device_flow_message()

            flow = app.initiate_device_flow(scopes=scopes)
            if "error" in flow:
                return None, f"❌ 设备码登录启动失败：{flow.get('error_description', flow['error'])}"

            self._device_flow_data = flow
            self._flow_expires_at = time.time() + flow.get("expires_in", 900)
            return None, self._format_device_flow_message()

    # ── API 请求 ──────────────────────────────────────

    async def _request(
        self, method: str, path: str, json_data: dict | None = None
    ) -> dict[str, Any]:
        """发送 Graph API 请求，含 401 刷新和 429 重试。"""
        return await self._do_request(method, path, json_data)

    async def _do_request(
        self,
        method: str,
        path: str,
        json_data: dict | None = None,
        *,
        _is_retry: bool = False,
    ) -> dict[str, Any]:
        client = await self._get_client()
        url = f"{GRAPH_BASE}{path}"
        headers = {
            "Authorization": f"Bearer {self._current_token}",
            "Content-Type": "application/json",
        }

        try:
            resp = await client.request(method, url, headers=headers, json=json_data)
        except Exception as e:
            logger.error(f"Graph API request failed: {e}")
            return {"error": f"请求失败: {e}"}

        # 401 → 刷新 token 并重试一次
        if resp.status_code == 401 and not _is_retry:
            logger.info("MS Graph 401, refreshing token...")
            app = self._get_msal_app()
            if self._account:
                result = await asyncio.to_thread(
                    app.acquire_token_silent,
                    ["Tasks.ReadWrite"],
                    account=self._account,
                    force_refresh=True,
                )
                if result and "access_token" in result:
                    self._current_token = result["access_token"]
                    return await self._do_request(
                        method, path, json_data, _is_retry=True
                    )

        # 429 → Retry-After
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", 5))
            if retry_after > 30:
                logger.warning(f"MS Graph 429 Retry-After={retry_after}s, returning rate_limited")
                raise TodoError("rate_limited", f"Microsoft To Do is busy. Try again in {retry_after}s.")
            logger.warning(f"MS Graph 429, waiting {retry_after}s")
            await asyncio.sleep(retry_after)
            return await self._do_request(method, path, json_data, _is_retry=True)

        if resp.status_code >= 400:
            logger.error(f"Graph API error {resp.status_code}: {resp.text[:300]}")
            return {"error": f"API 请求失败 ({resp.status_code})"}

        return resp.json()

    # ── 业务方法 ──────────────────────────────────────

    async def get_lists(self) -> list[dict]:
        """获取所有任务列表（跟随 @odata.nextLink 分页）。"""
        all_lists = []
        url = "/me/todo/lists"
        seen: set[str] = set()

        while url:
            if url in seen:
                break
            seen.add(url)

            data = await self._request("GET", url)
            if "error" in data:
                return []
            all_lists.extend(data.get("value", []))
            url = data.get("@odata.nextLink", "")
            if url:
                # 从完整 URL 提取路径部分
                if url.startswith(GRAPH_BASE):
                    url = url[len(GRAPH_BASE):]

        return all_lists

    async def get_default_list(self) -> dict:
        """获取默认任务列表。"""
        lists = await self.get_lists()
        for lst in lists:
            if lst.get("wellknownListName") == "defaultList":
                return lst
        raise TodoError("default_list_not_found", "Default task list was not found")

    async def get_tasks(self, list_id: str, max_results: int = 20, include_completed: bool = False) -> list[dict]:
        """获取任务列表中的任务（跟随 @odata.nextLink）。"""
        all_tasks = []
        url = f"/me/todo/lists/{list_id}/tasks"
        params: dict[str, Any] = {"$top": min(max_results, 50), "$orderby": "createdDateTime desc"}
        seen: set[str] = set()

        while url and len(all_tasks) < max_results:
            if url in seen:
                break
            seen.add(url)

            data = await self._request("GET", url)
            if "error" in data:
                return []
            tasks = data.get("value", [])
            if not include_completed:
                tasks = [t for t in tasks if t.get("status") != "completed"]
            all_tasks.extend(tasks)
            url = data.get("@odata.nextLink", "")
            if url and url.startswith(GRAPH_BASE):
                url = url[len(GRAPH_BASE):]

        return all_tasks[:max_results]

    async def create_task(
        self,
        list_id: str,
        title: str,
        due_date: str | None = None,
        reminder_date: str | None = None,
        note: str | None = None,
        importance: str = "normal",
    ) -> dict:
        """创建任务。"""
        task: dict[str, Any] = {"title": title.strip()}

        if due_date:
            task["dueDateTime"] = {"dateTime": due_date, "timeZone": "Asia/Shanghai"}
        if reminder_date:
            task["reminderDateTime"] = {"dateTime": reminder_date, "timeZone": "Asia/Shanghai"}
            task["isReminderOn"] = True
        if note:
            task["body"] = {"contentType": "text", "content": note}
        if importance in ("low", "normal", "high"):
            task["importance"] = importance

        return await self._request("POST", f"/me/todo/lists/{list_id}/tasks", json_data=task)

    async def complete_task(self, list_id: str, task_id: str | None = None, task_title: str | None = None) -> dict:
        """完成任务。task_id 优先，否则按标题精确匹配。"""
        if task_id:
            # 先验证 task 存在
            result = await self._request("GET", f"/me/todo/lists/{list_id}/tasks/{task_id}")
            if "error" in result:
                return {"error": "not_found", "message": f"Task {task_id} not found"}
            if result.get("status") == "completed":
                return {"completed": True, "title": result.get("title", ""), "already_completed": True}
            # 执行完成
            patch_result = await self._request(
                "PATCH", f"/me/todo/lists/{list_id}/tasks/{task_id}",
                json_data={"status": "completed"},
            )
            return {"completed": True, "title": patch_result.get("title", "")}

        # 按标题匹配
        if not task_title:
            return {"error": "invalid_args", "message": "task_title or task_id required"}

        tasks = await self.get_tasks(list_id, max_results=999)  # 获取全部未完成任务
        matches = [t for t in tasks if t.get("title", "").strip() == task_title.strip()]

        if not matches:
            return {"error": "not_found", "message": f'No task matching "{task_title}"'}
        if len(matches) > 1:
            candidates = [
                {"task_id": t["id"], "title": t["title"], "due": t.get("dueDateTime", {}).get("dateTime", "")}
                for t in matches
            ]
            return {"error": "ambiguous", "candidates": candidates}

        task = matches[0]
        result = await self._request(
            "PATCH", f"/me/todo/lists/{list_id}/tasks/{task['id']}",
            json_data={"status": "completed"},
        )
        return {"completed": True, "title": result.get("title", task_title)}

    async def find_list_by_name(self, name: str) -> list[dict]:
        """按名称模糊匹配列表。"""
        lists = await self.get_lists()
        name_lower = name.strip().lower()
        return [lst for lst in lists if name_lower in lst.get("displayName", "").lower()]
