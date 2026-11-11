"""Воркеры: облегчённые headless-сессии `claude -p` под надзором отдельного процесса.

Воркер стартует без пользовательских настроек, MCP и CLAUDE.md (≈5k токенов контекста
вместо ≈47k у обычного субагента). Тир задаёт модель, effort и права:
scout — Haiku, только чтение; code-low/code/code-high — Sonnet с правкой; review — приёмщик.
Каждый запуск — каталог <root>/.cplan/jobs/<job_id>/ с job.json и stream.jsonl.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid

from . import core

CONFIG_FILE = "~/.config/claude-planner/config.json"
MAX_PARALLEL = 4
MAX_FIX_ROUNDS = 1
DEFAULT_TIERS = {
    "scout": {"model": "haiku", "effort": None, "mode": "read", "timeout": 300},
    "code-low": {"model": "sonnet", "effort": "low", "mode": "edit", "timeout": 900},
    "code": {"model": "sonnet", "effort": "medium", "mode": "edit", "timeout": 1500},
    "code-high": {"model": "sonnet", "effort": "high", "mode": "edit", "timeout": 2400},
    "review": {"model": "opus", "effort": "medium", "mode": "read", "timeout": 900},
}
ACTIVE = ("QUEUED", "RUNNING", "VERIFYING", "VERIFY_WAIT")
EDITING = ("QUEUED", "RUNNING")
READ_BASH = ("rg", "fd", "ls", "wc", "bat", "cat", "head", "tail", "sed -n", "tree", "stat", "file", "jq",
             "ast-grep", "codegraph", "git log", "git diff", "git show", "git status", "git blame",
             "git ls-files", "git grep", "git rev-parse")
DENY_BASH = ("git commit", "git push", "git reset", "git checkout", "git restore", "git stash", "git clean",
             "git rebase", "git merge", "sudo", "ssh", "scp", "rsync", "systemctl", "docker", "kubectl",
             "pip install", "npm install", "apt", "pkill", "killall")
SCRUB_ENV = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION",
             "CLAUDE_PID", "CLAUDE_JOB_DIR", "CLAUDE_EFFORT", "CLAUDE_CODE_MESSAGING_SOCKET",
             "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_CODE_SESSION_ATTENDED", "CLAUDE_CODE_EXECPATH",
             "CLAUDE_CODE_PLUGIN_DIRS")  # последнее — чтобы панель прогресса не грузилась в воркеров

COMMON_RULES = (
    "Локализуй до чтения: rg -n, затем Read с offset/limit. Файл длиннее 400 строк целиком не читай. "
    "Пути, символы, флаги не выдумывай — подсматривай в файлах и выводе команд. "
    "Независимые вызовы инструментов делай пачкой в одном шаге. Вопросы пользователю и субагенты недоступны."
)
SYS_SCOUT = (
    "Ты разведчик кода в проекте {root}. Только чтение. " + COMMON_RULES + "\n"
    "Ответ — не длиннее 40 строк, без вступлений:\n"
    "1) ГДЕ: точные file:line и имена символов по вопросу.\n"
    "2) КАК УСТРОЕНО: 3–6 строк, только то, что нужно для правки.\n"
    "3) ЧТО МЕНЯТЬ: файлы и места (file:L1-L2), если вопрос про изменение.\n"
    "4) ПРОВЕРКА: какой командой это проверяется в проекте (тесты, сборка).\n"
    "5) РИСКИ: что легко сломать. Чего не нашёл — так и напиши, не домысливай."
)
SYS_CODE = (
    "Ты исполнитель одного пункта плана в проекте {root}. " + COMMON_RULES + "\n"
    "1) Делай ровно пункт, не расширяй задачу. Правь только файлы из списка; без другого файла не обойтись — "
    "закончи отчётом STATUS: BLOCKED с причиной.\n"
    "2) Минимальный дифф в стиле соседнего кода. Никаких заглушек, TODO и закомментированного кода.\n"
    "3) Прогони проверки пункта сам и чини, пока не пройдут: после тебя их запустит приёмщик.\n"
    "4) Запрещено: git commit/push/reset/checkout/stash, sudo, ssh, установка пакетов, правка чужих файлов.\n"
    "Финальный ответ — не длиннее 15 строк: первая строка «STATUS: DONE» или «STATUS: BLOCKED», затем "
    "изменённые места (file:line), какие проверки прогнал и с каким результатом, грабли."
)
SYS_REVIEW = (
    "Ты приёмщик в проекте {root}: враждебно проверяешь выполненный план. Только чтение. " + COMMON_RULES + "\n"
    "Зелёные проверки доказывают лишь то, что код не упал. Проверь по диффу и по коду: выполнена ли цель и "
    "критерий каждого пункта по существу, нет ли заглушек, сломанных соседних путей, забытых мест "
    "использования, правок вне задачи. Каждое замечание — с file:line, которое ты сам прочитал. "
    "severity=blocker только для того, что делает результат неверным или ломает существующее; вкусовщина "
    "и улучшения — minor. В issues пиши только дефекты: подтверждения «проверил, всё верно» не нужны, "
    "при отсутствии дефектов issues пустой. Проверки пунктов планер только что перезапустил сам — их "
    "результат дан в задании, не трать шаги на перезапуск. Нет блокеров — verdict=approved."
)
REVIEW_SCHEMA = {
    "type": "object", "required": ["verdict", "issues"], "additionalProperties": False,
    "properties": {
        "verdict": {"type": "string", "enum": ["approved", "rejected"]},
        "issues": {"type": "array", "items": {
            "type": "object", "required": ["severity", "problem"], "additionalProperties": False,
            "properties": {"severity": {"type": "string", "enum": ["blocker", "minor"]},
                           "item_id": {"type": "string"}, "file": {"type": "string"},
                           "line": {"type": "integer"}, "problem": {"type": "string"},
                           "fix": {"type": "string"}}}},
    },
}


def tiers() -> dict:
    out = {k: dict(v) for k, v in DEFAULT_TIERS.items()}
    try:
        with open(os.path.expanduser(CONFIG_FILE), encoding="utf-8") as fh:
            for name, patch in (json.load(fh).get("tiers") or {}).items():
                if name in out and isinstance(patch, dict):
                    out[name].update({k: v for k, v in patch.items() if k in ("model", "effort", "timeout")})
    except (OSError, ValueError):
        pass
    return out


def claude_bin() -> str:
    return os.environ.get("CPLAN_CLAUDE_BIN") or shutil.which("claude") or "claude"


def hooks_script() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "hooks.py")


def build_cmd(job: dict, resume: bool) -> list[str]:
    tier = tiers()[job["tier"]]
    cmd = [claude_bin(), "-p", "--model", tier["model"], "--setting-sources", "", "--strict-mcp-config",
           "--output-format", "stream-json", "--verbose", "--permission-mode", "dontAsk"]
    if tier.get("effort"):
        cmd += ["--effort", tier["effort"]]
    cmd += ["--resume", job["session_id"]] if resume else ["--session-id", job["session_id"]]
    if tier["mode"] == "read":
        cmd += ["--tools", "Read,Grep,Glob,Bash",
                "--allowedTools", "Read", "Grep", "Glob", *[f"Bash({c}:*)" for c in READ_BASH]]
    else:
        cmd += ["--tools", "Read,Grep,Glob,Bash,Edit,Write",
                "--allowedTools", "Read", "Grep", "Glob", "Bash", "Edit", "Write",
                "--disallowedTools", *[f"Bash({c}:*)" for c in DENY_BASH]]
    guard = {"hooks": {"PreToolUse": [{"matcher": "Read", "hooks": [
        {"type": "command", "command": f"{sys.executable} {hooks_script()} read_cap", "timeout": 5}]}]}}
    cmd += ["--settings", json.dumps(guard), "--system-prompt", job["system"]]
    if job.get("schema"):
        cmd += ["--json-schema", json.dumps(job["schema"])]
    return cmd


def child_env() -> dict:
    env = {k: v for k, v in os.environ.items() if k not in SCRUB_ENV}
    env["CPLAN_WORKER"] = "1"
    return env


# ───────────────────────── промпты ─────────────────────────

_SLICE_RE = re.compile(r"([A-Za-z0-9_./-]+?\.[A-Za-z0-9_]+):L?(\d+)(?:\s*(?:-|\.\.)\s*L?(\d+))?")


def hydrate(root: str, text: str, max_chars: int = 12000) -> str:
    """Вырезает с диска срезы path:L1-L2, упомянутые в контексте, и отдаёт их воркеру готовыми."""
    out, used, seen = [], 0, set()
    for m in _SLICE_RE.finditer(text or ""):
        rel, a, b = m.group(1), int(m.group(2)), int(m.group(3) or m.group(2))
        a, b = max(1, min(a, b) - (0 if m.group(3) else 15)), max(a, b) + (0 if m.group(3) else 25)
        b = min(b, a + 220)
        path = os.path.join(root, rel)
        if (rel, a, b) in seen or not os.path.isfile(path):
            continue
        seen.add((rel, a, b))
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()[a - 1:b]
        except OSError:
            continue
        block = f"--- {rel}:{a}-{a + len(lines) - 1}\n" + "".join(f"{a + n}\t{ln}" for n, ln in enumerate(lines))
        if used + len(block) > max_chars:
            break
        used += len(block)
        out.append(block.rstrip("\n"))
    return "\n".join(out)


def item_prompt(plan: dict, item: dict) -> str:
    root = plan["root"]
    parts = [f"ЦЕЛЬ ПЛАНА: {plan['goal']}", f"ПУНКТ {item['id']}: {item['title']}",
             f"КРИТЕРИЙ ПРИЁМКИ: {item['acceptance']}"]
    if item["files"]:
        parts.append("ФАЙЛЫ (правь только их; new: — создать): " + ", ".join(item["files"]))
    parts.append("ПРОВЕРКИ (cwd = корень проекта):\n" + "\n".join(
        f"- {c['type']}: {core.check_label(c)}" + (f" → в выводе должно быть {c['contains']!r}" if c.get("contains") else "")
        for c in item["verify"]))
    if item.get("context"):
        parts.append("КОНТЕКСТ: " + item["context"])
        slices = hydrate(root, item["context"])
        if slices:
            parts.append("СРЕЗЫ КОДА (актуальны на момент старта, номера строк слева):\n" + slices)
    return "\n\n".join(parts)


def fix_prompt(item: dict, ev: dict) -> str:
    return ("Приёмщик запустил проверки пункта — не прошли. Исправь причину (не проверку) и заверши тем же "
            "форматом отчёта.\n\n" + core.fail_report(item, ev))


# ───────────────────────── файлы заданий ─────────────────────────

def jobs_dir(root: str) -> str:
    return core.pdir(root, "jobs")


def job_path(root: str, job_id: str) -> str:
    return os.path.join(jobs_dir(root), job_id, "job.json")


def read_job(root: str, job_id: str) -> dict | None:
    try:
        with open(job_path(root, job_id), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def write_job(job: dict) -> None:
    core.atomic_write(job_path(job["root"], job["id"]), json.dumps(job, ensure_ascii=False, indent=1))


def _alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def reap(job: dict) -> dict:
    """Надзиратель умер, а статус ещё активный — фиксируем честный провал."""
    if job["status"] in ACTIVE and job.get("supervisor_pid") and not _alive(job["supervisor_pid"]) \
            and time.time() - job.get("started", 0) > 5:
        fresh = read_job(job["root"], job["id"]) or job
        if fresh["status"] in ACTIVE:
            fresh.update(status="FAILED", reason="надзиратель воркера умер", ended=time.time())
            write_job(fresh)
            if fresh.get("item_id"):
                try:
                    core.set_status(fresh["root"], fresh["item_id"], "failed", "воркер оборвался", job=fresh["id"])
                except core.PlanError:
                    pass
        return fresh
    return job


_CHILDREN: list[subprocess.Popen] = []


def _reap_children() -> None:
    """Собирает завершившихся надзирателей, иначе они висят зомби и выглядят живыми."""
    _CHILDREN[:] = [p for p in _CHILDREN if p.poll() is None]


def list_jobs(root: str) -> list[dict]:
    _reap_children()
    out = []
    try:
        names = sorted(os.listdir(jobs_dir(root)))
    except OSError:
        return out
    for name in names:
        job = read_job(root, name)
        if job:
            out.append(reap(job))
    return sorted(out, key=lambda j: j.get("started", 0))


def active_jobs(root: str) -> list[dict]:
    return [j for j in list_jobs(root) if j["status"] in ACTIVE]


def resolve(root: str, ref: str) -> dict:
    """Находит задание по id задания или по id пункта (последнее)."""
    ref = str(ref).strip()
    jobs = list_jobs(root)
    for j in jobs:
        if j["id"] == ref:
            return j
    mine = [j for j in jobs if str(j.get("item_id")) == ref]
    if mine:
        return mine[-1]
    raise core.PlanError(f"Задания {ref!r} нет. Есть: " + (", ".join(j["id"] for j in jobs[-8:]) or "ни одного") + ".")


def spawn(root: str, kind: str, tier: str, tag: str, prompt: str, system: str, item_id: str | None = None,
          schema: dict | None = None, resume_session: str | None = None, max_fix: int = MAX_FIX_ROUNDS,
          on_created=None) -> dict:
    running = active_jobs(root)
    if len(running) >= MAX_PARALLEL:
        raise core.PlanError(f"Уже работает {len(running)} воркеров (предел {MAX_PARALLEL}). Дождись: plan_jobs(wait=true).")
    job_id = f"{re.sub(r'[^A-Za-z0-9_.-]', '-', tag)[:24]}-{time.strftime('%H%M%S')}-{uuid.uuid4().hex[:4]}"
    job = {"id": job_id, "root": root, "kind": kind, "tier": tier, "item_id": item_id, "status": "QUEUED",
           "session_id": resume_session or str(uuid.uuid4()), "resume": bool(resume_session),
           "system": system.format(root=root), "prompt": prompt, "schema": schema, "max_fix": max_fix,
           "fix_round": 0, "started": time.time(), "timeout": tiers()[tier]["timeout"],
           "model": tiers()[tier]["model"], "effort": tiers()[tier].get("effort")}
    write_job(job)
    if on_created:
        on_created(job)  # пункт помечается «в работе» до старта процесса: иначе гонка с быстрым воркером
    log = open(os.path.join(jobs_dir(root), job_id, "supervisor.log"), "ab")
    pkg_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    proc = subprocess.Popen([sys.executable, "-m", "planner.supervisor", os.path.join(jobs_dir(root), job_id)],
                            cwd=pkg_parent, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                            start_new_session=True, env=os.environ.copy())
    log.close()
    _CHILDREN.append(proc)
    job["supervisor_pid"] = proc.pid
    fresh = read_job(root, job_id) or job
    fresh["supervisor_pid"] = proc.pid
    write_job(fresh)
    return fresh


def kill(root: str, job: dict, reason: str = "остановлен оркестратором") -> dict:
    if job["status"] in ACTIVE and job.get("supervisor_pid"):
        try:
            os.killpg(int(job["supervisor_pid"]), signal.SIGTERM)
        except (OSError, ValueError):
            pass
        for _ in range(30):
            if not _alive(job["supervisor_pid"]):
                break
            time.sleep(0.1)
        else:
            try:
                os.killpg(int(job["supervisor_pid"]), signal.SIGKILL)
            except (OSError, ValueError):
                pass
    fresh = read_job(root, job["id"]) or job
    if fresh["status"] in ACTIVE:
        fresh.update(status="KILLED", reason=reason, ended=time.time())
        write_job(fresh)
        if fresh.get("item_id"):
            core.set_status(root, fresh["item_id"], "failed", reason, job=fresh["id"])
    return fresh


# ───────────────────────── телеметрия из stream-json ─────────────────────────

def stream_path(root: str, job_id: str) -> str:
    return os.path.join(jobs_dir(root), job_id, "stream.jsonl")


def _brief(name: str, inp: dict) -> str:
    val = inp.get("file_path") or inp.get("command") or inp.get("pattern") or inp.get("path") or ""
    return f"{name} {str(val).replace(chr(10), ' ')[:90]}".strip()


def telemetry(root: str, job_id: str, trail: int = 0) -> dict:
    t = {"steps": 0, "last": "", "out_tokens": 0, "ctx": 0, "touched": [], "text": "", "structured": None,
         "cost": 0.0, "turns": 0, "finished": False, "is_error": False, "denials": 0, "trail": []}
    touched, seen = [], set()
    try:
        fh = open(stream_path(root, job_id), encoding="utf-8", errors="replace")
    except OSError:
        return t
    with fh:
        for line in fh:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            kind = d.get("type")
            if kind == "assistant":
                msg = d.get("message") or {}
                u = msg.get("usage") or {}
                if msg.get("id") not in seen:
                    seen.add(msg.get("id"))
                    t["out_tokens"] += u.get("output_tokens") or 0
                    t["ctx"] = max(t["ctx"], sum(u.get(k) or 0 for k in ("input_tokens", "cache_read_input_tokens",
                                                                         "cache_creation_input_tokens")))
                for b in msg.get("content") or []:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "tool_use":
                        t["steps"] += 1
                        t["last"] = _brief(b.get("name", ""), b.get("input") or {})
                        t["trail"].append(t["last"])
                        fp = (b.get("input") or {}).get("file_path")
                        if b.get("name") in ("Edit", "Write") and fp and fp not in touched:
                            touched.append(fp)
                    elif b.get("type") == "text" and b.get("text", "").strip():
                        t["text"] = b["text"].strip()
                t["finished"] = False
            elif kind == "result":
                t["finished"] = True
                t["is_error"] = bool(d.get("is_error")) or d.get("subtype") != "success"
                t["cost"] = max(t["cost"], d.get("total_cost_usd") or 0.0)
                t["turns"] += d.get("num_turns") or 0
                t["denials"] += len(d.get("permission_denials") or [])
                if isinstance(d.get("result"), str) and d["result"].strip():
                    t["text"] = d["result"].strip()
                if d.get("structured_output") is not None:
                    t["structured"] = d["structured_output"]
    t["touched"] = [os.path.relpath(p, root) if p.startswith(root + os.sep) else p for p in touched]
    t["trail"] = t["trail"][-trail:] if trail else []
    return t


def out_of_scope(item: dict, touched: list[str]) -> list[str]:
    allowed = set(core.file_paths(item))
    return [p for p in touched if p not in allowed] if allowed else []


def view(job: dict, trail: int = 0) -> dict:
    t = telemetry(job["root"], job["id"], trail)
    end = job.get("ended") or time.time()
    return {"job": job["id"], "item": job.get("item_id"), "kind": job["kind"], "tier": job["tier"],
            "model": job.get("model"), "status": job["status"], "reason": job.get("reason", ""),
            "sec": int(end - job.get("started", end)), "steps": t["steps"], "last": t["last"],
            "out_tokens": t["out_tokens"], "ctx": t["ctx"], "cost": round(t["cost"], 4),
            "fix_round": job.get("fix_round", 0), "touched": t["touched"],
            "out_of_scope": job.get("out_of_scope") or [], "report": t["text"], "structured": t["structured"],
            "trail": t["trail"]}


def table(views: list[dict]) -> str:
    if not views:
        return "Воркеров нет."
    rows = []
    for v in views:
        what = f"пункт {v['item']}" if v["item"] else v["kind"]
        tail = v["reason"] if v["status"] in ("FAILED", "KILLED") and v["reason"] else v["last"]
        rows.append(f"- {v['job']} | {what} | {v['tier']} ({v['model']}) | {v['status']}"
                    + (f" (доработка {v['fix_round']})" if v["fix_round"] else "")
                    + f" | {v['sec']}s, шагов {v['steps']}, ctx {v['ctx'] // 1000}k, ${v['cost']}"
                    + (f" | {tail[:100]}" if tail else "")
                    + (f" | ВНЕ ПУНКТА: {', '.join(v['out_of_scope'][:4])}" if v["out_of_scope"] else ""))
    return "\n".join(rows)


def wait(root: str, job_ids: list[str] | None, mode: str = "any", timeout: float = 600, progress=None) -> list[dict]:
    """Ждёт завершения заданий: any — первого, all — всех. Возвращает актуальные задания."""
    deadline = time.time() + max(1, timeout)
    watch = set(job_ids) if job_ids else {j["id"] for j in active_jobs(root)}
    tick = 0
    while True:
        jobs = [j for j in list_jobs(root) if j["id"] in watch]
        done = [j for j in jobs if j["status"] not in ACTIVE]
        if not jobs or (mode == "any" and done) or len(done) == len(jobs) or time.time() >= deadline:
            return jobs
        tick += 1
        if progress and tick % 5 == 0:
            live = [view(j) for j in jobs if j["status"] in ACTIVE]
            progress(len(done), len(jobs), "; ".join(f"{v['item'] or v['kind']}: {v['status']} шаг {v['steps']} "
                                                     f"{v['last'][:50]}" for v in live)[:300])
        time.sleep(1.0)


def cleanup_session(job: dict) -> None:
    """Сессия воркера не должна засорять /resume пользователя: после успеха удаляем её файл."""
    base = os.path.expanduser("~/.claude/projects")
    try:
        for proj in os.listdir(base):
            path = os.path.join(base, proj, job["session_id"] + ".jsonl")
            if os.path.isfile(path):
                os.remove(path)
    except OSError:
        pass
