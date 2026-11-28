#!/data/data/com.termux/files/usr/bin/env python
"""
Sort and merge sections in TOML/gitconfig-like config files.

Sections are the names inside [...] (section names). Duplicate sections are merged; when the same key has different values, the user is prompted to either keep one value or rename the duplicate section (so both survive).

Usage:
    python3 sort_config.py <config-file> The file is updated in place.

"""

from __future__ import annotations
from collections import OrderedDict
import re
import sys
from typing import Any, Iterable


SECTION_RE = re.compile(r"^\s*\[([^\]]+)\]\s*(.*)$")
KV_RE = re.compile(r"^(\s*)([^=\s]+)\s*=\s*(.*)$")


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------
def split_value_comment(rest: str):
    """Split 'value  # comment' into ('value', '# comment') respecting quotes."""
    in_quote = None
    for i, ch in enumerate(rest):
        if in_quote:
            if ch == in_quote:
                in_quote = None
        elif ch in ('"', "'"):
            in_quote = ch
        elif ch in ("#", ";"):
            return rest[:i].rstrip(), rest[i:]
    return rest.rstrip(), ""


def parse_file(path: str):
    with open(path) as f:
        lines = f.readlines()

    sections = []
    current = {"name": None, "items": [], "comments": [], "trailing": ""}
    pending = []

    for raw in lines:
        line = raw.rstrip("\n")
        m = SECTION_RE.match(line)
        if m:
            if current["items"] or current["name"] is not None or current["comments"]:
                sections.append(current)
            current = {
                "name": m.group(1).strip(),
                "items": [],
                "comments": pending,
                "trailing": m.group(2).strip(),
            }
            pending = []
            continue
        if not line.strip():
            pending.append("")
            continue
        if line.lstrip().startswith(("#", ";")):
            pending.append(line)
            continue
        m = KV_RE.match(line)
        if m:
            indent, key, rest = m.groups()
            value, comment = split_value_comment(rest)
            current["items"].append(
                {
                    "key": key,
                    "value": value,
                    "comment": comment,
                    "leading": pending,
                    "indent": indent,
                }
            )
            pending = []
            continue
        pending.append(line)

    if current["items"] or current["name"] is not None or current["comments"]:
        sections.append(current)

    return sections


# --------------------------------------------------------------------------
# User interaction
# --------------------------------------------------------------------------
def ask_section_action(section: str, num_conflicts: str) -> str:
    print()
    print(
        f"Section [{section}] appears multiple times with {num_conflicts} conflicting key(s)."
    )
    print("  (m) Merge   - resolve conflicts per key")
    print("  (r) Rename  - keep this whole section under a new name")
    while True:
        c = input("Choose [m/r]: ").strip().lower()
        if c in ("m", "merge"):
            return "merge"
        if c in ("r", "rename"):
            return "rename"


def ask_conflict(section: str, key: str, v1: str, v2: str) -> str:
    print()
    print(f"  Conflict in [{section}] for '{key}':")
    print(f"    (1) {v1}")
    print(f"    (2) {v2}")
    while True:
        c = input("  Keep [1/2]: ").strip()
        if c == "1":
            return "first"
        if c == "2":
            return "second"


# --------------------------------------------------------------------------
# Merging
# --------------------------------------------------------------------------
def merge_sections(sections: Iterable[Any]):
    groups = OrderedDict()
    for sec in sections:
        groups.setdefault(sec["name"], []).append(sec)

    used_names = {s["name"] for s in sections if s["name"] is not None}
    result = []

    for name, secs in groups.items():
        if len(secs) == 1:
            result.append(secs[0])
            continue

        merged = {
            "name": name,
            "items": list(secs[0]["items"]),
            "comments": secs[0]["comments"],
            "trailing": secs[0].get("trailing", ""),
        }
        extras = []

        for idx in range(1, len(secs)):
            sec = secs[idx]
            merged_map = {it["key"]: it for it in merged["items"]}
            conflicts = [
                it
                for it in sec["items"]
                if it["key"] in merged_map
                and merged_map[it["key"]]["value"] != it["value"]
            ]

            if conflicts:
                action = ask_section_action(name or "(top-level)", len(conflicts))
                if action == "rename":
                    base = name or "section"
                    new_name = base + "_1"
                    n = 2
                    while new_name in used_names:
                        new_name = f"{base}_{n}"
                        n += 1
                    used_names.add(new_name)
                    extras.append(
                        {
                            "name": new_name,
                            "items": list(sec["items"]),
                            "comments": sec["comments"],
                            "trailing": sec.get("trailing", ""),
                        }
                    )
                    continue

            for it in sec["items"]:
                k = it["key"]
                if k in merged_map:
                    if merged_map[k]["value"] != it["value"]:
                        choice = ask_conflict(
                            name or "(top-level)",
                            k,
                            merged_map[k]["value"],
                            it["value"],
                        )
                        if choice == "second":
                            for j, m in enumerate(merged["items"]):
                                if m["key"] == k:
                                    merged["items"][j] = it
                                    break
                            merged_map[k] = it
                else:
                    merged["items"].append(it)
                    merged_map[k] = it

        result.append(merged)
        result.extend(extras)

    return result


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------
def detect_indent(sections: Iterable[Any]):
    for sec in sections:
        for item in sec["items"]:
            if item.get("indent"):
                return item["indent"]
    return "    "


def write_file(path: str, sections: Iterable[Any], indent: int) -> None:
    toplevel = [s for s in sections if s["name"] is None]
    named = [s for s in sections if s["name"] is not None]

    named.sort(key=lambda s: s["name"])
    for sec in named:
        sec["items"].sort(key=lambda i: i["key"])
    for sec in toplevel:
        sec["items"].sort(key=lambda i: i["key"])

    out = []

    for sec in toplevel:
        for item in sec["items"]:
            for c in item.get("leading", []):
                out.append(c)
            line = f"{indent}{item['key']} = {item['value']}"
            if item["comment"]:
                line += "  " + item["comment"]
            out.append(line)

    first = not out
    for sec in named:
        if not first:
            out.append("")
        first = False
        for c in sec.get("comments", []):
            out.append(c)
        header = f"[{sec['name']}]"
        if sec.get("trailing"):
            header += " " + sec["trailing"]
        out.append(header)
        for item in sec["items"]:
            for c in item.get("leading", []):
                out.append(c)
            line = f"{indent}{item['key']} = {item['value']}"
            if item["comment"]:
                line += "  " + item["comment"]
            out.append(line)

    with open(path, "w") as f:
        f.write("\n".join(out))
        if out:
            f.write("\n")


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
def main() -> None:
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <config-file>", file=sys.stderr)
        sys.exit(1)

    path = sys.argv[1]
    sections = parse_file(path)
    sections = merge_sections(sections)
    indent = detect_indent(sections)
    write_file(path, sections, indent)
    print(f"Sorted and merged {path}")


if __name__ == "__main__":
    main()
