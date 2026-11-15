"""Какой проект Plane ведёт папку: одно правило на оба планера (claude-planner и agy-planner).

Порядок: явная привязка папки → временная папка (в Plane не ведётся) → таблица соответствий →
совпадение имени с проектом Plane → общий проект по умолчанию. Имена берутся у репозитория, самой
папки и папок выше неё (~/work/fers-perenos/src — это fers, а не «src»). Имя папки само проект не заводит:
новый проект создаётся только если он объявлен в таблице, а в Plane его ещё нет.

Таблица — ~/.config/plane-projects.json (или $PLANE_PROJECTS_FILE), запасная — рядом с модулем:
  {"default": "исследования", "skip": ["*/scratch/*"],
   "projects": {"fers": {"aliases": ["fers-*"], "paths": ["~/work/fers-migration"]}, "ops": ["infra"]}}
Ключ — имя проекта в Plane; aliases — имена папок и репозиториев (можно со «*»), paths — папки целиком.
"create_unknown": true возвращает прежнее поведение (незнакомая папка заводит проект по своему имени).

Модуль без зависимостей и одинаков в обоих планерах (claude-planner: planner/projects.py,
agy-planner: mcp/plane_projects.py) — правь оба сразу.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
import tempfile

TABLE_ENV = "PLANE_PROJECTS_FILE"
TABLE_FILES = ("~/.config/plane-projects.json",
               os.path.join(os.path.dirname(os.path.abspath(__file__)), "plane-projects.json"))
# Папки, из которых планы в Plane не идут: тестовые прогоны и черновики. Тесты планера обнуляют.
SCRATCH_ROOTS = ("/tmp", "/var/tmp", "/dev/shm", "~/.cache", tempfile.gettempdir())
DEFAULT_PROJECT = "исследования"


def norm(text) -> str:
    return re.sub(r"[^a-z0-9а-яё]", "", str(text or "").casefold())


def _norm_pattern(text) -> str:
    return re.sub(r"[^a-z0-9а-яё*]", "", str(text or "").casefold())


def load_table(env: dict | None = None) -> dict:
    """Первая читаемая таблица: из окружения, из ~/.config, запасная из репозитория планера."""
    environ = os.environ if env is None else env
    for path in (environ.get(TABLE_ENV, ""), *TABLE_FILES):
        if not path:
            continue
        try:
            with open(os.path.expanduser(path), encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            return data
    return {}


def _real(path: str) -> str:
    return os.path.realpath(os.path.expanduser(str(path)))


def _under(path: str, parent: str) -> bool:
    return path == parent or path.startswith(parent.rstrip("/") + "/")


def is_scratch(root: str, table: dict | None = None) -> bool:
    """Временная папка: /tmp, кэш и то, что перечислено в skip таблицы."""
    real = _real(root)
    if any(_under(real, _real(r)) for r in SCRATCH_ROOTS if r):
        return True
    for pat in (table or {}).get("skip") or []:
        full = os.path.expanduser(str(pat))
        if fnmatch.fnmatch(real, full) or (os.path.isabs(full) and _under(real, _real(full))):
            return True
    return False


def _git(root: str, *args: str) -> str:
    try:
        out = subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def _parents(real: str, limit: int = 3) -> list[str]:
    """Имена папок выше: ~/work/fers-perenos/src — это fers-perenos, а не «src».

    Общие папки (домашняя, ~/work, /mnt/datadisk/Projects) именами проектов не считаются."""
    home, out, cur = _real("~"), [], os.path.dirname(real)
    while len(out) < limit:
        depth = len([part for part in cur.split("/") if part])
        if _under(cur, home):
            if cur == home or os.path.dirname(cur) == home:
                break
        elif depth < 4:
            break
        out.append(os.path.basename(cur))
        cur = os.path.dirname(cur)
    return out


def names(root: str) -> list[str]:
    """Имена-кандидаты по убыванию веса: основной репозиторий рабочего дерева, origin, сама папка, папки выше."""
    real, out = _real(root), []
    common = _git(real, "rev-parse", "--git-common-dir")
    if common:
        common = os.path.realpath(common if os.path.isabs(common) else os.path.join(real, common))
        main = os.path.dirname(common) if os.path.basename(common) == ".git" else re.sub(r"\.git$", "", common)
        # репозиторий в домашней папке (dotfiles) именем проекта не считается
        if not _under(_real("~"), main):
            out.append(os.path.basename(main))
    origin = _git(real, "config", "--get", "remote.origin.url")
    if origin:
        out.append(re.sub(r"\.git$", "", re.split(r"[/:]", origin.rstrip("/"))[-1]))
    out.append(os.path.basename(real))
    out.extend(_parents(real))
    seen, uniq = set(), []
    for name in out:
        if name and norm(name) not in seen:
            seen.add(norm(name))
            uniq.append(name)
    return uniq


def _entries(table: dict) -> list[tuple[str, list[str], list[str]]]:
    out = []
    for name, spec in ((table or {}).get("projects") or {}).items():
        if isinstance(spec, dict):
            out.append((str(name), [str(a) for a in spec.get("aliases") or []], [str(p) for p in spec.get("paths") or []]))
        else:
            out.append((str(name), [str(a) for a in spec or []], []))
    return out


def lookup(root: str, candidates: list[str], table: dict) -> str:
    """Имя проекта Plane из таблицы: по папке целиком, по точному имени, по шаблону (самый длинный)."""
    real, entries = _real(root), _entries(table)
    for name, _aliases, paths in entries:
        for pat in paths:
            full = os.path.expanduser(pat)
            if fnmatch.fnmatch(real, full) or _under(real, _real(full)):
                return name
    for cand in candidates:
        key = norm(cand)
        if not key:
            continue
        for name, aliases, _paths in entries:
            if key == norm(name) or any("*" not in a and key == norm(a) for a in aliases):
                return name
        best = ("", "")
        for name, aliases, _paths in entries:
            for alias in aliases:
                pat = _norm_pattern(alias)
                if "*" in pat and pat.strip("*") and fnmatch.fnmatchcase(key, pat) and len(pat) > len(best[0]):
                    best = (pat, name)
        if best[1]:
            return best[1]
    return ""


def find(projects: list, name: str) -> dict | None:
    """Живой (не архивный) проект Plane по имени или идентификатору."""
    key = norm(name)
    if not key:
        return None
    return next((p for p in projects or [] if not p.get("archived_at")
                 and key in (norm(p.get("name")), norm(p.get("identifier")))), None)


def resolve(root: str, projects: list, table: dict | None = None) -> dict:
    """Проект Plane для папки: {'rule', 'project', 'name'}.

    rule: skip — временная папка, Plane не нужен; table — проект из таблицы; declared — объявлен в
    таблице, но в Plane его нет (project=None, можно создать с именем name); name — имя папки или
    репозитория совпало с проектом; default — общий проект; none — определить не удалось.
    """
    table = load_table() if table is None else table
    if is_scratch(root, table):
        return {"rule": "skip", "project": None, "name": ""}
    candidates = names(root)
    declared = lookup(root, candidates, table)
    if declared:
        hit = find(projects, declared)
        return {"rule": "table" if hit else "declared", "project": hit, "name": declared}
    for cand in candidates:
        hit = find(projects, cand)
        if hit:
            return {"rule": "name", "project": hit, "name": str(hit.get("name") or cand)}
    if table.get("create_unknown") and candidates:  # прежнее поведение, только по явному флагу таблицы
        return {"rule": "declared", "project": None, "name": candidates[0]}
    default = str(table.get("default") if "default" in table else DEFAULT_PROJECT)
    hit = find(projects, default)
    return {"rule": "default" if hit else "none", "project": hit, "name": default}
