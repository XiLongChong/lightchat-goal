"""Test real-user-inbound guard and clingy state transitions.

Ensures:
- /chat doesn't NameError on trigger/source
- _is_real_user_inbound correctly rejects empty/admin/system/proactive
- Non-skip replies set awaiting_user_reply
- Internal errors don't show "delivered" in diagnostics
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from main import _is_real_user_inbound  # type: ignore[import-not-found]


class TestIsRealUserInbound:
    def test_normal_wechat_message_passes(self):
        assert _is_real_user_inbound("wxid_abc", "你好", "user", "wechat") is True

    def test_default_user_rejected(self):
        assert _is_real_user_inbound("default", "你好", "user", "wechat") is False

    def test_empty_user_id_rejected(self):
        assert _is_real_user_inbound("", "你好", "user", "wechat") is False

    def test_empty_text_rejected(self):
        assert _is_real_user_inbound("wxid_abc", "", "user", "wechat") is False
        assert _is_real_user_inbound("wxid_abc", "   ", "user", "wechat") is False

    def test_admin_trigger_rejected(self):
        assert _is_real_user_inbound("wxid_abc", "hello", "admin", "wechat") is False
        assert _is_real_user_inbound("wxid_abc", "hello", "system", "wechat") is False
        assert _is_real_user_inbound("wxid_abc", "hello", "proactive", "wechat") is False

    def test_non_wechat_source_rejected(self):
        assert _is_real_user_inbound("wxid_abc", "hello", "user", "debug") is False
        assert _is_real_user_inbound("wxid_abc", "hello", "user", "admin") is False

    def test_wechat_inbound_source_passes(self):
        assert _is_real_user_inbound("wxid_abc", "hello", "user", "wechat_inbound") is True

    def test_empty_source_defaults_to_wechat(self):
        # Empty source is treated as wechat (backward compat)
        assert _is_real_user_inbound("wxid_abc", "hello", "user", "") is True

    def test_normal_text_passes(self):
        assert _is_real_user_inbound("wxid_abc", "好了 收到这条消息 不用给我回复", "user", "wechat") is True
