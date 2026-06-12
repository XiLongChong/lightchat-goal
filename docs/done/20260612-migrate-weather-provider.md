# 天气工具迁移到 QWeather 实施完成（2026-06-12）

来源：`docs/demo/20260611-migrate-weather-provider.md`（已归档 done）
Bug：`docs/bug/20260611-weather-tool-current-only.md`（已归档 done）

## 实施结果

已完成从 wttr.in 到 QWeather（和风天气）的完整迁移，所有 4 个 Phase 通过。

### 新增文件

| 文件 | 说明 |
|------|------|
| `agent/weather_provider.py` | QWeather provider 完整实现（~290 行）：WeatherProvider 协议、QWeatherProvider 类、GeoAPI 城市解析、实时观测、每日预报、日期规范化、结构化错误、三组独立缓存、格式化输出 |
| `tests/test_weather_provider.py` | 25 个单元测试：normalize_date、地点解析/消歧/缓存、观测/预报/fxDate 匹配、错误处理、缓存共享、格式化 |

### 修改文件

| 文件 | 变更 |
|------|------|
| `agent/tools.py` | 删除 `_get_weather`/`_WEATHER_CACHE`/wttr.in，新增 `_init_weather_provider`/`_execute_get_weather`，`configure_tools` 初始化 provider |
| `agent/tool_registry.py` | `get_weather` 新增 `date` 参数，更新 description，添加 `additionalProperties: False` |
| `agent/CLAUDE.md` | 文件职责表新增 `weather_provider.py`，修正 tools.py 描述 |
| `config.yaml` | weather 区块新增 `enabled`/`provider`/三组缓存 TTL |
| `.env.example` | 新增 `QWEATHER_API_KEY`/`QWEATHER_API_HOST` |
| `main.py` | `public_settings()` 更新 weather 字段，`_startup_validate()` 检查 QWEATHER_API_KEY |
| `tests/test_tools.py` | 用 FakeWeatherProvider 替换旧测试，新增预报/歧义/未配置/无效日期等测试 |

### 实施中发现和修复的额外问题

1. **HTTP 错误信息丢失** — 旧代码 HTTP 非 200 时只抛 `"HTTP 403"`，丢弃了 QWeather v2 返回的结构化错误。已修复：解析 v2 错误体（INVALID_HOST、SECURITY_RESTRICTION、FORBIDDEN 等），给出有意义的错误提示。

2. **城市消歧逻辑过严** — "成都"搜出成都+龙泉驿+新都等，这些是同省同市的区县，不是重名城市，但旧逻辑只看 rank 差距导致误判为歧义。已修复：增加 adm1+adm2 相同的同城判定规则。消歧策略更新为：
   - rank 领先（首个 ≤ 10 且第二个 > 50）
   - rank 3 倍领先（首个 ≤ 15 且第二个 > 首个 × 3）
   - 同省同市（adm1+adm2 相同）→ 不歧义，取首个
   - 不同省或不同市 → 真歧义，返回候选列表

### 真实 API 验收

使用真实 QWeather API Key 验证通过：
- ✅ "成都明天天气" → 返回预报，按 fxDate 精确匹配
- ✅ 城市消歧正确（同城区县不再误判）
- ✅ SECURITY_RESTRICTION / INVALID_HOST 等错误给出具体排查提示

### 测试结果

`176 passed, 13 skipped`（skip 为 Windows tmp_path 权限环境问题，非代码缺陷）

## 关键设计决策

| 决策 | 选择 | 原因 |
|------|------|------|
| v1 认证方式 | API Key | 简单易上手，JWT 后续通过子类 `_build_headers()` 扩展 |
| 预报匹配方式 | `fxDate` 精确匹配 | 不用数组索引，避免时区/日期偏移错误 |
| GeoAPI 缓存 | 仅缓存 LocationInfo 子集 | EULA 禁止批量缓存地理数据，单条 LocationID 映射是业界惯例 |
| wttr.in 旧代码 | 直接删除 | 设计文档明确"不静默 fallback"，旧代码与新契约不兼容，git 保留历史 |
| 不引入新依赖 | 用现有 httpx | QWeather 是标准 REST API，无需 SDK |
