"""Read the limits and protected folders from bookmark-rules.md.

The file opens with a block of `key: value` lines between two `---` lines. Only that
block is read here; the prose below it is for Claude. `protected` may repeat.
"""

import os

INTEGER_KEYS = ("bar_max_folders", "bar_max_depth")
LIST_KEYS = ("protected",)
ROOT_TOKENS = ("BAR", "OTHER", "MOBILE")
DEFAULT_PATH = "bookmark-rules.md"


class RulesError(Exception):
    pass


def load_rules(path=DEFAULT_PATH):
    if not os.path.exists(path):
        raise RulesError(
            f"no rules file at {path}. Run /setup in Claude Code to write one, "
            "or copy bookmark-rules.example.md and edit it."
        )
    with open(path, encoding="utf-8-sig") as fh:
        return parse_rules(fh.read(), path)


def parse_rules(text, source="rules"):
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise RulesError(f"{source}: must start with a --- line")
    try:
        end = next(i for i, line in enumerate(lines[1:], 1) if line.strip() == "---")
    except StopIteration:
        raise RulesError(f"{source}: the front block has no closing --- line") from None

    rules = {key: [] for key in LIST_KEYS}
    for lineno, line in enumerate(lines[1:end], 2):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, sep, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if not sep or not value:
            raise RulesError(f"{source} line {lineno}: expected 'key: value'")
        if key in INTEGER_KEYS:
            if not value.isdigit() or int(value) < 1:
                raise RulesError(f"{source} line {lineno}: {key} must be a whole number above 0")
            rules[key] = int(value)
        elif key in LIST_KEYS:
            if value.split("/")[0] not in ROOT_TOKENS:
                raise RulesError(
                    f"{source} line {lineno}: {key} path {value!r} must start with "
                    + ", ".join(ROOT_TOKENS)
                )
            rules[key].append(value.rstrip("/"))
        else:
            raise RulesError(f"{source} line {lineno}: unknown key {key!r}")

    missing = [key for key in INTEGER_KEYS if key not in rules]
    if missing:
        raise RulesError(f"{source}: missing {', '.join(missing)}")
    return rules


def is_protected(folder_path, protected):
    """True when folder_path ('BAR/A/B') is a protected folder or lies inside one."""
    return any(folder_path == p or folder_path.startswith(p + "/") for p in protected)
