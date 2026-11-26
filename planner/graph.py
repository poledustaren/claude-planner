"""Граф кода (codegraph CLI): кто использует файл. Работает, только если в проекте уже есть индекс `.codegraph`
(один раз `codegraph init -y <проект>`); без индекса или без утилиты все функции молча возвращают пусто.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
import re
import shutil
import subprocess
import time

_USED = re.compile(r"used by \d+ files?: (.+?)\s*$")
SYNC_TTL = 300.0
CACHE_TTL = 60.0
_LAST_SYNC: dict[str, float] = {}
_CACHE: dict[tuple[str, tuple[str, ...]], tuple[float, dict[str, list[str]]]] = {}


def available(root: str) -> bool:
    return os.path.isdir(os.path.join(root, ".codegraph")) and shutil.which("codegraph") is not None


def _run(*args: str, timeout: int = 10) -> str:
    try:
        return subprocess.run(["codegraph", *args], capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _maybe_async_sync(root: str) -> None:
    """Досинхронизация индекса в фоне не чаще раза в SYNC_TTL, без блокировки plan_init/plan_run."""
    now = time.monotonic()
    if now - _LAST_SYNC.get(root, 0.0) < SYNC_TTL:
        return
    _LAST_SYNC[root] = now
    try:
        subprocess.Popen(["codegraph", "sync", root], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        pass


def dependents(root: str, rel: str) -> list[str]:
    """Файлы, которые используют символы файла rel (первая строка `codegraph node -f <файл> --symbols-only`)."""
    match = _USED.search(_run("node", "-p", root, "-f", rel, "--symbols-only").split("\n", 1)[0])
    return [p.strip() for p in match.group(1).split(",") if "/" in p or "." in p] if match else []


def links(root: str, files: list[str], cap: int = 24) -> dict[str, list[str]]:
    """{файл: кто его использует} для файлов пунктов. Индекс читается сразу; sync — асинхронно в фоне."""
    if not files or not available(root):
        return {}
    uniq = tuple(list(dict.fromkeys(files))[:cap])
    now = time.monotonic()
    cached = _CACHE.get((root, uniq))
    if cached and now - cached[0] < CACHE_TTL:
        return dict(cached[1])
    _maybe_async_sync(root)
    out: dict[str, list[str]] = {}
    with ThreadPoolExecutor(max_workers=min(4, len(uniq))) as pool:
        for rel, users in zip(uniq, pool.map(lambda r: dependents(root, r), uniq)):
            if users:
                out[rel] = users
    _CACHE[(root, uniq)] = (now, out)
    if len(_CACHE) > 64:
        _CACHE.clear()
    return dict(out)
