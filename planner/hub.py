"""События плана в Agent Hub: в сессии хаба планер сам сообщает раннеру своего хоста, что с планом.

Раннер кладёт в окружение сессии HUB2_PLAN_URL, HUB2_HOOK_TOKEN_FILE и HUB2_TASK (agent-hub-v2,
docs/API.md, «События планера»). По событию хаб ведёт задачу в Plane: пауза → «На уточнении», план
закрыт → «В проверке», прогресс пунктов виден на карточке. Вне сессии хаба переменных нет — ничего не
шлётся. Доставка — по возможности: сбой инструмент планера не ломает.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from . import core

ENV_URL, ENV_TOKEN_FILE, ENV_TASK = "HUB2_PLAN_URL", "HUB2_HOOK_TOKEN_FILE", "HUB2_TASK"
TIMEOUT = 2


def target() -> tuple[str, str, str] | None:
    """(url, токен, задача) сессии хаба или None, если сессия не из хаба."""
    url, task = os.environ.get(ENV_URL, "").strip(), os.environ.get(ENV_TASK, "").strip()
    token_file = os.environ.get(ENV_TOKEN_FILE, "").strip()
    if not (url.startswith("http://") and task and token_file):
        return None
    try:
        with open(os.path.expanduser(token_file), encoding="utf-8") as fh:
            token = fh.read().strip()
    except OSError:
        return None
    return (url, token, task) if token else None


def event(plan: dict) -> dict:
    return {"status": plan.get("status") or "active", "hold": plan.get("hold") or "",
            "done": sum(i["status"] in core.CLOSED for i in plan.get("items", [])),
            "total": len(plan.get("items", [])), "title": plan.get("title") or ""}


def notify(plan: dict | None) -> bool:
    """Сообщить хабу состояние плана. True — событие принято."""
    dest = target() if plan else None
    if dest is None:
        return False
    url, token, task = dest
    req = urllib.request.Request(url, data=json.dumps(event(plan), ensure_ascii=False).encode(), method="POST",
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
