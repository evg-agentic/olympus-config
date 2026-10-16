# olympus-config

Olympus 的平台级公共配置。目前包含**模型价目表**：Olympus 按它给每次模型调用和工具调用计价。

价格源链接：

```text
https://raw.githubusercontent.com/evg-agentic/olympus-config/main/pricing/model-prices.json
```

## 仓库结构

| 路径 | 说明 |
|---|---|
| `pricing/model-prices.json` | 价目表，由构建脚本生成，不要手改 |
| `pricing/curated.json` | 自行维护的条目：上游价目表没有收录或口径不同的模型，以及按次、按张、按秒计价的项目 |
| `scripts/build_pricing.py` | 拉取上游价目，和 `curated.json` 合并，生成价目表 |
| `scripts/validate_pricing.py` | 校验价目表，规则和 Olympus 同步价格源时一致 |
| `scripts/pricing_diff.py` | 生成价目变更摘要 |
| `tests/` | 单元测试 |
| `.github/workflows/` | 每日同步与校验 |

## 价格口径

一律按模型厂商的官方价目：模型由谁发布，就以谁的官方 API 定价为准。DeepSeek 按 DeepSeek，千问按阿里云百炼，Claude 按 Anthropic。

经第三方渠道（如云厂商转售）调用时，实际价格可能不同，这部分不在本表中，由使用该渠道的组织在 Olympus 中用租户规则覆盖。

范围：

- 按厂商公开的标准价，不含限时折扣、免费额度和 Batch 价。
- 千问按百炼华北 2（北京）地域的人民币价；缓存价按各模型支持的缓存方式取，同时支持隐式与显式缓存的模型按隐式缓存命中价计。
- 已退役的型号保留最后的官方价，条目的 `note` 中写明。
- 音频、图像等按模态单独计价的部分不在本格式范围内。

## 数据来源

计价时按文件顺序取第一个命中的条目。价目表中的条目按以下来源依次排列，同一模型以排在前面的来源为准：

1. **`pricing/curated.json`**：DeepSeek（含高峰 / 空闲分时价）、阿里云百炼的千问系列（含上下文阶梯和思考模式价）、向量与视觉模型，以及 Tavily、通义万相、Supadata、语音识别等非 token 计价项。
2. **[langfuse](https://github.com/langfuse/langfuse)** `worker/src/constants/default-model-prices.json`：OpenAI、Anthropic、Google。匹配正则沿用原文件，覆盖 Bedrock、Vertex、Azure 等命名；上下文阶梯与服务档位换算为本仓库的条件档。
3. **[litellm](https://github.com/BerriAI/litellm)** `model_prices_and_context_window.json`：xAI、Moonshot、MiniMax、智谱、Mistral，以及 OpenAI 在 langfuse 中未收录的型号。同一厂商中价格相同的型号合并为一条。

价目表的 `sources` 字段记录了生成时所用的上游提交。

## 格式（schema_version 1）

```json
{
  "id": "deepseek-v4-flash",
  "match": "(?i)^(deepseek/)?deepseek-v4-flash$",
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

| 字段 | 说明 |
|---|---|
| `id` | 条目标识，`[a-z0-9._-]{1,32}`，文件内唯一 |
| `match` | Python 正则，用 `re.search` 匹配原样的模型名或计费键；不超过 200 字符，不允许嵌套的无界重复 |
| `unit` | `mtok`（每百万 token）、`request`、`image`、`credit`、`second` |
| `currency` | `USD` 或 `CNY`，费率按原币种填写 |
| `examples` | 必须匹配到本条目的计费键，用于校验正则没有写得过宽 |
| `source` / `note` | 价格出处与说明，仅供阅读 |
| `tiers` | 计价档位，见下文 |

**档位**：恰好一档不带 `when`，为默认档。条件档的 `when` 中所有条件同时成立才命中；命中多档时取 `priority` 最高的一档，作用于整次调用。条件档未写的费率取默认档的值，因此叠加多个条件的档应写全费率。

**费率键**：`input`、`output`、`cache_read`、`cache_write_5m`、`cache_write_1h`；非 token 条目只有 `per_unit`。费率全为 0 的档必须填写 `zero_reason`。

**条件**：

| 条件 | 写法 | 含义 |
|---|---|---|
| `input_tokens` | `{"gt": N}` 或 `{"gte": N}` | 本次调用的全部输入 token（含缓存读写） |
| `time_window` | `{"tz": "Asia/Shanghai", "hours": ["HH:MM-HH:MM"], "weekdays": [1, 2, 3, 4, 5]}` | 调用开始时间落在时段内；时段可跨零点，`weekdays` 可省略 |
| `service_tier` | `{"in": ["priority"]}` | 请求的服务档位 |
| `reasoning` | `{"used": true}` | 本次调用产生了思考 token |

## 更新

GitHub Actions 每天运行同步任务：拉取上游最新价目，与 `curated.json` 合并并校验；价目有变化时开 PR，PR 正文列出新增、删除和改价的条目，合并后生效。修改 `curated.json` 并推送到 `main` 时也会触发同步。

本地构建与测试只需 Python 3.12 标准库：

```bash
python3 scripts/build_pricing.py
python3 scripts/validate_pricing.py pricing/model-prices.json
python3 -m unittest discover -s tests
```

构建脚本会调用 GitHub API 查询上游提交，设置 `GH_TOKEN` 或 `GITHUB_TOKEN` 可避免匿名访问限流。

## 致谢与许可

价目数据有很大一部分来自以下开源项目，感谢它们的维护者持续跟进各厂商的价格：

- [langfuse/langfuse](https://github.com/langfuse/langfuse)，MIT 许可，Copyright (c) 2023-2026 ClickHouse, Inc.
- [BerriAI/litellm](https://github.com/BerriAI/litellm)，MIT 许可，Copyright (c) 2023 Berri AI

本仓库按自身格式转换并选用了其中的条目，原版权与许可声明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
