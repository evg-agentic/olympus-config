"""Describe what changed between two price catalogs, as the body of a sync PR.

A reviewer approves money, so the summary leads with prices: every entry that
was added, removed or re-priced, with the default tier's rates before and after.
"""

from __future__ import annotations

from typing import Any

RATE_ORDER = ("input", "output", "cache_read", "cache_write_5m", "cache_write_1h", "per_unit")


def format_rates(entry: dict[str, Any]) -> str:
    rates = entry["tiers"][0]["rates"]
    shown = ", ".join(f"{key} {rates[key]:g}" for key in RATE_ORDER if key in rates)
    return f"{entry['currency']} {shown}"


def examples_of(entry: dict[str, Any], limit: int = 4) -> str:
    """The model names an entry prices — its id alone hides a merged group."""
    names = entry["examples"]
    shown = ", ".join(names[:limit])
    return shown if len(names) <= limit else f"{shown} 等 {len(names)} 个"


def changed_fields(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    notes: list[str] = []
    if old["tiers"][0]["rates"] != new["tiers"][0]["rates"] or old["currency"] != new["currency"]:
        notes.append(f"默认档 {format_rates(old)} → {format_rates(new)}")
    if old["tiers"][1:] != new["tiers"][1:]:
        notes.append(f"条件档 {len(old['tiers']) - 1} → {len(new['tiers']) - 1} 个（内容有变）")
    if old["match"] != new["match"]:
        notes.append("匹配正则有变")
    if old.get("unit", "mtok") != new.get("unit", "mtok"):
        notes.append(f"单位 {old.get('unit', 'mtok')} → {new.get('unit', 'mtok')}")
    if not notes and old != new:
        notes.append("说明性字段有变（source / note / examples）")
    return notes


def upstream_lines(previous: dict[str, Any] | None, current: dict[str, Any]) -> list[str]:
    before = {s["name"]: s for s in (previous or {}).get("sources", [])}
    lines: list[str] = []
    for source in current["sources"]:
        if "repo" not in source:
            continue
        old_commit = before.get(source["name"], {}).get("commit")
        new_commit = source["commit"]
        if old_commit and old_commit != new_commit and "local" not in (old_commit, new_commit):
            link = f"https://github.com/{source['repo']}/compare/{old_commit[:12]}...{new_commit[:12]}"
            lines.append(f"- {source['name']}：[{old_commit[:8]} → {new_commit[:8]}]({link})")
        else:
            lines.append(f"- {source['name']}：`{new_commit[:8]}`")
    return lines


def summarize(previous: dict[str, Any] | None, current: dict[str, Any], report: list[str]) -> str:
    old = {m["id"]: m for m in (previous or {}).get("models", [])}
    new = {m["id"]: m for m in current["models"]}
    added = [new[i] for i in new if i not in old]
    removed = [old[i] for i in old if i not in new]
    changed = [(old[i], new[i]) for i in new if i in old and old[i] != new[i]]

    out = ["## 模型价目同步", "", f"共 {len(new)} 条：新增 {len(added)}，删除 {len(removed)}，变更 {len(changed)}。", ""]
    out += ["### 上游版本", "", *upstream_lines(previous, current), ""]
    if changed:
        out += ["### 变更（请重点核对）", ""]
        out += [f"- `{n['id']}`：{'；'.join(changed_fields(o, n))}" for o, n in changed]
        out.append("")
    if removed:
        out += ["### 删除（这些模型之后会显示为未计价）", ""]
        out += [f"- `{m['id']}`（{examples_of(m)}）" for m in removed]
        out.append("")
    if added:
        out += ["### 新增", ""]
        out += [f"- `{m['id']}`（{examples_of(m)}）：{format_rates(m)}，{len(m['tiers'])} 档" for m in added]
        out.append("")
    if report:
        out += ["<details><summary>构建日志（被覆盖或跳过的上游条目）</summary>", "", "```text", *report, "```", "</details>", ""]
    out.append("核对无误后合并；合并后 Olympus 在下一次同步时（每日）拉取新价目，只影响之后的调用。")
    return "\n".join(out) + "\n"
