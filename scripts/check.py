"""Все проверки качества одной командой: `uv run python scripts/check.py`.

Флаги:
  --fast  без pytest (линтер, формат, типы, миграции — секунды вместо минут)
  --fix   ruff сам чинит то, что умеет, и форматирует код

Шаги идут до конца даже после падения одного из них — в итоге видно всё сразу.
Код выхода 1, если упал хотя бы один шаг (для CI и pre-push).
"""

# ПОЧЕМУ: скрипт на Python, а не .sh/.ps1 — одинаково работает в PowerShell,
# Git Bash и в CI на Linux; инструменты берутся из того же окружения uv

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable


@dataclass(frozen=True)
class Step:
    name: str
    cmd: list[str]


def build_steps(*, fast: bool, fix: bool) -> list[Step]:
    if fix:
        lint = Step("ruff check --fix", [PY, "-m", "ruff", "check", "--fix", "."])
        fmt = Step("ruff format", [PY, "-m", "ruff", "format", "."])
    else:
        lint = Step("ruff check", [PY, "-m", "ruff", "check", "."])
        fmt = Step("ruff format --check", [PY, "-m", "ruff", "format", "--check", "."])

    steps = [
        lint,
        fmt,
        Step("mypy", [PY, "-m", "mypy", "."]),
        # ПОЧЕМУ: ловит изменённую модель без миграции — иначе всплывёт только на проде
        Step(
            "makemigrations --check",
            [PY, "manage.py", "makemigrations", "--check", "--dry-run"],
        ),
    ]
    if not fast:
        steps.append(Step("pytest", [PY, "-m", "pytest", "-q"]))
    return steps


def run(step: Step) -> tuple[bool, float]:
    print(f"\n=== {step.name} ===", flush=True)
    started = time.monotonic()
    code = subprocess.call(step.cmd, cwd=ROOT)
    return code == 0, time.monotonic() - started


def main() -> int:
    # ПОЧЕМУ: в консоли Windows при перенаправлении вывода кодировка не UTF-8
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Проверки качества бэкенда")
    parser.add_argument("--fast", action="store_true", help="пропустить pytest")
    parser.add_argument("--fix", action="store_true", help="ruff чинит и форматирует")
    args = parser.parse_args()

    results = [(step, *run(step)) for step in build_steps(fast=args.fast, fix=args.fix)]

    print("\n=== Итог ===")
    for step, ok, seconds in results:
        mark = "OK  " if ok else "FAIL"
        print(f"  {mark}  {step.name:<26} {seconds:6.1f} с")

    failed = [step.name for step, ok, _ in results if not ok]
    if failed:
        print(f"\nУпало: {', '.join(failed)}")
        return 1
    print("\nВсё зелёное.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
