#!/usr/bin/env python3
"""Validate a model price catalog against schema_version 1.

The same rules Olympus applies when it syncs the catalog: one bad entry rejects
the whole file, so run this before every push.

    python3 scripts/validate_pricing.py pricing/model-prices.json
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    import re._parser as sre_parse  # Python 3.11+
except ImportError:  # pragma: no cover
    import sre_parse  # type: ignore[no-redef]

SCHEMA_VERSION = 1
MAX_PATTERN_LENGTH = 200
ID_PATTERN = re.compile(r"^[a-z0-9._-]{1,32}$")
HOURS_PATTERN = re.compile(r"^([01]\d|2[0-3]):[0-5]\d-([01]\d|2[0-4]):[0-5]\d$")
UNITS = {"mtok", "request", "image", "credit", "second"}
CURRENCIES = {"USD", "CNY"}
TOKEN_RATES = {"input", "output", "cache_read", "cache_write_5m", "cache_write_1h"}
UNIT_RATES = {"per_unit"}
ENTRY_FIELDS = {"id", "match", "unit", "currency", "tiers", "examples", "source", "note"}
TIER_FIELDS = {"name", "priority", "when", "rates", "zero_reason"}


class CatalogError(Exception):
    """The catalog breaks a schema rule."""


def star_height(pattern: str) -> int:
    """How deeply unbounded repeats nest. Above 1 (``(a+)+``) is the shape that
    backtracks catastrophically, so it is refused like langfuse's safe-regex."""

    def walk(items: Any) -> int:
        height = 0
        for op, arg in items:
            name = str(op)
            if name in ("MAX_REPEAT", "MIN_REPEAT", "POSSESSIVE_REPEAT"):
                low, high, sub = arg
                inner = walk(sub)
                height = max(height, inner + (1 if high > 1 else 0))
            elif name == "SUBPATTERN":
                height = max(height, walk(arg[-1]))
            elif name == "BRANCH":
                height = max(height, *(walk(branch) for branch in arg[1]))
            elif name in ("ASSERT", "ASSERT_NOT"):
                height = max(height, walk(arg[1]))
        return height

    return walk(sre_parse.parse(pattern))


def check_pattern(pattern: object, where: str) -> re.Pattern[str]:
    if not isinstance(pattern, str) or not pattern:
        raise CatalogError(f"{where}: match must be a non-empty string")
    if len(pattern) > MAX_PATTERN_LENGTH:
        raise CatalogError(f"{where}: match longer than {MAX_PATTERN_LENGTH} characters")
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        raise CatalogError(f"{where}: match does not compile: {exc}") from exc
    if star_height(pattern) > 1:
        raise CatalogError(f"{where}: match nests unbounded repeats (catastrophic backtracking)")
    return compiled


def check_condition(condition: object, where: str) -> None:
    if not isinstance(condition, dict) or len(condition) != 1:
        raise CatalogError(f"{where}: a condition is an object with exactly one kind")
    (kind, body), = condition.items()
    if kind == "input_tokens":
        if not isinstance(body, dict) or len(body) != 1 or next(iter(body)) not in ("gt", "gte"):
            raise CatalogError(f"{where}: input_tokens takes exactly one of gt / gte")
        threshold = next(iter(body.values()))
        if not isinstance(threshold, int) or threshold <= 0:
            raise CatalogError(f"{where}: input_tokens threshold must be a positive integer")
    elif kind == "time_window":
        if not isinstance(body, dict) or set(body) - {"tz", "hours", "weekdays"}:
            raise CatalogError(f"{where}: time_window takes tz, hours, weekdays")
        try:
            ZoneInfo(str(body.get("tz")))
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise CatalogError(f"{where}: time_window.tz is not an IANA zone") from exc
        hours = body.get("hours")
        if not isinstance(hours, list) or not hours or not all(
            isinstance(h, str) and HOURS_PATTERN.match(h) for h in hours
        ):
            raise CatalogError(f"{where}: time_window.hours must be a list of HH:MM-HH:MM")
        weekdays = body.get("weekdays")
        if weekdays is not None and (
            not isinstance(weekdays, list)
            or not weekdays
            or not all(isinstance(d, int) and 1 <= d <= 7 for d in weekdays)
        ):
            raise CatalogError(f"{where}: time_window.weekdays must be ISO days 1..7")
    elif kind == "reasoning":
        if body != {"used": True}:
            raise CatalogError(f'{where}: reasoning takes exactly {{"used": true}}')
    elif kind == "service_tier":
        values = body.get("in") if isinstance(body, dict) else None
        if not isinstance(values, list) or not values or not all(isinstance(v, str) and v for v in values):
            raise CatalogError(f"{where}: service_tier takes a non-empty in: [...]")
    else:
        raise CatalogError(f"{where}: unknown condition kind {kind!r}")


def check_rates(rates: object, unit: str, where: str, *, is_default: bool) -> dict[str, float]:
    allowed = TOKEN_RATES if unit == "mtok" else UNIT_RATES
    if not isinstance(rates, dict) or not rates:
        raise CatalogError(f"{where}: rates must be a non-empty object")
    unknown = set(rates) - allowed
    if unknown:
        raise CatalogError(f"{where}: unknown rate keys {sorted(unknown)} for unit {unit}")
    for key, value in rates.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise CatalogError(f"{where}: rate {key} must be a non-negative number")
    if is_default:
        required = {"input", "output"} if unit == "mtok" else {"per_unit"}
        missing = required - set(rates)
        if missing:
            raise CatalogError(f"{where}: default tier is missing {sorted(missing)}")
    return rates


def check_tiers(tiers: object, unit: str, where: str) -> None:
    if not isinstance(tiers, list) or not tiers:
        raise CatalogError(f"{where}: tiers must be a non-empty list")
    defaults = [t for t in tiers if isinstance(t, dict) and not t.get("when")]
    if len(defaults) != 1:
        raise CatalogError(f"{where}: exactly one tier must have no conditions (the default)")
    default_rates = check_rates(defaults[0].get("rates"), unit, f"{where} default tier", is_default=True)
    priorities: set[int] = set()
    for index, tier in enumerate(tiers):
        tier_where = f"{where} tier[{index}]"
        if not isinstance(tier, dict):
            raise CatalogError(f"{tier_where}: must be an object")
        unknown = set(tier) - TIER_FIELDS
        if unknown:
            raise CatalogError(f"{tier_where}: unknown fields {sorted(unknown)}")
        if not isinstance(tier.get("name"), str) or not tier["name"].strip():
            raise CatalogError(f"{tier_where}: name is required")
        rates = tier.get("rates")
        if tier.get("when"):
            if not isinstance(tier["when"], list):
                raise CatalogError(f"{tier_where}: when must be a list")
            for condition in tier["when"]:
                check_condition(condition, tier_where)
            priority = tier.get("priority")
            if not isinstance(priority, int) or isinstance(priority, bool) or priority <= 0:
                raise CatalogError(f"{tier_where}: a conditional tier needs a positive integer priority")
            if priority in priorities:
                raise CatalogError(f"{tier_where}: priority {priority} is not unique in this entry")
            priorities.add(priority)
            check_rates(rates, unit, tier_where, is_default=False)
            effective = {**default_rates, **rates}
        else:
            if "priority" in tier:
                raise CatalogError(f"{tier_where}: the default tier takes no priority")
            effective = default_rates
        if all(value == 0 for value in effective.values()) and not str(tier.get("zero_reason") or "").strip():
            raise CatalogError(f"{tier_where}: an all-zero tier must say why in zero_reason")


def entries(catalog: object) -> Iterator[dict[str, Any]]:
    if not isinstance(catalog, dict):
        raise CatalogError("catalog must be an object")
    if catalog.get("schema_version") != SCHEMA_VERSION:
        raise CatalogError(f"schema_version must be {SCHEMA_VERSION}")
    models = catalog.get("models")
    if not isinstance(models, list) or not models:
        raise CatalogError("models must be a non-empty list")
    yield from models


def validate(catalog: object) -> list[dict[str, Any]]:
    """Every rule, then routing: each example must land on its own entry when
    entries are tried in file order — a pattern written too wide fails here."""
    seen: set[str] = set()
    compiled: list[tuple[dict[str, Any], re.Pattern[str]]] = []
    for position, entry in enumerate(entries(catalog)):
        where = f"models[{position}]"
        if not isinstance(entry, dict):
            raise CatalogError(f"{where}: must be an object")
        entry_id = entry.get("id")
        if not isinstance(entry_id, str) or not ID_PATTERN.match(entry_id):
            raise CatalogError(f"{where}: id must match {ID_PATTERN.pattern}")
        where = f"models[{position}] ({entry_id})"
        if entry_id in seen:
            raise CatalogError(f"{where}: duplicate id")
        seen.add(entry_id)
        unknown = set(entry) - ENTRY_FIELDS
        if unknown:
            raise CatalogError(f"{where}: unknown fields {sorted(unknown)}")
        unit = entry.get("unit", "mtok")
        if unit not in UNITS:
            raise CatalogError(f"{where}: unit must be one of {sorted(UNITS)}")
        if entry.get("currency") not in CURRENCIES:
            raise CatalogError(f"{where}: currency must be one of {sorted(CURRENCIES)}")
        check_tiers(entry.get("tiers"), unit, where)
        examples = entry.get("examples")
        if not isinstance(examples, list) or not examples or not all(isinstance(e, str) and e for e in examples):
            raise CatalogError(f"{where}: examples must list at least one billable key")
        compiled.append((entry, check_pattern(entry.get("match"), where)))

    for entry, _ in compiled:
        for example in entry["examples"]:
            winner = next((e for e, pattern in compiled if pattern.search(example)), None)
            if winner is None:
                raise CatalogError(f"{entry['id']}: example {example!r} matches no entry")
            if winner is not entry:
                raise CatalogError(
                    f"{entry['id']}: example {example!r} is taken by earlier entry {winner['id']!r}"
                )
    return [entry for entry, _ in compiled]


def main(argv: list[str]) -> int:
    path = Path(argv[1] if len(argv) > 1 else "pricing/model-prices.json")
    try:
        models = validate(json.loads(path.read_text(encoding="utf-8")))
    except (CatalogError, json.JSONDecodeError) as exc:
        print(f"INVALID {path}: {exc}", file=sys.stderr)
        return 1
    tiers = sum(len(m["tiers"]) for m in models)
    print(f"OK {path}: {len(models)} entries, {tiers} tiers")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
