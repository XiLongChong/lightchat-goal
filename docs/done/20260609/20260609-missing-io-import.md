# make_qr_png 缺少 import io（2026-06-09）

## 位置

`main.py:829` — `make_qr_png()` 函数内使用了 `io.BytesIO()`，但顶层没有 `import io`。

## 现象

调用 `/wechat/deploy/qr-image` 端点时抛出 `NameError: name 'io' is not defined`，二维码 PNG 生成失败。

## 根因

`make_qr_png()` 内部使用了 `import qrcode`（局部导入，正常工作），但 `io.BytesIO()` 没有对应的 `import io`——`io` 并非 Python 内置，需要显式导入。

## 触发条件

管理后台微信部署流程中，点击查看二维码图片时触发。普通聊天流程不受影响。

## 修复方向

在 `main.py` 顶部补充 `import io`。
