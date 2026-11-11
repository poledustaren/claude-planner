#!/usr/bin/env python3
"""Установка claude-planner в ~/.claude. Всё обратимо: ничего не удаляется, а переносится в бэкап.

  install.py install   MCP-сервер planner, скилл, хуки, команда cplan, панель прогресса plan-board
  install.py rules     минимальный CLAUDE.md вместо rules/common/*
  install.py prune     убрать мёртвый груз (GSD, tool-recall, skill-selector) из контекста
  install.py all       install + rules + prune
  install.py check     что установлено и работает (exit 1 при расхождении)
  install.py restore <каталог бэкапа>   вернуть как было
"""
from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.abspath(__file__))
CLAUDE = os.path.expanduser("~/.claude")
SETTINGS = os.path.join(CLAUDE, "settings.json")
SERVER = os.path.join(REPO, "planner", "server.py")
HOOKS = os.path.join(REPO, "planner", "hooks.py")
SKILL_LINK = os.path.join(CLAUDE, "skills", "planner")
CLI_LINK = os.path.expanduser("~/bin/cplan")
PY = "/usr/bin/python3"
HOOK_MARK = "claude-planner/planner/hooks.py"
MOD = os.path.join(REPO, "mod", "plan-board")
MOD_ENV = "CLAUDE_CODE_PLUGIN_DIRS"
CONTEXT_WINDOW = 300_000  # автокомпактация основной сессии; встроенные субагенты наследуют. Состояние плана
                          # переживает сжатие (plan_show), поэтому миллионное окно с планером не нужно
HOOK_MODES = ("read_cap", "agent_gate", "track", "prompt", "stop", "subagent")
PRUNE_GLOBS = ("skills/gsd-*", "agents/gsd-*.md", "get-shit-done", "skills/tool-recall", "skills/skill-selector",
               "skills/learned", "skills/orchestrator")
MANUAL_ONLY = ("skills/model-router/SKILL.md",)  # остаётся по /имя, но сам в контекст не лезет


class Backup:
    def __init__(self):
        self.dir = os.path.join(CLAUDE, "backups", "claude-planner-" + time.strftime("%Y%m%d-%H%M%S"))
        self.ops: list[dict] = []

    def _dst(self, src: str) -> str:
        return os.path.join(self.dir, os.path.relpath(src, CLAUDE))

    def copy(self, src: str) -> None:
        if os.path.exists(src):
            dst = self._dst(src)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            self.ops.append({"op": "copy", "src": src, "dst": dst})
            self.save()

    def move(self, src: str) -> None:
        dst = self._dst(src)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.move(src, dst)
        self.ops.append({"op": "move", "src": src, "dst": dst})
        self.save()

    def note(self, **op) -> None:
        self.ops.append(op)
        self.save()

    def save(self) -> None:
        os.makedirs(self.dir, exist_ok=True)
        with open(os.path.join(self.dir, "manifest.json"), "w", encoding="utf-8") as fh:
            json.dump(self.ops, fh, ensure_ascii=False, indent=1)


def read_json(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path: str, data: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def hook_entries() -> list[tuple[str, dict]]:
    def cmd(mode: str) -> dict:
        return {"type": "command", "command": f"{PY} {HOOKS} {mode}", "timeout": 5}
    return [("PreToolUse", {"matcher": "Read", "hooks": [cmd("read_cap")]}),
            ("PreToolUse", {"matcher": "mcp__planner__.*", "hooks": [cmd("agent_gate")]}),
            ("PostToolUse", {"matcher": "mcp__planner__.*|Edit|Write", "hooks": [cmd("track")]}),
            ("UserPromptSubmit", {"hooks": [cmd("prompt")]}),
            ("Stop", {"hooks": [cmd("stop")]}),
            ("SubagentStart", {"hooks": [cmd("subagent")]})]


def strip_hooks(settings: dict) -> None:
    hooks = settings.get("hooks") or {}
    for event in list(hooks):
        hooks[event] = [g for g in hooks[event] if not any(HOOK_MARK in h.get("command", "") for h in g.get("hooks", []))]
        if not hooks[event]:
            del hooks[event]


def mcp(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["claude", "mcp", *args], capture_output=True, text=True, timeout=120)


def mcp_registered() -> bool:
    try:
        entry = (read_json(os.path.expanduser("~/.claude.json")).get("mcpServers") or {}).get("planner") or {}
    except (OSError, ValueError):
        return False
    return SERVER in (entry.get("args") or [])


def do_install(bk: Backup) -> None:
    bk.copy(SETTINGS)
    if not mcp_registered():
        mcp("remove", "planner", "--scope", "user")
        res = mcp("add", "--scope", "user", "planner", "--", PY, SERVER)
        if res.returncode != 0:
            sys.exit(f"claude mcp add не удался: {res.stderr.strip() or res.stdout.strip()}")
        bk.note(op="mcp")
    for link, target in ((SKILL_LINK, os.path.join(REPO, "skill", "planner")), (CLI_LINK, os.path.join(REPO, "planner", "cli.py"))):
        os.makedirs(os.path.dirname(link), exist_ok=True)
        if os.path.islink(link):
            os.remove(link)
        elif os.path.exists(link):
            bk.move(link)
        os.symlink(target, link)
        bk.note(op="symlink", path=link)
    os.chmod(os.path.join(REPO, "planner", "cli.py"), 0o755)
    settings = read_json(SETTINGS)
    strip_hooks(settings)
    for event, group in hook_entries():
        settings.setdefault("hooks", {}).setdefault(event, []).append(group)
    settings["autoCompactWindow"] = CONTEXT_WINDOW
    env = settings.setdefault("env", {})
    dirs = [d for d in str(env.get(MOD_ENV) or "").split(os.pathsep) if d and d != MOD]
    env[MOD_ENV] = os.pathsep.join([*dirs, MOD])
    write_json(SETTINGS, settings)
    print(f"install: MCP planner, скилл planner, хуки ({', '.join(HOOK_MODES)}), команда cplan, "
          f"панель plan-board, окно автокомпактации {CONTEXT_WINDOW // 1000}k")


def do_rules(bk: Backup) -> None:
    target = os.path.join(CLAUDE, "CLAUDE.md")
    bk.copy(target)
    moved = 0
    for path in sorted(glob.glob(os.path.join(CLAUDE, "rules", "common", "*.md"))):
        bk.move(path)
        moved += 1
    shutil.copy2(os.path.join(REPO, "rules", "CLAUDE.md"), target)
    print(f"rules: новый CLAUDE.md ({os.path.getsize(target)} байт), {moved} файлов rules/common ушли в бэкап")


def do_prune(bk: Backup) -> None:
    moved = 0
    for pattern in PRUNE_GLOBS:
        for path in sorted(glob.glob(os.path.join(CLAUDE, pattern))):
            bk.move(path)
            moved += 1
    for rel in MANUAL_ONLY:
        path = os.path.join(CLAUDE, rel)
        if os.path.isfile(path):
            text = open(path, encoding="utf-8").read()
            if "disable-model-invocation" not in text.split("---", 2)[1]:
                bk.copy(path)
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(text.replace("---\n", "---\ndisable-model-invocation: true\n", 1))
    print(f"prune: {moved} каталогов и файлов ушли в бэкап; model-router остался только по ручному вызову")


def do_restore(path: str) -> None:
    ops = read_json(os.path.join(path, "manifest.json"))
    for op in reversed(ops):
        if op["op"] == "symlink" and os.path.islink(op["path"]):
            os.remove(op["path"])
        elif op["op"] == "mcp":
            mcp("remove", "planner", "--scope", "user")
        elif op["op"] == "move" and os.path.exists(op["dst"]):
            if os.path.lexists(op["src"]):
                sys.exit(f"Не могу вернуть {op['src']}: путь занят.")
            os.makedirs(os.path.dirname(op["src"]), exist_ok=True)
            shutil.move(op["dst"], op["src"])
        elif op["op"] == "copy" and os.path.exists(op["dst"]):
            shutil.copy2(op["dst"], op["src"])
    print(f"restore: возвращено {len(ops)} изменений из {path}")


def do_check() -> int:
    checks = []
    checks.append(("MCP planner зарегистрирован", mcp_registered()))
    checks.append(("скилл planner на месте", os.path.isfile(os.path.join(SKILL_LINK, "SKILL.md"))))
    checks.append(("команда cplan на месте", os.path.exists(CLI_LINK)))
    try:
        settings = read_json(SETTINGS)
    except (OSError, ValueError):
        settings = {}
    hooks = json.dumps(settings.get("hooks") or {})
    checks.append((f"{len(HOOK_MODES)} хуков в settings.json", all(f"hooks.py {m}\"" in hooks for m in HOOK_MODES)))
    window = settings.get("autoCompactWindow")
    env_window = (settings.get("env") or {}).get("CLAUDE_CODE_AUTO_COMPACT_WINDOW")
    checks.append((f"окно автокомпактации {window} (нужно ≤ {CONTEXT_WINDOW})",
                   isinstance(window, int) and window <= CONTEXT_WINDOW and not env_window))
    try:
        mod_on = MOD in str((read_json(SETTINGS).get("env") or {}).get(MOD_ENV) or "").split(os.pathsep)
    except (OSError, ValueError):
        mod_on = False
    checks.append(("панель plan-board подключена", mod_on and os.path.isfile(os.path.join(MOD, "hooks", "register.tsx"))))
    try:
        same = open(os.path.join(CLAUDE, "CLAUDE.md"), encoding="utf-8").read() == open(os.path.join(REPO, "rules", "CLAUDE.md"), encoding="utf-8").read()
    except OSError:
        same = False
    checks.append(("CLAUDE.md — минимальный", same))
    checks.append(("rules/common пуст", not glob.glob(os.path.join(CLAUDE, "rules", "common", "*.md"))))
    checks.append(("мёртвый груз убран", not any(glob.glob(os.path.join(CLAUDE, p)) for p in PRUNE_GLOBS)))
    payload = ('{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n')
    res = subprocess.run([PY, SERVER], input=payload, capture_output=True, text=True, timeout=30)
    tools = len(re.findall(r'"name": "(?:plan|wiki)_', res.stdout))
    checks.append((f"сервер отвечает, инструментов {tools}", tools == 13))
    checks.append(("claude в PATH", shutil.which("claude") is not None))
    for name, ok in checks:
        print(("OK   " if ok else "FAIL ") + name)
    return 0 if all(ok for _, ok in checks) else 1


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "check":
        return do_check()
    if cmd == "restore" and len(argv) > 2:
        do_restore(argv[2])
        return 0
    steps = {"install": [do_install], "rules": [do_rules], "prune": [do_prune],
             "all": [do_install, do_rules, do_prune]}.get(cmd)
    if not steps:
        print(__doc__)
        return 2
    bk = Backup()
    for step in steps:
        step(bk)
    print(f"Бэкап и манифест: {bk.dir}\nОткат: {PY} {os.path.abspath(__file__)} restore {bk.dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
