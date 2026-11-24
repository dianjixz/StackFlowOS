"""Shared readers for the Astryx console frontend tests.

The console sources are TypeScript/TSX; the Python suite never runs them, so
these helpers parse the files textually (dictionary keys, literal `t()` calls,
screen markers) instead of depending on a Node toolchain.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEBUI = ROOT / "webui"
SRC = WEBUI / "src"
SCREENS = SRC / "screens"
KEYS_DIR = SRC / "i18n" / "keys"

_KEY_RE = re.compile(r"'([^']+)'\s*:")
_LITERAL_T_RE = re.compile(r"\bt\(\s*'([^']+)'")


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def split_pair(text: str) -> tuple[str, str]:
    """Split a dictionary module at its `export const en` marker."""
    zh_part, _, en_part = text.partition("export const en")
    return zh_part, en_part


def keys_in(text: str) -> set[str]:
    return set(_KEY_RE.findall(text))


def dictionary_module_keys(path: Path) -> tuple[set[str], set[str]]:
    zh_part, en_part = split_pair(read(path))
    return keys_in(zh_part), keys_in(en_part)


def merged_keys() -> set[str]:
    """Every key the UI can translate: legacy dict + shell extra + screens."""
    data = json.loads(read(SRC / "i18n" / "dict.json"))
    keys = set(data["zh"]) | set(data["en"])
    for path in [SRC / "i18n" / "extra.ts", *sorted(KEYS_DIR.glob("*.ts"))]:
        zh_part, en_part = split_pair(read(path))
        keys |= keys_in(zh_part) | keys_in(en_part)
    return keys


def literal_t_keys() -> dict[str, set[str]]:
    """`t('key', ...)` literals used outside the dictionaries."""
    found: dict[str, set[str]] = {}
    for path in sorted(list(SRC.rglob("*.ts")) + list(SRC.rglob("*.tsx"))):
        if "i18n" in path.parts:
            continue
        for key in _LITERAL_T_RE.findall(read(path)):
            found.setdefault(key, set()).add(str(path.relative_to(SRC)))
    return found


def screen_text(*screens: str) -> str:
    """Concatenated sources of one or more screen folders."""
    for name in screens:
        folder = SCREENS / name
        if not folder.is_dir():
            raise AssertionError(f"missing screen folder: {folder}")
    parts = []
    for name in screens:
        for path in sorted((SCREENS / name).rglob("*")):
            if path.suffix in {".ts", ".tsx", ".css"}:
                parts.append(read(path))
    return "\n".join(parts)


def i18n_keys(*screens: str) -> set[str]:
    """Keys contributed by a screen's own dictionary file."""
    keys: set[str] = set()
    for name in screens:
        path = KEYS_DIR / f"{name}.ts"
        if path.is_file():
            zh_part, en_part = split_pair(read(path))
            keys |= keys_in(zh_part) | keys_in(en_part)
    return keys
