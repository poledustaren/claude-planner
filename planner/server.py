#!/usr/bin/env python3
"""MCP-сервер planner: JSON-RPC 2.0 поверх stdio, только стандартная библиотека.

Каждый вызов инструмента обрабатывается в своём потоке, чтобы долгое ожидание воркеров
(plan_run / plan_jobs / plan_review с wait=true) не блокировало остальные вызовы.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from planner import core, flow  # noqa: E402

VERSION = "1.2.0"
DIRP = {"type": "string", "description": "Абсолютный путь к папке проекта"}
IDS = {"type": "array", "items": {"type": "string"}}
ITEM = {
    "type": "object", "required": ["title", "acceptance", "verify"],
    "properties": {
        "title": {"type": "string"},
        "acceptance": {"type": "string", "description": "Наблюдаемый результат, по которому видно, что сделано"},
        "verify": {"type": "array", "description": "Детерминированные проверки: строка-команда или объект "
                   "{type:cmd,cmd,timeout?,contains?} | {type:file_contains,path,pattern} | "
                   "{type:file_exists,paths} | {type:http,url,expect_status?,contains?}", "items": {}},
        "tier": {"type": "string", "enum": list(core.TIERS),
                 "description": "main — сам оркестратор; code-low — Sonnet/low (механика по образцу); "
                                "code — Sonnet/medium (по умолчанию); code-high — Sonnet/high (ветвистое)"},
        "files": {"type": "array", "items": {"type": "string"},
                  "description": "Пути от корня проекта, которые правит пункт; создаваемые — new:путь"},
        "depends_on": IDS,
        "context": {"type": "string", "description": "Ориентиры для воркера: path/file.py:L10-L80 (срез будет "
                    "вшит в его промпт), имена символов, ограничения"},
    },
}


def tool(name: str, desc: str, props: dict, required: list[str]) -> dict:
    return {"name": name, "description": desc,
            "inputSchema": {"type": "object", "properties": props, "required": required}}


TOOLS = [
    tool("plan_scout", "Разведка до планирования: 1–4 параллельных разведчика на Haiku (только чтение) отвечают "
         "на вопросы по коду и возвращают file:line, устройство, что менять и чем проверять. План не нужен.",
         {"project_dir": DIRP, "questions": {"type": "array", "items": {"type": "string"},
                                             "description": "По одному конкретному вопросу на подсистему"},
          "wait": {"type": "boolean", "default": True}, "timeout": {"type": "integer", "default": 300}},
         ["project_dir", "questions"]),
    tool("plan_init", "Создать план. Нужен для задач из 2+ пунктов; мелочь в один файл делай без плана. Сразу заводит "
         "задачу в Plane и обновляет паспорт проекта в Pages. Приёмка (review=\"auto\") включается от 4 пунктов.",
         {"project_dir": DIRP, "title": {"type": "string"},
          "goal": {"type": "string", "description": "Какой результат получит пользователь"},
          "items": {"type": "array", "items": ITEM},
          "review": {"description": "\"auto\" | true | false", "default": "auto"},
          "stack": {"type": "string", "description": "Паспорт проекта в Plane: стек (иначе определится по файлам)"},
          "features": {"type": "string", "description": "Паспорт: назначение и особенности проекта"},
          "deploy_rules": {"type": "string", "description": "Паспорт: как деплоится и проверяется"},
          "links": {"type": "string", "description": "Паспорт: репозиторий, стенды, дашборды"}},
         ["project_dir", "title", "goal", "items"]),
    tool("plan_update", "Изменить план: add — новые пункты (в т.ч. доработка после приёмки); update — правка "
         "полей пункта по id (reopen:true переоткрывает закрытый); title, goal — название и цель; remove — "
         "удалить пункты (с user_approval); hold — причина паузы (ждём пользователя), пустая строка снимает.",
         {"project_dir": DIRP, "add": {"type": "array", "items": ITEM},
          "update": {"type": "array", "items": {"type": "object"}}, "hold": {"type": "string"},
          "title": {"type": "string"}, "goal": {"type": "string"},
          "remove": IDS, "user_approval": {"type": "string", "description": "Цитата согласия пользователя (для remove)"}},
         ["project_dir"]),
    tool("plan_run", "Двигать план: стартует все готовые пункты (воркеры Sonnet параллельно, до 4; main — помечает "
         "тебе) и при wait ждёт первого завершившегося воркера. Надзиратель сам прогоняет проверки пункта и при "
         "провале один раз возвращает лог воркеру. Вызывай в цикле, пока план active.",
         {"project_dir": DIRP, "item_ids": {**IDS, "description": "Явный список; по умолчанию — все готовые"},
          "wait": {"type": "boolean", "description": "По умолчанию ждёт, если у тебя нет своих main в работе"},
          "timeout": {"type": "integer", "default": 600},
          "fresh": {"type": "boolean", "description": "Упавший пункт начать с нуля, а не дорабатывать в той же сессии"}},
         ["project_dir"]),
    tool("plan_jobs", "Состояние воркеров (шаги, контекст, стоимость, последнее действие); wait ждёт завершения: "
         "mode any — первого, all — всех.",
         {"project_dir": DIRP, "wait": {"type": "boolean", "default": False},
          "mode": {"type": "string", "enum": ["any", "all"], "default": "any"},
          "timeout": {"type": "integer", "default": 600}}, ["project_dir"]),
    tool("plan_worker", "Управление воркером по id задания или id пункта: inspect — телеметрия и последние шаги; "
         "logs — отчёт и траектория; kill; retry — заново (hint допишется в контекст пункта); steer — остановить "
         "и продолжить ту же сессию с корректировкой hint.",
         {"project_dir": DIRP, "action": {"type": "string", "enum": ["inspect", "logs", "kill", "retry", "steer"]},
          "id": {"type": "string"}, "hint": {"type": "string"}, "full": {"type": "boolean"}},
         ["project_dir", "action", "id"]),
    tool("plan_verify", "Прогнать проверки пунктов, которые делал ты сам (main) или которые упали. Единственный "
         "путь пункта в done; пункты воркеров проверяет надзиратель.",
         {"project_dir": DIRP, "item_ids": IDS}, ["project_dir"]),
    tool("plan_skip", "Закрыть пункты без проверки, только с дословной цитатой согласия пользователя: "
         "outcome=skipped — не делаем; done — результат принят как есть (проверка устарела, сделано иначе).",
         {"project_dir": DIRP, "item_id": {"type": "string"}, "item_ids": IDS, "reason": {"type": "string"},
          "outcome": {"type": "string", "enum": ["skipped", "done"], "default": "skipped"},
          "user_approval": {"type": "string"}}, ["project_dir", "reason", "user_approval"]),
    tool("plan_show", "План деревом со статусами и подсказкой следующего шага; с item_id — детали пункта и история "
         "проверок. После компактации контекста состояние бери отсюда.",
         {"project_dir": DIRP, "item_id": {"type": "string"}}, ["project_dir"]),
    tool("plan_review", "Приёмка закрытого плана независимым приёмщиком (только чтение): заново гоняет проверки, "
         "затем проверяет дифф по существу. Отклоняет только блокерами с file:line. Не больше 3 раундов.",
         {"project_dir": DIRP, "wait": {"type": "boolean", "default": True},
          "timeout": {"type": "integer", "default": 900},
          "notes": {"type": "string", "description": "Пояснения приёмщику, в т.ч. возражения на прошлые блокеры"},
          "screenshots": {"type": "array", "items": {"type": "string"},
                          "description": "Пути к скриншотам для задач с интерфейсом"}}, ["project_dir"]),
    tool("wiki_pages", "Страницы проекта в Outline (дерево «Проект · <имя>»): что уже задокументировано. Вызывай "
         "перед wiki_publish, чтобы дополнять существующее, а не плодить дубли.",
         {"project_dir": DIRP, "include_text": {"type": "boolean", "description": "Вернуть и текст страниц"}},
         ["project_dir"]),
    tool("wiki_publish", "Опубликовать документацию в Outline после закрытия пунктов. Структура страницы: Цель → Что "
         "сделано → Как устроено → Как запускать и проверять → Грабли → Ссылки. Ссылка сама попадает в задачу и "
         "паспорт проекта в Plane. Секреты маскируются, но не вставляй их.",
         {"project_dir": DIRP, "title": {"type": "string"}, "markdown": {"type": "string"},
          "mode": {"type": "string", "enum": ["upsert", "append"], "default": "upsert"},
          "parent_title": {"type": "string", "description": "Раздел проекта, под который положить страницу"}},
         ["project_dir", "title", "markdown"]),
    tool("plan_finish", "Закрыть план: финальный прогон всех проверок, перевод задачи Plane в «В проверке». Требует "
         "приёмки и свежей документации. С user_approval (цитата решения пользователя) закрывает без приёмки и "
         "вики; с force=true — как есть (план устарел): открытые пункты → skipped, проверки не гоняются.",
         {"project_dir": DIRP, "summary": {"type": "string", "description": "Что сделано и чем подтверждено"},
          "user_approval": {"type": "string"}, "force": {"type": "boolean"}},
         ["project_dir", "summary"]),
]

_out_lock = threading.Lock()


def send(msg: dict) -> None:
    with _out_lock:
        sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def log_error(text: str) -> None:
    try:
        path = os.path.expanduser("~/.cache/claude-planner/server.log")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{core.now()} {text}\n")
    except OSError:
        pass


def dispatch(name: str, a: dict, progress) -> str:
    d = a.get("project_dir")
    if name == "plan_scout":
        return flow.scout(d, a.get("questions"), a.get("wait", True), int(a.get("timeout", 300)), progress)
    if name == "plan_init":
        return flow.init(d, a.get("title"), a.get("goal"), a.get("items"), a.get("review", "auto"), "auto",
                         {k: a.get(k) for k in ("stack", "features", "deploy_rules", "links")})
    if name == "plan_update":
        return flow.update(d, a.get("add"), a.get("update"), a.get("hold"), a.get("title"), a.get("goal"),
                           a.get("remove"), a.get("user_approval"))
    if name == "plan_run":
        return flow.run(d, a.get("item_ids"), a.get("wait"), int(a.get("timeout", 600)), bool(a.get("fresh")), progress)
    if name == "plan_jobs":
        return flow.jobs(d, bool(a.get("wait")), a.get("mode", "any"), int(a.get("timeout", 600)), progress)
    if name == "plan_worker":
        return flow.worker_ctl(d, a.get("action"), a.get("id"), a.get("hint"), bool(a.get("full")))
    if name == "plan_verify":
        return flow.verify(d, a.get("item_ids"))
    if name == "plan_skip":
        return flow.skip(d, a.get("item_id"), a.get("reason"), a.get("user_approval"), a.get("item_ids"),
                         a.get("outcome") or "skipped")
    if name == "plan_show":
        return flow.show(d, a.get("item_id"))
    if name == "plan_review":
        return flow.review(d, a.get("wait", True), int(a.get("timeout", 900)), a.get("notes"), a.get("screenshots"), progress)
    if name == "wiki_pages":
        return flow.wiki_pages(d, bool(a.get("include_text")))
    if name == "wiki_publish":
        return flow.wiki_publish(d, a.get("title"), a.get("markdown"), a.get("mode", "upsert"), a.get("parent_title"))
    if name == "plan_finish":
        return flow.finish(d, a.get("summary"), a.get("user_approval"), bool(a.get("force")))
    raise core.PlanError(f"Инструмента {name!r} нет.")


def call_tool(req_id, params: dict) -> None:
    name, args = params.get("name"), params.get("arguments") or {}
    token = (params.get("_meta") or {}).get("progressToken")

    def progress(done, total, message):
        if token is not None:
            send({"jsonrpc": "2.0", "method": "notifications/progress",
                  "params": {"progressToken": token, "progress": done, "total": total, "message": message}})

    try:
        text, is_error = dispatch(name, args, progress), False
    except core.PlanError as exc:
        text, is_error = str(exc), True
    except Exception as exc:  # noqa: BLE001 — сервер не должен падать от одного вызова
        log_error(f"{name}: {traceback.format_exc()}")
        text, is_error = f"Внутренняя ошибка planner ({type(exc).__name__}: {exc}). Лог: ~/.cache/claude-planner/server.log", True
    send({"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": text}], "isError": is_error}})


def handle(msg: dict) -> None:
    method, req_id = msg.get("method"), msg.get("id")
    if method == "initialize":
        proto = (msg.get("params") or {}).get("protocolVersion") or "2024-11-05"
        send({"jsonrpc": "2.0", "id": req_id, "result": {
            "protocolVersion": proto, "capabilities": {"tools": {}},
            "serverInfo": {"name": "planner", "version": VERSION},
            "instructions": "Планер с доказательной приёмкой и воркерами Claude. Протокол — скилл planner. "
                            "Цикл: plan_scout? → plan_init → plan_run (в цикле) → plan_verify (main) → "
                            "plan_review → wiki_publish → plan_finish. Состояние после компактации — plan_show."}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}})
    elif method == "tools/call":
        threading.Thread(target=call_tool, args=(req_id, msg.get("params") or {}), daemon=True).start()
    elif method == "ping":
        send({"jsonrpc": "2.0", "id": req_id, "result": {}})
    elif req_id is not None:
        send({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Метод {method} не поддерживается"}})


def main() -> int:
    pending: list[threading.Thread] = []
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Битый JSON"}})
            continue
        before = set(threading.enumerate())
        handle(msg)
        pending += [t for t in threading.enumerate() if t not in before]
    for t in pending:  # stdin закрыт: дать вызовам дописать ответы
        t.join(timeout=30)
    return 0


if __name__ == "__main__":
    sys.exit(main())
