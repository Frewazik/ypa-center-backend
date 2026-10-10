"""Собирает locale/*/LC_MESSAGES/*.po в .mo: `uv run python scripts/compile_messages.py`.

Флаг --check: не пишет файлы, а падает, если .mo устарел относительно .po
(для CI и pre-push).
"""

# ПОЧЕМУ свой скрипт, а не manage.py compilemessages: тот вызывает msgfmt из
# GNU gettext, которого нет в Windows. Поддержано только то, что есть в наших
# .po: однострочные и склеенные строки, без plural и msgctxt — остальное
# падает с ошибкой, а не теряется молча

from __future__ import annotations

import argparse
import ast
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_MO_MAGIC = 0x950412DE
_HEADER_SIZE = 7 * 4


def parse_po(path: Path) -> dict[str, str]:
    messages: dict[str, str] = {}
    key: str | None = None
    parts: dict[str, list[str]] = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(("msgid ", "msgstr ")):
            key, _, literal = line.partition(" ")
            if key == "msgid" and parts:
                _store(messages, parts, path)
                parts = {}
        elif line.startswith('"') and key is not None:
            literal = line
        else:
            raise ValueError(f"{path}:{number}: неподдерживаемая строка: {raw!r}")
        parts.setdefault(key, []).append(ast.literal_eval(literal))
    if parts:
        _store(messages, parts, path)
    return messages


def _store(messages: dict[str, str], parts: dict[str, list[str]], path: Path) -> None:
    if set(parts) != {"msgid", "msgstr"}:
        raise ValueError(f"{path}: у записи нет пары msgid/msgstr: {parts}")
    msgid = "".join(parts["msgid"])
    if msgid in messages:
        raise ValueError(f"{path}: msgid повторяется: {msgid!r}")
    messages[msgid] = "".join(parts["msgstr"])


def build_mo(messages: dict[str, str]) -> bytes:
    keys = sorted(messages)
    ids = [key.encode("utf-8") for key in keys]
    strs = [messages[key].encode("utf-8") for key in keys]
    count = len(keys)
    ids_table = _HEADER_SIZE
    strs_table = ids_table + count * 8
    data_start = strs_table + count * 8

    offsets: list[tuple[int, int]] = []
    data = b""
    for chunk in (*ids, *strs):
        offsets.append((len(chunk), data_start + len(data)))
        data += chunk + b"\0"

    header = struct.pack(
        "<7I", _MO_MAGIC, 0, count, ids_table, strs_table, 0, data_start
    )
    tables = b"".join(struct.pack("<2I", *pair) for pair in offsets)
    return header + tables + data


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    stale: list[Path] = []
    for po in sorted(ROOT.glob("locale/*/LC_MESSAGES/*.po")):
        mo = po.with_suffix(".mo")
        compiled = build_mo(parse_po(po))
        if mo.exists() and mo.read_bytes() == compiled:
            continue
        if args.check:
            stale.append(mo)
            continue
        mo.write_bytes(compiled)
        print(f"собран {mo.relative_to(ROOT)}")

    for mo in stale:
        print(f"устарел {mo.relative_to(ROOT)} — запустите scripts/compile_messages.py")
    return 1 if stale else 0


if __name__ == "__main__":
    sys.exit(main())
