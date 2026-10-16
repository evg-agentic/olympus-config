# olympus-config

Olympus 的平台级公共配置。目前只有一份：**模型价目表**。

| 文件 | 说明 |
|---|---|
| `pricing/model-prices.json` | 生成的价目表。Olympus 平台管理员把它的 raw 链接配成"价格源"，平台每天拉取一次 |
| `pricing/curated.json` | 手工维护的条目：国内厂商的人民币价、分时段价、非 token 计价项 |
| `scripts/build_pricing.py` | 合并上游价目表和手工条目，生成 `model-prices.json` |
| `scripts/validate_pricing.py` | 校验价目表。Olympus 同步时用同样的规则，一条不过整份拒收 |

价格源链接：

```text
https://raw.githubusercontent.com/evg-agentic/olympus-config/main/pricing/model-prices.json
```

## 更新价目

```bash
python3 scripts/build_pricing.py
```

脚本会拉取上游最新的价目表、合并、校验，然后覆盖 `pricing/model-prices.json`，并打印被丢弃或跳过的条目。只用 Python 标准库，不需要安装依赖。

改了 `curated.json` 之后也要重新跑一遍构建。推送前确认校验通过：

```bash
python3 scripts/validate_pricing.py pricing/model-prices.json
```

## 价格口径

**一律按模型厂商的官方价目**：模型由谁发布，就以谁的官方 API 定价为准。DeepSeek 看 DeepSeek，千问看阿里云百炼，Claude 看 Anthropic。

经第三方渠道调用时，价格可能和官方价不一致，例如百炼上的 DeepSeek、各家云上转售的模型。这种差价不写进本表，由使用该渠道的组织在 Olympus 里自己加租户规则覆盖。

## 数据来源与优先级

计价时按文件顺序找第一个命中的条目。构建脚本按下面的顺序排列条目，同一个模型名以排在前面的来源为准：

1. **`curated.json`**（手工维护）：
   - DeepSeek：按官方价目页，含高峰/空闲分时价；
   - 阿里云百炼的千问、向量模型、视觉模型：人民币价；
   - Tavily、通义万相、Supadata、语音识别：按次、按张、按 credit、按秒计价。
2. **[langfuse](https://github.com/langfuse/langfuse) `default-model-prices.json`**：OpenAI、Anthropic、Google。
   - 正则保留原样，覆盖 Bedrock、Vertex、Azure 等命名。
   - 原来的档位换算成本仓库的条件档：上下文阶梯，以及 priority/flex/fast 服务档位。
   - langfuse 每天用 CI 对照各厂商官方价目做审计。
3. **[litellm](https://github.com/BerriAI/litellm) `model_prices_and_context_window.json`**：
   - 补 langfuse 没有的厂商：xAI、Moonshot、MiniMax、智谱（zai）、Mistral；
   - 再补 OpenAI 在 langfuse 里缺的型号。
   - 同一厂商里价格相同的型号合并成一条。
   - 已标记退役（`deprecation_date` 已过）的型号不收。

## 格式（schema_version 1）

```json
{
  "id": "deepseek-v4-flash",
  "match": "(?i)^(deepseek/)?deepseek-v4-flash(-vision-exp)?$",
  "unit": "mtok",
  "currency": "CNY",
  "source": "价格出处",
  "note": "补充说明（可选）",
  "examples": ["deepseek-v4-flash"],
  "tiers": [
    { "name": "空闲时段", "rates": { "input": 1.5, "output": 4.5, "cache_read": 0.05 } },
    { "name": "高峰时段", "priority": 10,
      "when": [{ "time_window": { "tz": "Asia/Shanghai", "hours": ["09:00-12:00", "14:00-18:00"], "weekdays": [1, 2, 3, 4, 5] } }],
      "rates": { "input": 3.0, "output": 9.0, "cache_read": 0.1 } }
  ]
}
```

- **`match`**：Python 正则，用 `re.search` 匹配原样的模型名或计费键，大小写由 `(?i)` 声明。长度不超过 200，不允许嵌套的无界重复（防止灾难性回溯）。
- **`examples`**：必须落到本条目的计费键。校验时逐个检查：如果某个例子被排在前面的条目抢先命中，就说明那条正则写宽了，整份拒收。
- **`unit`**：`mtok`（每百万 token）、`request`、`image`、`credit` 或 `second`。
- **`currency`**：`USD` 或 `CNY`，费率按原币种填写。
- **`tiers`**：恰好一档没有 `when`，这档就是默认档。条件档的规则：
  - 条件之间是“与”的关系；
  - 同时命中多档时取 `priority` 最高的一档，作用于整次调用；
  - 没写的费率沿用默认档。
- **费率键**：`input`、`output`、`cache_read`、`cache_write_5m`、`cache_write_1h`；非 token 条目只有 `per_unit`。全为 0 的档必须写 `zero_reason`。
- **条件**：
  - `input_tokens {gt|gte: N}`：全部输入，含缓存读写；
  - `time_window {tz, hours, weekdays?}`：按调用开始时间判断，时段可以跨零点；
  - `service_tier {in: [...]}`：请求的服务档位。Anthropic 的 `speed: fast` 也按 `fast` 上报。

完整设计见 olympus 仓库 `docs/design/model-pricing-catalog.zh.md`。

## 已知缺口

- **百炼的 DeepSeek 价格不同：** 同名模型在百炼上是另一套价格（例如 `deepseek-v4-flash` 不分时、¥1/¥2）。按上面的价格口径，本表按 DeepSeek 官方价计。走百炼的组织需要在 Olympus 里加租户规则覆盖。
- **千问只有首档价：** 只收录了单次输入 ≤256K 的价格，更高档位的人民币价还没核对。
- **已退役的名字不计价：** `deepseek-chat`、`deepseek-reasoner`、`gpt-5.x-codex` 这些名字都没收录，用到时会显示为“未计价”。
- **音频、图像按模态单独计价的部分**（如 Gemini 的音频输入、OpenAI 的 realtime）不在本格式范围内。
