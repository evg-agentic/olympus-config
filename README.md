# olympus-config

Olympus 的平台级公共配置。目前只有一份：**模型价目表**。

| 文件 | 说明 |
|---|---|
| `pricing/model-prices.json` | **生成的**价目表，不要手改。Olympus 平台管理员把它的 raw 链接配成"价格源"，平台每天拉取一次 |
| `pricing/curated.json` | **我们自己维护的条目**：上游没有或口径不对的模型（DeepSeek 分时价、百炼人民币价）以及非 token 计价项 |
| `scripts/build_pricing.py` | 拉取上游价目、和手工条目合并，生成 `model-prices.json` |
| `scripts/validate_pricing.py` | 校验价目表。Olympus 同步时用同样的规则，一条不过整份拒收 |
| `scripts/pricing_diff.py` | 生成同步 PR 的变更摘要 |
| `.github/workflows/sync-pricing.yml` | 每日同步任务 |

价格源链接：

```text
https://raw.githubusercontent.com/evg-agentic/olympus-config/main/pricing/model-prices.json
```

## 更新价目

### 自动同步（日常）

GitHub Actions 每天 UTC 06:30（北京 14:30）运行 `sync-pricing.yml`：

1. 拉取 langfuse、litellm 价目文件的最新版本，并记下各自的提交号；
2. 和 `pricing/curated.json` 合并，跑单元测试和校验；
3. 如果生成的价目有变化，就开一个 PR（分支 `bot/sync-model-prices`；已有未合并的 PR 就更新它）。PR 正文列出新增、删除和改价的条目，以及上游版本的对比链接。

上游有提交但我们收录的价格没变时不会开 PR。合并 PR 后，Olympus 在下一次同步时拉到新价目，只影响之后的调用。

也可以在 Actions 页面手动触发同步。

### 改手工条目

直接改 `pricing/curated.json` 并推到 `main`。推送会立刻触发同步任务，重新生成价目并开 PR。想在本地先看效果，可以这样跑：

```bash
GH_TOKEN=$(gh auth token) python3 scripts/build_pricing.py --summary /tmp/pr-body.md
python3 -m unittest discover -s tests
```

只用 Python 标准库，不需要安装依赖。不带 token 也能跑，但 GitHub API 匿名访问容易被限流。

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

## 致谢与许可

价目数据有很大一部分来自下面两个开源项目，感谢它们的维护者长期跟进各家厂商的价格：

- [langfuse/langfuse](https://github.com/langfuse/langfuse)：`worker/src/constants/default-model-prices.json`，MIT 许可，Copyright (c) 2023-2026 ClickHouse, Inc.
- [BerriAI/litellm](https://github.com/BerriAI/litellm)：`model_prices_and_context_window.json`，MIT 许可，Copyright (c) 2023 Berri AI

我们按本仓库的格式转换并选用了其中的条目，原版权和许可声明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
