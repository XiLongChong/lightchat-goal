from bridge.wechat_poller import WeChatBridge


def test_bounded_reply_delay_clamps_model_delay():
    bridge = WeChatBridge(token="token", max_reply_delay=3.0)

    assert bridge._bounded_reply_delay(12) == 3.0
    assert bridge._bounded_reply_delay(-2) == 0.0
    assert bridge._bounded_reply_delay("bad") == 0.0


def test_bounded_reply_delay_can_disable_upper_bound():
    bridge = WeChatBridge(token="token", max_reply_delay=0)

    assert bridge._bounded_reply_delay(12) == 12.0
