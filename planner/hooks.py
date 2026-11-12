#!/usr/bin/env python3
"""Хуки Claude Code для планера. Автономный скрипт (без импорта пакета — старт за миллисекунды).

  read_cap  PreToolUse(Read)            — не читать большой файл кода целиком
  subagent  SubagentStart               — встроенный субагент получает действующий план и правила работы в нём
  agent_gate PreToolUse(planner)        — субагент план не меняет и пункты не закрывает: это делает основная сессия
  plan_gate PreToolUse(Edit|Write…)     — правка файла проекта только при открытом плане; вес плана выбирает модель
  track     PostToolUse(planner|Edit…)  — помнить, какие планы ведёт сессия; разовая подсказка про план
  prompt    UserPromptSubmit            — новый ход пользователя: сбросить счётчики
  stop      Stop                        — не бросать план, который вели в этом ходу

Любая внутренняя ошибка — fail-open: хук молчит и ничего не блокирует.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time

READ_CAP = int(os.environ.get("CPLAN_READ_CAP", "500") or 0)
NUDGE_AFTER = 4
MAX_BLOCKS = 2
CODE_EXT = {".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".cs", ".go", ".rs", ".java", ".kt", ".gd",
            ".php", ".rb", ".c", ".cc", ".cpp", ".h", ".hpp", ".swift", ".vue", ".svelte", ".scala", ".dart"}
ACTIVE = ("QUEUED", "RUNNING", "VERIFYING", "VERIFY_WAIT")
OPEN = ("todo", "in_progress", "failed")
AGENT_GATE = (os.environ.get("CPLAN_AGENT_GATE", "1") or "0") != "0"
OWNER_TOOLS = ("plan_init", "plan_update", "plan_skip", "plan_finish", "plan_run", "plan_verify", "plan_review",
               "plan_worker", "wiki_publish")  # читать план (plan_show, plan_jobs, plan_scout, wiki_pages) субагенту можно
BRIEF_CAP = 4000
PLAN_GATE = (os.environ.get("CPLAN_PLAN_GATE", "1") or "0") != "0"
PROJECT_DIRS = (os.environ.get("CPLAN_PROJECT_DIRS") or "~/projects:~/work:/mnt/datadisk/Projects").split(":")
SELF = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))  # планер правят напрямую: сам себя он не гейтит


def state_path(session_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id or "nosession")[:80]
    return os.path.join(os.path.expanduser("~/.cache/claude-planner/sessions"), safe + ".json")


def load_state(session_id: str) -> dict:
    try:
        with open(state_path(session_id), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_state(session_id: str, st: dict) -> None:
    path = state_path(session_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(st, fh, ensure_ascii=False)
    os.replace(tmp, path)


def git_root(path: str) -> str | None:
    cur = path if os.path.isdir(path) else os.path.dirname(path)
    home = os.path.expanduser("~")
    while cur and cur not in ("/", home):
        if os.path.exists(os.path.join(cur, ".git")):
            return cur
        cur = os.path.dirname(cur)
    return None


def read_plan(root: str) -> dict | None:
    try:
        with open(os.path.join(root, ".cplan", "plan.json"), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def active_jobs(root: str) -> list[str]:
    out = []
    base = os.path.join(root, ".cplan", "jobs")
    try:
        names = os.listdir(base)
    except OSError:
        return out
    for name in names:
        try:
            with open(os.path.join(base, name, "job.json"), encoding="utf-8") as fh:
                job = json.load(fh)
        except (OSError, ValueError):
            continue
        if job.get("status") in ACTIVE:
            try:
                os.kill(int(job.get("supervisor_pid") or 0), 0)
            except (OSError, ValueError):
                continue
            out.append(name)
    return out


# ───────────────────────── read_cap ─────────────────────────

def read_cap(data: dict) -> dict:
    inp = data.get("tool_input") or {}
    path = inp.get("file_path")
    if not READ_CAP or not isinstance(path, str) or inp.get("offset") or inp.get("limit"):
        return {}
    if os.path.splitext(path)[1].lower() not in CODE_EXT:
        return {}
    try:
        if os.path.getsize(path) > 8_000_000:
            return {}
        with open(path, "rb") as fh:
            raw = fh.read()
        lines = raw.count(b"\n") + (0 if raw.endswith(b"\n") or not raw else 1)
    except OSError:
        return {}
    if lines <= READ_CAP:
        return {}
    reason = (f"{os.path.basename(path)}: {lines} строк — целиком не читаем. Найди нужное место "
              f"(rg -n '<символ>' {path}), затем Read с offset и limit (до 300 строк).")
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


# ───────────────────────── субагенты и план ─────────────────────────

def plan_root(path: str) -> str | None:
    """Ближайшая вверх по дереву папка с планом."""
    cur = path if os.path.isdir(path) else os.path.dirname(path)
    home = os.path.expanduser("~")
    while cur and cur not in ("/", home):
        if os.path.isfile(os.path.join(cur, ".cplan", "plan.json")):
            return cur
        cur = os.path.dirname(cur)
    return None


def plan_brief(root: str, plan: dict) -> str:
    lines = [f"План «{plan.get('title')}» в {root} ({plan.get('status')}). Цель: {str(plan.get('goal') or '')[:300]}"]
    if plan.get("hold"):
        lines.append(f"План на паузе: {plan['hold']}")
    for it in (plan.get("items") or [])[:30]:
        held = ", у воркера — файлы не трогать" if it.get("status") == "in_progress" and it.get("job") else ""
        files = ", ".join((it.get("files") or [])[:6])
        lines.append(f"- {it.get('id')} [{it.get('status')}{held}] {str(it.get('title'))[:90]}"
                     + (f" | файлы: {files}" if files else ""))
    return "\n".join(lines)


def subagent(data: dict) -> dict:
    roots = list(load_state(data.get("session_id") or "").get("roots") or [])
    cwd = data.get("cwd")
    here = plan_root(os.path.realpath(cwd)) if isinstance(cwd, str) and cwd else None
    if here and here not in roots:
        roots.append(here)
    briefs = []
    for root in roots:
        plan = read_plan(root)
        if plan and plan.get("status") != "finished":
            briefs.append(plan_brief(root, plan))
    if not briefs:
        return {}
    rules = (
        "Основная сессия ведёт работу по плану, ты работаешь внутри него:\n"
        "1) Делай только порученное. Если поручение — пункт плана, держись его файлов и критерия приёмки.\n"
        "2) Файлы пунктов, которые сейчас у воркеров, не правь: их правят параллельно.\n"
        "3) План не меняй и пункты не закрывай — это делает основная сессия (plan_show тебе доступен для чтения).\n"
        "4) Задача расходится с планом, нужен чужой файл или план устарел — остановись и напиши об этом в отчёте, "
        "решение примут основная сессия и пользователь.\n"
        "5) В отчёте назови изменённые файлы (file:line) и команду, которой проверил результат.")
    text = (rules + "\n\n" + "\n\n".join(briefs))[:BRIEF_CAP]
    return {"hookSpecificOutput": {"hookEventName": "SubagentStart", "additionalContext": text}}


def agent_gate(data: dict) -> dict:
    if not AGENT_GATE or not data.get("agent_id"):
        return {}
    name = str(data.get("tool_name") or "").rsplit("__", 1)[-1]
    if name not in OWNER_TOOLS:
        return {}
    reason = (f"{name}: план ведёт основная сессия, субагент его не меняет и пункты не закрывает. Сделай порученное и "
              "верни отчёт: что изменил (file:line), чем проверил, что расходится с планом. Смотреть план можно: "
              "plan_show.")
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


# ───────────────────────── plan_gate ─────────────────────────

def open_plan(path: str) -> str | None:
    """Папка с незакрытым планом, который накрывает файл."""
    cur = os.path.dirname(path)
    home = os.path.realpath(os.path.expanduser("~"))
    while cur and cur not in ("/", home):
        plan = read_plan(cur)
        if plan and plan.get("status") != "finished":
            return cur
        cur = os.path.dirname(cur)
    return None


def project_root(path: str) -> str | None:
    """Проект файла: git-репозиторий, папка с планом или папка первого уровня в каталогах проектов."""
    root = git_root(path) or plan_root(path)
    if root:
        return root
    for base in PROJECT_DIRS:
        base = os.path.realpath(os.path.expanduser(base)) if base else ""
        if base and path.startswith(base + os.sep):
            rest = path[len(base) + 1:].split(os.sep)
            return os.path.join(base, rest[0]) if len(rest) > 1 else None
    return None


def wrong_branch(root: str, path: str) -> str | None:
    """План привязан к ветке: в другой ветке того же рабочего дерева править нельзя (отдельный worktree — можно)."""
    plan = read_plan(root) or {}
    want = plan.get("branch")
    if not want or git_root(path) != root:
        return None
    try:
        with open(os.path.join(root, ".git", "HEAD"), encoding="utf-8") as fh:
            head = fh.read().strip()
    except OSError:
        return None
    cur = head[len("ref: refs/heads/"):] if head.startswith("ref: refs/heads/") else None
    if not cur or cur == want:
        return None
    return (f"план «{plan.get('title')}» ведётся в ветке {want}, а сейчас {cur}. Вернись: git switch {want} — либо "
            f"перенеси план в эту ветку: plan_update(branch=\"{cur}\").")


def light_budget(root: str, path: str) -> str | None:
    """План из одних пунктов без проверки — чеклист: один файл кода на пункт, дальше нужен пункт с verify."""
    plan = read_plan(root) or {}
    items = plan.get("items") or []
    if not items or any(not i.get("light") for i in items) or os.path.splitext(path)[1].lower() not in CODE_EXT:
        return None
    book = os.path.join(root, ".cplan", "light_files.json")
    try:
        with open(book, encoding="utf-8") as fh:
            st = json.load(fh)
    except (OSError, ValueError):
        st = {}
    if st.get("plan") != plan.get("created_at"):
        st = {"plan": plan.get("created_at"), "files": []}
    if path in st["files"]:
        return None
    if len(st["files"]) >= len(items):
        return (f"в плане только пункты без проверки ({len(items)}), а это уже {len(st['files']) + 1}-й файл кода. "
                "Пункт без проверки — один файл кода. Задача выросла: добавь пункт с проверкой "
                "plan_update(add=[{title, acceptance, verify, files}]) или дай проверку имеющемуся "
                "plan_update(update=[{id, verify: [...]}]) — после этого правь дальше.")
    st["files"].append(path)
    tmp = f"{book}.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(st, fh, ensure_ascii=False)
    os.replace(tmp, book)
    return None


def plan_gate(data: dict) -> dict:
    inp = data.get("tool_input") or {}
    path = inp.get("file_path") or inp.get("notebook_path")
    if not PLAN_GATE or not isinstance(path, str) or not path:
        return {}
    path = os.path.realpath(os.path.join(str(data.get("cwd") or ""), os.path.expanduser(path)))
    own = os.path.join(os.path.realpath(os.path.expanduser("~")), ".claude") + os.sep  # память, настройки, скиллы
    if path.startswith((own, SELF + os.sep)) or "/.cplan/" in path:
        return {}
    if "/.claude/" in path and "/.claude/worktrees/" not in path:
        return {}
    root = project_root(path)
    covered = open_plan(path)
    name = os.path.basename(path)
    if covered:
        over = wrong_branch(covered, path) or light_budget(covered, path)
        if not over:
            return {}
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                       "permissionDecisionReason": f"{name}: {over}"}}
    if not root:
        return {}
    if data.get("agent_id"):
        reason = (f"{name}: в {root} нет открытого плана, а правки в проекте идут только по плану. План заводит "
                  "основная сессия: остановись и верни ей отчёт — что и в каких файлах нужно изменить (file:line).")
    else:
        reason = (f"{name}: правка без плана. В проекте правки идут только по плану — это решает хук, а не размер "
                  f"задачи. Заведи план: скилл planner → plan_init(project_dir=\"{root}\", title, goal, items). Вес "
                  "выбираешь ты и меняешь по ходу: мелочь в одном файле, отладка, эксперимент, не код — пункт без проверки "
                  "{title, light: trivial|debug|spike|nocode|followup}, закрывается plan_verify(note); работа с "
                  "наблюдаемым результатом — пункт tier \"main\" с verify, делаешь сам; работы "
                  "больше — независимые пункты воркерам (code-mini — механика на Haiku / code-low / code / code-high), незнакомый код — сначала "
                  "plan_scout; выросла задача — plan_update(add=[...]). Инструментов planner в сессии нет — скажи "
                  "пользователю. Через Bash запрет не обходи.")
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


# ───────────────────────── track / prompt ─────────────────────────

def track(data: dict) -> dict:
    if data.get("agent_id"):
        return {}
    sid, tool = data.get("session_id") or "", data.get("tool_name") or ""
    inp = data.get("tool_input") or {}
    st = load_state(sid)
    if "planner__" in tool:
        root = inp.get("project_dir")
        if isinstance(root, str) and root:
            root = os.path.realpath(os.path.expanduser(root))
            root = git_root(root) or root
            roots = st.setdefault("roots", [])
            if root not in roots:
                roots.append(root)
        st["turn"] = True
        st["ts"] = time.time()
        save_state(sid, st)
        return {}
    path = inp.get("file_path")
    if tool not in ("Edit", "Write", "MultiEdit") or not isinstance(path, str) or st.get("nudged"):
        return {}
    root = git_root(os.path.realpath(path))
    if not root or "/.claude/" in path:
        return {}
    plan = read_plan(root)
    if plan and plan.get("status") != "finished":
        return {}
    edited = st.setdefault("edited", [])
    if path not in edited:
        edited.append(path)
    out = {}
    if len(edited) >= NUDGE_AFTER:
        st["nudged"] = True
        out = {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": (
            f"Правишь уже {len(edited)}-й файл в {root} без плана. Если это не цепочка мелочей — заведи план "
            "(скилл plan, mcp planner: plan_init с проверками), независимые пункты отдай воркерам.")}}
    save_state(sid, st)
    return out


def prompt(data: dict) -> dict:
    sid = data.get("session_id") or ""
    st = load_state(sid)
    if st:
        st.update(turn=False, blocks=0, block_hash="")
        save_state(sid, st)
    return {}


# ───────────────────────── stop ─────────────────────────

def stop(data: dict) -> dict:
    sid = data.get("session_id") or ""
    st = load_state(sid)
    if not st.get("turn") or not st.get("roots"):
        return {}
    reasons, digest = [], hashlib.sha1()
    for root in st["roots"]:
        plan = read_plan(root)
        if not plan or plan.get("status") == "finished" or plan.get("hold"):
            continue
        jobs = active_jobs(root)
        items = plan.get("items") or []
        digest.update(json.dumps([plan.get("status"), [(i["id"], i["status"]) for i in items], sorted(jobs)]).encode())
        opened = [i for i in items if i.get("status") in OPEN]
        if jobs:
            reasons.append(f"{root}: работают воркеры ({len(jobs)}) — plan_run() дождётся и проверит.")
        elif opened:
            reasons.append(f"{root}: открыты пункты " + ", ".join(f"{i['id']} [{i['status']}]" for i in opened[:8])
                           + " — доведи (plan_run / plan_verify). Обстоятельства изменились или нужен ответ "
                             "пользователя — объяви паузу plan_update(hold=\"причина\") и спроси: с его согласия "
                             "пункты закрывает plan_skip, план — plan_finish(force=true).")
        elif plan.get("status") == "review_pending":
            rejected = sum(1 for r in ((plan.get("review") or {}).get("rounds") or []) if r.get("verdict") == "rejected")
            if rejected < 3:
                reasons.append(f"{root}: все пункты закрыты, не пройдена приёмка — plan_review().")
        elif plan.get("status") == "docs_pending":
            reasons.append(f"{root}: работа принята, документация не опубликована — wiki_publish(title, notes): страницу напишет воркер.")
        elif plan.get("status") == "ready":
            reasons.append(f"{root}: всё проверено, план не закрыт — plan_finish(summary).")
    if not reasons:
        return {}
    cur = digest.hexdigest()
    blocks = st.get("blocks", 0) if st.get("block_hash") == cur else 0
    if blocks >= MAX_BLOCKS:
        return {}  # состояние не меняется — не зацикливаем, отпускаем
    st.update(blocks=blocks + 1, block_hash=cur)
    save_state(sid, st)
    return {"decision": "block", "reason": "План не доведён:\n" + "\n".join(reasons)}


MODES = {"read_cap": read_cap, "track": track, "prompt": prompt, "stop": stop, "subagent": subagent,
         "agent_gate": agent_gate, "plan_gate": plan_gate}


def main(argv: list[str]) -> int:
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
        out = MODES[argv[1]](data if isinstance(data, dict) else {})
    except Exception as exc:  # noqa: BLE001 — fail-open
        try:
            log = os.path.expanduser("~/.cache/claude-planner/hooks.log")
            os.makedirs(os.path.dirname(log), exist_ok=True)
            with open(log, "a", encoding="utf-8") as fh:
                fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {argv[1:]} {type(exc).__name__}: {exc}\n")
        except OSError:
            pass
        out = {}
    if out:
        sys.stdout.write(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
