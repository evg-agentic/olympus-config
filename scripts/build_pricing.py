#!/usr/bin/env python3
"""Build pricing/model-prices.json from the upstream price tables.

Order of the output (first matching entry wins at pricing time):

1. ``pricing/curated.json`` — hand-maintained: vendors the upstream tables get
   wrong for us (CNY list prices, time-of-day tariffs) and non-token items.
2. langfuse ``default-model-prices.json`` — OpenAI / Anthropic / Google, with
   its match patterns and pricing tiers converted.
3. litellm ``model_prices_and_context_window.json`` — the vendors langfuse does
   not cover (``LITELLM_VENDORS``), grouped into one entry per price.

An upstream entry is dropped when any of its examples already routes to an
earlier entry: the earlier source is the override.

    python3 scripts/build_pricing.py            # fetch upstream, write, validate
    python3 scripts/build_pricing.py --langfuse a.json --litellm b.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from validate_pricing import MAX_PATTERN_LENGTH, validate  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CURATED = ROOT / "pricing" / "curated.json"
OUTPUT = ROOT / "pricing" / "model-prices.json"

LANGFUSE_URL = "https://raw.githubusercontent.com/langfuse/langfuse/main/worker/src/constants/default-model-prices.json"
LITELLM_URL = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"

#: Retired or non-text models langfuse still carries.
LANGFUSE_DROP = re.compile(
    r"^(babbage|davinci|ft:|chat-bison|codechat|code-|textembedding|text-(ada|babbage|curie|davinci|bison|unicorn|embedding-ada)"
    r"|gemini-(1\.|pro$|live)|gpt-3\.5|gpt-4(-|$)|gpt-4\.5|gpt-4o-(audio|realtime)"
    r"|claude-(1|2|instant|3-(opus|sonnet|haiku))|jev$)"
)

#: langfuse persists one price per provider usage-key alias; ours is one key per bucket.
LANGFUSE_RATE_ALIASES: dict[str, tuple[str, ...]] = {
    "input": ("input", "input_tokens", "prompt_token_count", "promptTokenCount", "input_text"),
    "output": ("output", "output_tokens", "candidates_token_count", "candidatesTokenCount", "output_text"),
    "cache_read": ("input_cache_read", "cache_read_input_tokens", "input_cached_tokens", "cached_content_token_count"),
    "cache_write_5m": ("input_cache_creation_5m", "input_cache_creation", "cache_creation_input_tokens", "cache_write_tokens"),
    "cache_write_1h": ("input_cache_creation_1h",),
}

#: Usage-detail patterns langfuse uses for "the whole input" thresholds.
LANGFUSE_INPUT_PATTERNS = {"(input|prompt|cached)", "(input|cache_write)", "(input)"}

#: Request attributes langfuse conditions on; Olympus reports both as service_tier.
LANGFUSE_TIER_KEYS = {"service_tier", "speed"}


@dataclass(frozen=True)
class LitellmVendor:
    #: The vendor prefix a model id may carry (``xai/grok-4.7``).
    prefix: str
    include: str
    exclude: str | None = None
    #: How litellm keys this vendor's rows when that differs from ``prefix``
    #: (OpenAI's own rows carry no prefix at all).
    key_prefix: str | None = None

    @property
    def litellm_prefix(self) -> str:
        return self.prefix if self.key_prefix is None else self.key_prefix


LITELLM_VENDORS: dict[str, LitellmVendor] = {
    # Fills what langfuse lacks (bare gpt-5.6, the codex line); everything
    # langfuse already prices is dropped as overridden.
    "openai": LitellmVendor("openai/", r"^gpt-(5|6)", r"audio|realtime|search|image|transcribe|tts|chat-latest|\d{4}-\d{2}-\d{2}", key_prefix=""),
    "xai": LitellmVendor("xai/", r"^grok-(4\.(3|5|6|7|20)|code-fast|build)", r"beta|experimental|gv2"),
    "moonshot": LitellmVendor("moonshot/", r"^kimi-"),
    "minimax": LitellmVendor("minimax/", r"^MiniMax-M"),
    "zai": LitellmVendor("zai/", r"^glm-(4\.[5-7]|5)"),
    "mistral": LitellmVendor("mistral/", r".", r"^(zai-glm|glm-|labs-|mistral-vibe|voxtral)"),
}

#: litellm row modes that are text generation.
LITELLM_MODES = ("chat", "responses")

LITELLM_SERVICE_TIERS = ("priority", "flex")
LITELLM_ABOVE = re.compile(r"^input_cost_per_token_above_(\d+)k_tokens$")


class BuildError(Exception):
    """An upstream entry cannot be converted faithfully."""


def per_million(value: float) -> float:
    return round(value * 1_000_000, 6)


def entry_id(name: str) -> str:
    slug = re.sub(r"[^a-z0-9._-]+", "-", name.lower()).strip("-")
    return slug[:32].rstrip("-")


def zero_reason_if_free(rates: dict[str, float], source: str) -> dict[str, str]:
    return {"zero_reason": f"{source} 标为免费"} if rates and all(v == 0 for v in rates.values()) else {}


# ---- langfuse ---------------------------------------------------------------------


def langfuse_rates(prices: dict[str, float], where: str) -> dict[str, float]:
    rates: dict[str, float] = {}
    for bucket, aliases in LANGFUSE_RATE_ALIASES.items():
        values = {prices[a] for a in aliases if a in prices}
        if len(values) > 1:
            raise BuildError(f"{where}: aliases of {bucket} disagree: {sorted(values)}")
        if values:
            rates[bucket] = per_million(values.pop())
    return rates


def langfuse_condition(condition: dict[str, Any], where: str) -> dict[str, Any]:
    if "usageDetailPattern" in condition:
        if condition["usageDetailPattern"] not in LANGFUSE_INPUT_PATTERNS or condition["operator"] not in ("gt", "gte"):
            raise BuildError(f"{where}: unsupported usage condition {condition}")
        return {"input_tokens": {condition["operator"]: int(condition["value"])}}
    if condition.get("source") == "model_parameters" and condition.get("key") in LANGFUSE_TIER_KEYS:
        return {"service_tier": {"in": list(condition["values"])}}
    raise BuildError(f"{where}: unsupported condition {condition}")


def convert_langfuse(entry: dict[str, Any]) -> dict[str, Any]:
    name = entry["modelName"]
    tiers_in = entry["pricingTiers"]
    default = next(t for t in tiers_in if t["isDefault"])
    conditional = sorted((t for t in tiers_in if not t["isDefault"]), key=lambda t: t["priority"])
    default_rates = langfuse_rates(default["prices"], name)
    if set(default["prices"]) == {"total"}:
        # Embedding models are priced on one "total" bucket: all of it is input.
        default_rates = {"input": per_million(default["prices"]["total"]), "output": 0.0}
    if "input" not in default_rates or "output" not in default_rates:
        raise BuildError(f"{name}: no input/output price (total-priced legacy entry)")
    tiers: list[dict[str, Any]] = [{"name": "标准", "rates": default_rates, **zero_reason_if_free(default_rates, "langfuse")}]
    # langfuse tries conditional tiers in ASCENDING priority; ours picks the
    # HIGHEST, so the first one langfuse tries gets the largest number.
    for rank, tier in enumerate(conditional):
        rates = langfuse_rates(tier["prices"], f"{name}/{tier['name']}")
        tiers.append({
            "name": langfuse_tier_name(tier["name"]),
            "priority": (len(conditional) - rank) * 10,
            "when": [langfuse_condition(c, f"{name}/{tier['name']}") for c in tier["conditions"]],
            "rates": rates,
            **zero_reason_if_free({**default_rates, **rates}, "langfuse"),
        })
    return {
        "id": entry_id(name),
        "match": entry["matchPattern"],
        "unit": "mtok",
        "currency": "USD",
        "source": f"langfuse default-model-prices.json（{entry['updatedAt'][:10]}）",
        "examples": [langfuse_example(name, entry["matchPattern"])],
        "tiers": tiers,
    }


#: langfuse's tier names, in the words the litellm-built tiers use.
LANGFUSE_TIER_WORDS = (
    ("Large Context", "长上下文"),
    ("Large context", "长上下文"),
    ("Fast mode", "fast/priority"),
    ("Ultrafast", "ultrafast"),
    ("Priority", "priority"),
    ("Flex", "flex"),
)


def langfuse_tier_name(name: str) -> str:
    for english, ours in LANGFUSE_TIER_WORDS:
        name = name.replace(english, ours)
    return name


def langfuse_example(name: str, pattern: str) -> str:
    """The model name as providers send it. A few langfuse rows are named with a
    dot (``claude-3.5-sonnet-latest``) while the API id, and the pattern, use a
    hyphen."""
    if re.search(pattern, name):
        return name
    hyphenated = name.replace(".", "-")
    return hyphenated if re.search(pattern, hyphenated) else name


# ---- litellm ----------------------------------------------------------------------


def litellm_tier_rates(info: dict[str, Any], suffix: str) -> dict[str, float]:
    fields = {
        "input": f"input_cost_per_token{suffix}",
        "output": f"output_cost_per_token{suffix}",
        "cache_read": f"cache_read_input_token_cost{suffix}",
        "cache_write_5m": f"cache_creation_input_token_cost{suffix}",
    }
    rates = {bucket: per_million(info[f]) for bucket, f in fields.items() if info.get(f) is not None}
    if not suffix and info.get("cache_creation_input_token_cost_above_1hr") is not None:
        rates["cache_write_1h"] = per_million(info["cache_creation_input_token_cost_above_1hr"])
    return rates


def litellm_tiers(info: dict[str, Any], where: str) -> list[dict[str, Any]]:
    if info.get("tiered_pricing"):
        raise BuildError(f"{where}: tiered_pricing is not converted")
    default = litellm_tier_rates(info, "")
    if "input" not in default or "output" not in default:
        raise BuildError(f"{where}: no input/output price")
    thresholds = sorted(int(m.group(1)) for k in info if (m := LITELLM_ABOVE.match(k)))
    combos: list[tuple[int, int, str | None, dict[str, float]]] = []
    for threshold in [0, *thresholds]:
        for tier in (None, *LITELLM_SERVICE_TIERS):
            if not threshold and tier is None:
                continue
            suffix = (f"_above_{threshold}k_tokens" if threshold else "") + (f"_{tier}" if tier else "")
            rates = litellm_tier_rates(info, suffix)
            if rates:
                combos.append((bool(threshold) + bool(tier), threshold, tier, rates))
    tiers: list[dict[str, Any]] = [{"name": "标准", "rates": default, **zero_reason_if_free(default, "litellm")}]
    for rank, (_, threshold, tier, rates) in enumerate(sorted(combos, key=lambda c: (c[0], c[1], c[2] or ""))):
        when: list[dict[str, Any]] = []
        names: list[str] = []
        if threshold:
            when.append({"input_tokens": {"gt": threshold * 1000}})
            names.append(f"长上下文 >{threshold}K")
        if tier:
            when.append({"service_tier": {"in": [tier]}})
            names.append(tier)
        tiers.append({"name": " · ".join(names), "priority": (rank + 1) * 10, "when": when, "rates": rates})
    return tiers


def litellm_group_pattern(prefix: str, names: list[str]) -> str:
    return f"(?i)^({re.escape(prefix)})?({'|'.join(re.escape(n) for n in names)})$"


def convert_litellm(
    table: dict[str, Any], today: str, priced_by: Callable[[str], str | None]
) -> tuple[list[dict[str, Any]], list[str]]:
    """One entry per vendor and price. Names an earlier source already prices
    are left out before grouping, so one overridden name never takes its
    same-priced siblings down with it."""
    out: list[dict[str, Any]] = []
    skipped: list[str] = []
    for vendor, spec in LITELLM_VENDORS.items():
        groups: dict[str, tuple[list[dict[str, Any]], list[str]]] = {}
        for key, info in table.items():
            prefix = spec.litellm_prefix
            if not isinstance(info, dict) or info.get("mode") not in LITELLM_MODES:
                continue
            if not key.startswith(prefix) or (not prefix and info.get("litellm_provider") != vendor):
                continue
            name = key[len(prefix):]
            if not re.search(spec.include, name) or (spec.exclude and re.search(spec.exclude, name)):
                continue
            owner = priced_by(name)
            if owner is not None:
                skipped.append(f"{key}: already priced by {owner!r}")
                continue
            if info.get("deprecation_date") and str(info["deprecation_date"]) <= today:
                skipped.append(f"{key}: deprecated {info['deprecation_date']}")
                continue
            try:
                tiers = litellm_tiers(info, key)
            except BuildError as exc:
                skipped.append(str(exc))
                continue
            signature = json.dumps(tiers, sort_keys=True)
            groups.setdefault(signature, (tiers, []))[1].append(name)
        for tiers, names in groups.values():
            names.sort(key=lambda n: (len(n), n))
            chunk: list[str] = []
            for name in names:
                if chunk and len(litellm_group_pattern(spec.prefix, [*chunk, name])) > MAX_PATTERN_LENGTH:
                    out.append(litellm_entry(vendor, spec, chunk, tiers))
                    chunk = []
                chunk.append(name)
            out.append(litellm_entry(vendor, spec, chunk, tiers))
    return out, skipped


def litellm_entry(vendor: str, spec: LitellmVendor, names: list[str], tiers: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": entry_id(f"{vendor}-{names[0]}"),
        "match": litellm_group_pattern(spec.prefix, names),
        "unit": "mtok",
        "currency": "USD",
        "source": f"litellm model_prices_and_context_window.json（{spec.prefix}）",
        "examples": list(names),
        "tiers": tiers,
    }


# ---- assembly ---------------------------------------------------------------------


def load(path: str | None, url: str) -> Any:
    if path:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310 — fixed https URLs
        return json.loads(response.read())


def append_unless_overridden(models: list[dict[str, Any]], entry: dict[str, Any], report: list[str]) -> None:
    pattern = re.compile(entry["match"])
    if not all(pattern.search(e) for e in entry["examples"]):
        report.append(f"{entry['id']}: its own example does not match its pattern, dropped")
        return
    for example in entry["examples"]:
        earlier = next((m for m in models if re.search(m["match"], example)), None)
        if earlier is not None:
            report.append(f"{entry['id']}: {example!r} already priced by {earlier['id']!r}, dropped")
            return
    if any(m["id"] == entry["id"] for m in models):
        entry = {**entry, "id": entry_id(f"{entry['id'][:28]}-{len(models)}")}
    models.append(entry)


def build(langfuse: list[dict[str, Any]], litellm: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    report: list[str] = []
    today = date.today().isoformat()
    models: list[dict[str, Any]] = []
    for entry in json.loads(CURATED.read_text(encoding="utf-8"))["models"]:
        models.append(entry)
    for entry in langfuse:
        if LANGFUSE_DROP.match(entry["modelName"]):
            continue
        try:
            append_unless_overridden(models, convert_langfuse(entry), report)
        except BuildError as exc:
            report.append(f"langfuse skipped {exc}")
    def priced_by(name: str) -> str | None:
        return next((m["id"] for m in models if re.search(m["match"], name)), None)

    converted, skipped = convert_litellm(litellm, today, priced_by)
    report.extend(f"litellm skipped {s}" for s in skipped)
    for entry in converted:
        append_unless_overridden(models, entry, report)
    catalog = {
        "schema_version": 1,
        "updated_at": today,
        "sources": [
            {"name": "curated", "url": "pricing/curated.json"},
            {"name": "langfuse", "url": LANGFUSE_URL},
            {"name": "litellm", "url": LITELLM_URL},
        ],
        "models": models,
    }
    return catalog, report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--langfuse", help="local copy of langfuse default-model-prices.json")
    parser.add_argument("--litellm", help="local copy of litellm model_prices_and_context_window.json")
    args = parser.parse_args()
    catalog, report = build(load(args.langfuse, LANGFUSE_URL), load(args.litellm, LITELLM_URL))
    validate(catalog)
    OUTPUT.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for line in report:
        print(line)
    print(f"wrote {OUTPUT.relative_to(ROOT)}: {len(catalog['models'])} entries")
    return 0


if __name__ == "__main__":
    sys.exit(main())
