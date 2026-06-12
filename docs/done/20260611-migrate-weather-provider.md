# 天气工具迁移到专业天气 API（2026-06-11）

## 决策

停止继续扩展当前 `wttr.in` 天气实现，不实施：

`docs/demo/20260611-fix-weather-tool.md`

保留现有 `get_weather` 工具名称和 Agent 层契约，将底层 provider 迁移到专业天气 API。v1 默认选择 QWeather（和风天气）。

对应 Bug：

`docs/bug/20260611-weather-tool-current-only.md`

该 Bug 在迁移完成并通过真实天气查询验收前保持开放。

## 为什么不继续使用 wttr.in

`wttr.in` 适合个人查询、命令行和原型，但不作为当前产品的长期主天气数据源：

- 公共接口没有适合生产依赖的明确 SLA
- 城市名称解析和预报契约不够稳定
- 国内部署环境的网络质量需要额外验证
- 扩展日期、预警、降雨概率等能力时需要维护较多 provider 特有解析

迁移后可暂时保留旧实现代码用于回滚，但不自动 fallback。多个天气源结果和日期语义可能不同，静默 fallback 会让问题更难诊断。

## Provider 选择

### v1 默认：QWeather

选择理由：

- 中国城市和中文天气描述适配较好
- 提供实时天气、逐小时和 3 至 30 天预报
- 返回明确的预报日期 `fxDate`
- 支持天气预警、空气质量等后续能力
- 官方提供 GeoAPI，可将城市名解析为稳定 LocationID

认证优先使用 QWeather 推荐的 JWT。API Key 可用于早期开发，但不作为长期默认认证方式。

### 备选：Open-Meteo

Open-Meteo 可作为非商业原型或未来替代 provider。其免费开放接口不提供商业使用许可和 uptime 保证，正式商业使用需要订阅。

## 工具契约

保留工具名 `get_weather`，扩展为明确的日期查询：

```json
{
  "name": "get_weather",
  "description": "Get real-time weather or an official weather forecast for a city. Use the requested date exactly and never infer future weather from current conditions.",
  "parameters": {
    "type": "object",
    "properties": {
      "location": {
        "type": "string",
        "description": "City or region name, such as 成都 or 北京."
      },
      "date": {
        "type": "string",
        "description": "Optional date: today, tomorrow, 后天, or YYYY-MM-DD. Omit for current weather."
      }
    },
    "required": ["location"],
    "additionalProperties": false
  }
}
```

语义：

- 不传 `date`：实时天气
- `today` / `今天`：当天预报，不伪装成实时观测
- `tomorrow` / `明天`：明日预报
- `day after tomorrow` / `后天`：后日预报
- `YYYY-MM-DD`：在 provider 支持范围内按日期精确匹配
- 无法解析或超出范围：返回明确错误，不使用最近日期代替

## 内部返回结构

天气 provider 先返回结构化结果，最后由工具格式化为紧凑文本或 JSON：

```json
{
  "provider": "qweather",
  "location": {
    "name": "成都",
    "id": "101270101"
  },
  "kind": "forecast",
  "date": "2026-06-12",
  "condition_day": "多云",
  "condition_night": "小雨",
  "temp_min_c": 21,
  "temp_max_c": 29,
  "precip_mm": 2.4,
  "humidity_percent": 76,
  "wind": "东北风 1-2级",
  "updated_at": "2026-06-11T20:00+08:00"
}
```

模型必须能够区分：

- `kind=observation`：实时观测
- `kind=forecast`：预报

工具结果中始终包含数据日期和更新时间，避免模型把旧缓存或当天预报说成实时数据。

## 查询流程

1. 规范化用户地点和日期
2. 通过 QWeather GeoAPI 将地点解析为 LocationID
3. 缓存地点到 LocationID 的映射
4. 无日期时调用实时天气接口
5. 有日期时调用每日预报接口，并按 `fxDate` 精确匹配
6. 将 provider 响应转换为内部结构
7. 返回给模型，禁止根据当前天气自行推测未来

地点有歧义时不静默选择明显不同的城市。可以返回候选地点，或要求模型向用户确认省份/国家。

## 代码边界

建议新增：

```text
agent/weather_provider.py
```

职责：

- `WeatherProvider` 协议
- `QWeatherProvider`
- 地点解析和缓存
- 日期规范化
- provider 响应到内部结构的转换

现有文件修改：

- `agent/tool_registry.py`：增加 `date` 参数并更新描述
- `agent/tools.py`：调用 `WeatherProvider`，不直接拼 provider URL
- `config.yaml`：provider、超时、缓存和 API host
- `.env.example`：QWeather 凭据配置
- `tests/test_tools.py`：实时、今天、明天、具体日期和失败路径测试

建议配置：

```yaml
weather:
  enabled: true
  provider: qweather
  default_city: "成都"
  timeout_seconds: 5
  observation_cache_ttl_seconds: 600
  forecast_cache_ttl_seconds: 1800
  location_cache_ttl_seconds: 86400
```

凭据仅从环境变量读取。具体环境变量名称在认证实现确定后统一加入 `.env.example`，不得写入 `config.yaml`。

## 迁移策略

1. 先建立 `WeatherProvider` 边界和 QWeather fake 测试
2. 接入 GeoAPI、实时天气和每日预报
3. 修改 `get_weather` schema 和执行路径
4. 使用测试凭据做成都实时、今天、明天和具体日期验证
5. 观察网络错误、地点歧义和缓存行为
6. 验收后删除 `wttr.in` 专用解析和旧缓存逻辑
7. 将原 Bug 移出 `docs/bug`，迁移记录归档到 `docs/done`

迁移期间不同时推进旧 `wttr.in` 预报修复，避免为即将删除的接口增加维护成本。

## 验收

1. “成都现在天气”返回实时观测，并带更新时间
2. “成都今天天气”返回当天预报，明确标记为预报
3. “成都明天天气”按明天的 `fxDate` 返回，不使用数组固定索引猜测
4. 具体日期精确匹配，超出 provider 范围时明确失败
5. 同名城市有歧义时不静默选错地点
6. provider 超时、鉴权失败或额度不足时不返回伪造天气
7. Agent 不再依据当前温度推断未来降雨或出行建议
8. 缓存键至少包含 LocationID、查询类型和目标日期
9. 日志不包含 JWT 私钥、API Key 或完整认证 token

