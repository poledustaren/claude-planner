"""Граф кода (codegraph CLI): кто использует файл. Работает, только если в проекте уже есть индекс `.codegraph`
(один раз `codegraph init -y <проект>`); без индекса или без утилиты все функции молча возвращают пусто.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess

_USED = re.compile(r"used by \d+ files?: (.+?)\s*$")


def available(root: str) -> bool:
    return os.path.isdir(os.path.join(root, ".codegraph")) and shutil.which("codegraph") is not None


def _run(*args: str, timeout: int = 30) -> str:
    try:
        return subprocess.run(["codegraph", *args], capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def dependents(root: str, rel: str) -> list[str]:
    """Файлы, которые используют символы файла rel (первая строка `codegraph node -f <файл> --symbols-only`)."""
    match = _USED.search(_run("node", "-p", root, "-f", rel, "--symbols-only").split("\n", 1)[0])
    return [p.strip() for p in match.group(1).split(",") if "/" in p or "." in p] if match else []


def links(root: str, files: list[str], cap: int = 24) -> dict[str, list[str]]:
    """{файл: кто его использует} для файлов пунктов. Перед запросами индекс досинхронизируется."""
    if not files or not available(root):
        return {}
    _run("sync", root, timeout=90)
    out = {}
    for rel in list(dict.fromkeys(files))[:cap]:
        users = dependents(root, rel)
        if users:
            out[rel] = users
    return out
