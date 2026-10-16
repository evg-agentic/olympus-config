"""Behaviour of the validator, the upstream converters and the PR summary.

    python3 -m unittest discover -s tests
"""

from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from build_pricing import convert_langfuse, litellm_tiers  # noqa: E402
from pricing_diff import summarize  # noqa: E402
from validate_pricing import CatalogError, validate  # noqa: E402


def entry(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "m",
        "match": "^m$",
        "currency": "USD",
        "examples": ["m"],
        "tiers": [{"name": "标准", "rates": {"input": 1, "output": 2}}],
    }
    return {**base, **overrides}


def catalog(*models: dict[str, Any]) -> dict[str, Any]:
    return {"schema_version": 1, "models": list(models)}


class ValidatorTest(unittest.TestCase):
    def assert_rejected(self, cat: dict[str, Any], fragment: str) -> None:
        with self.assertRaises(CatalogError) as caught:
            validate(cat)
        self.assertIn(fragment, str(caught.exception))

    def test_published_catalog_is_valid(self) -> None:
        validate(json.loads((ROOT / "pricing" / "model-prices.json").read_text(encoding="utf-8")))

    def test_nested_repeat_is_refused(self) -> None:
        self.assert_rejected(catalog(entry(match="^(m+)+$")), "catastrophic backtracking")

    def test_zero_rate_needs_a_reason(self) -> None:
        free = entry(tiers=[{"name": "标准", "rates": {"input": 0, "output": 0}}])
        self.assert_rejected(catalog(free), "zero_reason")
        free["tiers"][0]["zero_reason"] = "厂商免费"
        validate(catalog(free))

    def test_exactly_one_default_tier(self) -> None:
        two = entry(tiers=[{"name": "a", "rates": {"input": 1, "output": 1}}, {"name": "b", "rates": {"input": 1, "output": 1}}])
        self.assert_rejected(catalog(two), "exactly one tier")

    def test_time_window_needs_a_real_zone(self) -> None:
        tiers = [
            {"name": "标准", "rates": {"input": 1, "output": 1}},
            {"name": "夜间", "priority": 10, "when": [{"time_window": {"tz": "Mars/Base", "hours": ["01:00-02:00"]}}], "rates": {"input": 2}},
        ]
        self.assert_rejected(catalog(entry(tiers=tiers)), "IANA")

    def test_reasoning_condition_takes_only_used_true(self) -> None:
        def with_condition(condition: dict[str, Any]) -> dict[str, Any]:
            return catalog(entry(tiers=[
                {"name": "标准", "rates": {"input": 1, "output": 2}},
                {"name": "思考模式", "priority": 10, "when": [condition], "rates": {"input": 1, "output": 8}},
            ]))

        validate(with_condition({"reasoning": {"used": True}}))
        self.assert_rejected(with_condition({"reasoning": {"used": False}}), "reasoning takes exactly")

    def test_unknown_rate_key_is_refused(self) -> None:
        self.assert_rejected(catalog(entry(tiers=[{"name": "标准", "rates": {"input": 1, "output": 1, "audio": 3}}])), "unknown rate keys")

    def test_a_wide_pattern_cannot_steal_a_later_example(self) -> None:
        wide = entry(id="wide", match="m", examples=["mm"])
        self.assert_rejected(catalog(wide, entry()), "taken by earlier entry 'wide'")


class LangfuseConversionTest(unittest.TestCase):
    def test_tiers_convert_and_priority_is_inverted(self) -> None:
        source = {
            "modelName": "gpt-x",
            "matchPattern": "(?i)^(openai/)?(gpt-x)$",
            "updatedAt": "2026-10-01T00:00:00.000Z",
            "pricingTiers": [
                {"name": "Standard", "isDefault": True, "priority": 0, "conditions": [],
                 "prices": {"input": 1e-06, "output": 2e-06, "input_cache_read": 1e-07, "cache_read_input_tokens": 1e-07}},
                {"name": "Flex · Large context (>272K)", "isDefault": False, "priority": 1,
                 "conditions": [{"source": "model_parameters", "key": "service_tier", "operator": "in", "values": ["flex"]},
                                {"usageDetailPattern": "(input|cache_write)", "operator": "gt", "value": 272000, "caseSensitive": False}],
                 "prices": {"input": 1e-06, "output": 1e-06}},
                {"name": "Large Context (>272K)", "isDefault": False, "priority": 2,
                 "conditions": [{"usageDetailPattern": "(input|prompt|cached)", "operator": "gt", "value": 272000, "caseSensitive": False}],
                 "prices": {"input": 2e-06, "output": 4e-06}},
            ],
        }
        converted = convert_langfuse(source)
        default, combo, long_context = converted["tiers"]
        self.assertEqual(default["rates"], {"input": 1.0, "output": 2.0, "cache_read": 0.1})
        # langfuse tries priority 1 first; ours picks the highest number.
        self.assertGreater(combo["priority"], long_context["priority"])
        self.assertEqual(combo["when"], [{"service_tier": {"in": ["flex"]}}, {"input_tokens": {"gt": 272000}}])
        self.assertEqual(long_context["name"], "长上下文 (>272K)")
        validate(catalog(converted))

    def test_embedding_priced_on_total_is_input(self) -> None:
        source = {
            "modelName": "text-embedding-3-small", "matchPattern": "(?i)^(text-embedding-3-small)$",
            "updatedAt": "2026-10-01T00:00:00.000Z",
            "pricingTiers": [{"name": "Standard", "isDefault": True, "priority": 0, "conditions": [], "prices": {"total": 2e-08}}],
        }
        self.assertEqual(convert_langfuse(source)["tiers"][0]["rates"], {"input": 0.02, "output": 0.0})


class LitellmConversionTest(unittest.TestCase):
    def test_threshold_and_service_tier_fields_become_tiers(self) -> None:
        info = {
            "input_cost_per_token": 1e-06, "output_cost_per_token": 2e-06,
            "input_cost_per_token_above_200k_tokens": 2e-06,
            "input_cost_per_token_priority": 3e-06,
            "input_cost_per_token_above_200k_tokens_priority": 4e-06,
        }
        tiers = litellm_tiers(info, "x")
        by_name = {t["name"]: t for t in tiers}
        self.assertEqual(by_name["长上下文 >200K"]["when"], [{"input_tokens": {"gt": 200000}}])
        both = by_name["长上下文 >200K · priority"]
        self.assertEqual(both["priority"], max(t.get("priority", 0) for t in tiers))
        self.assertEqual(both["rates"], {"input": 4.0})


class SummaryTest(unittest.TestCase):
    def test_reprice_removal_and_addition_are_listed(self) -> None:
        before = catalog(entry(id="a", match="^a$", examples=["a"]), entry(id="b", match="^b$", examples=["b"]))
        before["sources"] = [{"name": "langfuse", "repo": "langfuse/langfuse", "path": "p", "commit": "1" * 40}]
        after = copy.deepcopy(before)
        after["models"][0]["tiers"][0]["rates"]["input"] = 3
        after["models"].pop(1)
        after["models"].append(entry(id="c", match="^c$", examples=["c"]))
        after["sources"][0]["commit"] = "2" * 40
        text = summarize(before, after, [])
        self.assertIn("新增 1，删除 1，变更 1", text)
        self.assertIn("`a`：默认档 USD input 1, output 2 → USD input 3, output 2", text)
        self.assertIn("compare/111111111111...222222222222", text)


if __name__ == "__main__":
    unittest.main()
