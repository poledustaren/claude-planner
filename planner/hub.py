"""События плана в Agent Hub: планер сам сообщает раннеру своего хоста, что с планом.

В сессии хаба раннер кладёт в окружение HUB2_PLAN_URL, HUB2_HOOK_TOKEN_FILE и HUB2_TASK (agent-hub-v2,
docs/API.md, «События планера»). По событию хаб ведёт задачу в Plane: пауза → «На уточнении», план
закрыт → «В проверке», прогресс пунктов виден на карточке.

Сессия вне хаба (обычный терминал, IDE) тоже видна на доске, если на хосте есть раннер (~/.hub2):
событие уходит по id задачи Plane, которую ведёт план, с блоком external — хаб показывает такую
сессию карточкой, но не запускает и не останавливает её. Доставка — по возможности: сбой инструмент
планера не ломает.
"""
from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request

from . import core

ENV_URL, ENV_TOKEN_FILE, ENV_TASK = "HUB2_PLAN_URL", "HUB2_HOOK_TOKEN_FILE", "HUB2_TASK"
HUB_CONFIG = "~/.hub2/config.json"
OWNERS_DIR = "~/.cache/claude-planner/owners"  # хук track пишет сюда, какая сессия ведёт план папки
PHASES = ("busy", "idle", "ended")
TIMEOUT = 2


def _token(path: str) -> str:
    try:
        with open(os.path.expanduser(path), encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _hub_session() -> tuple[str, str, str] | None:
    """(url, токен, задача) сессии, запущенной хабом, или None."""
    url, task = os.environ.get(ENV_URL, "").strip(), os.environ.get(ENV_TASK, "").strip()
    token_file = os.environ.get(ENV_TOKEN_FILE, "").strip()
    if not (url.startswith("http://") and task and token_file):
        return None
    token = _token(token_file)
    return (url, token, task) if token else None


def _local_runner() -> tuple[str, str] | None:
    """(url, файл токена) раннера хаба на этом хосте по ~/.hub2/config.json; None — хаба здесь нет."""
    try:
        with open(os.path.expanduser(HUB_CONFIG), encoding="utf-8") as fh:
            cfg = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(cfg, dict):
        return None
    try:
        port = int(cfg.get("port") or 7920)
    except (TypeError, ValueError):
        port = 7920
    binds = [str(b) for b in cfg.get("bind") or [] if b] if isinstance(cfg.get("bind"), list) else []
    host = "127.0.0.1" if not binds or "127.0.0.1" in binds or "0.0.0.0" in binds else binds[0]
    hub_dir = os.path.expanduser(str(cfg.get("hubDir") or os.path.dirname(HUB_CONFIG)))
    return f"http://{host}:{port}/api/runner/plan", os.path.join(hub_dir, "hook-token")


def external(plan: dict | None) -> bool:
    """План ведёт сессия вне хаба: событие пойдёт с блоком external."""
    return _hub_session() is None and not os.environ.get(ENV_TASK, "").strip() and bool(plan)


def target(plan: dict | None = None) -> tuple[str, str, str] | None:
    """(url, токен, задача): сессия хаба — из окружения; вне хаба — раннер этого хоста и задача Plane плана."""
    inner = _hub_session()
    if inner is not None or not external(plan):
        return inner
    issue = str(((plan or {}).get("plane") or {}).get("issue_id") or "").strip()
    runner = _local_runner() if issue else None
    if runner is None:
        return None
    token = _token(runner[1])
    return (runner[0], token, issue) if token else None


def owner_path(root: str) -> str:
    digest = hashlib.sha1(os.path.realpath(root).encode()).hexdigest()[:16]
    return os.path.join(os.path.expanduser(OWNERS_DIR), digest + ".json")


def _owner(plan: dict) -> dict:
    """Сессия Claude, которая ведёт план: записана хуком track; чужая запись (другой план) не годится."""
    try:
        with open(owner_path(plan.get("root") or ""), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) and data.get("plan") == plan.get("created_at") else {}


def _external(plan: dict, phase: str | None) -> dict:
    owner, info = _owner(plan), plan.get("plane") or {}
    out = {"agent": "claude", "cwd": plan.get("root") or "",
           "label": str(info.get("issue_key") or plan.get("project") or os.path.basename(plan.get("root") or ""))[:100],
           "phase": phase if phase in PHASES else "idle" if plan.get("status") == "finished" or plan.get("hold") else "busy"}
    if owner.get("session_id"):
        out["sessionId"] = str(owner["session_id"])
    if owner.get("transcript_path"):
        out["transcriptPath"] = str(owner["transcript_path"])
    return out


def _details(snap: dict) -> dict:
    """Структура процесса из snapshot(): пункты, воркеры, брифы разведки."""
    out = {"goal": snap.get("goal") or "", "next": snap.get("next") or "",
           "updatedAt": snap.get("updatedAt") or ""}
    if snap.get("root"):
        out["planDir"] = core.pdir(snap["root"])  # хаб показывает файлы плана во вкладке «Артефакты»
    if snap.get("summary"):
        out["summary"] = snap["summary"]
    items = []
    for i in (snap.get("items") or [])[:60]:
        one = {"id": i.get("id") or "", "title": i.get("title") or "", "status": i.get("status") or "",
               "tier": i.get("tier") or "", "wave": i.get("wave") or 0,
               "acceptance": i.get("acceptance") or "", "files": i.get("files") or [],
               "dependsOn": i.get("dependsOn") or []}
        if i.get("light"):
            one["light"] = i["light"]
        if i.get("evidence"):
            one["evidence"] = i["evidence"]
        if i.get("agent"):
            one["agent"] = i["agent"]
        items.append(one)
    out["items"] = items
    jobs = []
    for j in (snap.get("jobs") or [])[:24]:
        one = {"id": j.get("id") or j.get("job") or "", "item": j.get("item") or "",
               "kind": j.get("kind") or "", "tier": j.get("tier") or "", "model": j.get("model") or "",
               "status": j.get("status") or "", "running": bool(j.get("running", j.get("isRunning"))),
               "sec": j.get("sec") or 0, "steps": j.get("steps") or 0, "cost": j.get("cost") or 0,
               "last": (j.get("last") or "")[:160]}
        if j.get("report"):
            one["report"] = j["report"][:1500]
        if j.get("reason"):
            one["reason"] = j["reason"]
        jobs.append(one)
    out["jobs"] = jobs
    scouts = []
    for s in (snap.get("scouts") or [])[:4]:
        one = {"id": s.get("id") or "", "question": (s.get("question") or "")[:300],
               "model": s.get("model") or "", "status": s.get("status") or "", "sec": s.get("sec") or 0}
        if s.get("brief"):
            one["brief"] = s["brief"][:1500]
        scouts.append(one)
    out["scouts"] = scouts
    return out


def event(plan: dict, details: dict | None = None, phase: str | None = None) -> dict:
    body = {"status": plan.get("status") or "active", "hold": plan.get("hold") or "",
            "done": sum(i["status"] in core.CLOSED for i in plan.get("items", [])),
            "total": len(plan.get("items", [])), "title": plan.get("title") or ""}
    if details is not None:
        body["details"] = _details(details)
    if external(plan):
        body["external"] = _external(plan, phase)
    return body


def notify(plan: dict | None, details: dict | None = None, phase: str | None = None) -> bool:
    """Сообщить хабу состояние плана. True — событие принято. phase — фаза сессии вне хаба (хуки)."""
    dest = target(plan) if plan else None
    if dest is None:
        return False
    url, token, task = dest
    body = json.dumps(event(plan, details, phase), ensure_ascii=False).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Authorization": f"Bearer {token}", "X-Hub2-Task": task,
                                          "Content-Type": "application/json"})
    try:
        # Раннер всегда на этом же хосте: прокси из окружения сессии не нужен и только мешает.
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=TIMEOUT) as resp:
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as exc:
        exc.close()  # раннер отказал (нет такой сессии) — это не сбой планера
        return False
    except (OSError, ValueError):
        return False
