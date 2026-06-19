# main.py 职责拆分完成记录（2026-06-19）

关联方案：

- `docs/demo/20260610-main-py-extraction.md`（本地 demo 草案，目录被 `.gitignore` 忽略）

## 背景

`main.py` 长期承担入口装配、HTTP 路由、配置加载、WeChat 投递、用户 turn 状态、
协议事件写入、post-turn 观测 hooks、每日压缩触发编排、QR 工具和 admin 展示辅助等职责。
本次按批次拆出可独立测试的运行模块，保留 `main.py` 作为应用装配层和 FastAPI 路由层。

## 已完成批次

| 批次 | 内容 | 新增模块 |
| --- | --- | --- |
| A | admin 展示标签、联系人发送判断、日志读取、QR 工具 | `runtime/admin_labels.py`, `runtime/contact.py`, `runtime/log_reader.py`, `bridge/qr_utils.py` |
| B | 用户 turn 锁、中断检测、inbound seq 记录 | `runtime/user_turns.py` |
| C | 协议事件写入器 | `runtime/protocol_writer.py` |
| D | 通用 post-turn 观测 hooks | `runtime/turn_hooks.py` |
| E | WeChat sender 发送循环 | `bridge/sender.py` |
| F | 每日压缩后台触发编排 | `runtime/daily_compression.py` |
| G | 配置加载、深合并、runtime settings 读写 | `runtime/config.py` |

## 设计边界

- `main.py` 继续负责 FastAPI app、lifespan、路由、全局组件创建和运行时组件同步。
- `runtime/config.py` 只负责配置读取、合并和保存，不认识 bridge、tools、scheduler 或 FastAPI。
- `bridge/sender.py` 通过 `bridge_getter` 和 `config_getter` 注入运行依赖，不读取 `main.py` 或全局 `cfg`。
- `runtime/daily_compression.py` 只处理后台触发编排；压缩算法仍在 `agent/compressor.py`。
- `runtime/turn_hooks.py` 只包含通用观测 hooks；proactive scheduling hook 仍留在 `main.py`，因为它捕获 `task_scheduler` 和 proactive task 元数据。
- `agent/`、`runtime/`、`bridge/`、`memory/` 下没有业务模块 `import main`。

## 验证

```text
python -m pytest
382 passed, 13 skipped, 1 warning

python -m py_compile main.py runtime/config.py
passed

rg "import main|from main import" agent runtime bridge memory tests -g "*.py"
only tests import main

git diff --check
only LF -> CRLF warnings
```

## 注意事项

- `docs/demo/` 被 `.gitignore` 忽略；本次提交使用此 done 文档记录可追踪完成状态。
- `pc-agent.err.log` 和 `pc-agent.out.log` 是本地运行日志，不属于提交范围。
