"""Операции планера, как их видит оркестратор: каждая возвращает короткий текст с итогом
и детерминированной подсказкой следующего шага. MCP-сервер только раскладывает вызовы сюда.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time

from . import core, outline, plane, workers

REPORT_CAP = 3500
WIKI_TEXT_CAP = 6000
DIFF_CAP = 30000


# ───────────────────────── подсказка следующего шага ─────────────────────────

def next_hint(root: str, plan: dict) -> str:
    if plan["status"] == "finished":
        return "План закрыт."
    if plan["status"] == "ready":
        return "Дальше: plan_finish(summary)."
    if plan["status"] == "docs_pending":
        return ("Дальше: документация. wiki_publish(project_dir, title, notes=\"решения и грабли из разговора, "
                "которых нет в плане\") — страницу напишет воркер по плану и диффу, тебе вернётся ссылка. Тема уже "
                "описана — дай тот же title: воркер обновит страницу. После — plan_finish(summary). Вики недоступна "
                "или пользователь от неё отказался — plan_finish(summary, user_approval=<его цитата>).")
    if plan["status"] == "review_pending":
        if core.review_stuck(plan):
            return ("Лимит раундов приёмки исчерпан: покажи пользователю блокеры и спроси решение; "
                    "закрыть можно только plan_finish(summary, user_approval=<цитата>).")
        rounds = core.review_rounds(plan)
        if rounds and rounds[-1]["verdict"] == "rejected" and rounds[-1]["ts"] >= core.last_closed_at(plan):
            return ("Приёмка отклонена: под каждый блокер добавь пункт доработки plan_update(add=[...]) со своей "
                    "проверкой либо оспорь через plan_review(notes=...).")
        return ("Дальше: plan_review(). Пользователь решил закрыть без приёмки — "
                "plan_finish(summary, user_approval=<его цитата>).")
    active = workers.active_jobs(root)
    ready = [i["id"] for i in core.ready_items(plan)]
    mains = [i["id"] for i in plan["items"] if i["status"] == "in_progress" and i["tier"] == "main"]
    failed = [i["id"] for i in plan["items"] if i["status"] == "failed"]
    easy = [i["id"] for i in plan["items"] if i.get("light") and i["status"] in ("todo", "in_progress")]
    ready = [i for i in ready if i not in easy]
    mains = [i for i in mains if i not in easy]
    parts = []
    if easy:
        parts.append(f"пункты без проверки {', '.join(easy)}: сделай и закрой plan_verify(item_ids=[...], "
                     "note=\"что сделал и чем убедился\")")
    if ready:
        parts.append(f"готовы к старту {', '.join(ready)}: plan_run()")
    if mains and active:
        parts.append(f"пока работают воркеры ({len(active)}), делай свои пункты main {', '.join(mains)} и закрывай "
                     "их plan_verify(); после этого plan_run() дождётся воркеров")
    elif mains:
        parts.append(f"пункты main {', '.join(mains)} делаешь сам, затем plan_verify()")
    elif active:
        parts.append(f"работают воркеры ({len(active)}): plan_run() дождётся ближайшего")
    if failed:
        parts.append(f"упали {', '.join(failed)}: plan_show(item_id) → plan_run(item_ids) вернёт воркеру лог "
                     "провала, plan_worker(steer) — с подсказкой, plan_update — если неверен сам пункт; пункт "
                     "больше не нужен или проверка потеряла смысл — спроси пользователя и закрой plan_skip")
    if plan.get("hold"):
        parts.append(f"план на паузе: {plan['hold']}")
    return "Дальше: " + ("; ".join(parts) if parts else "добавь пункты plan_update(add=[...]).")


def _tail(root: str, extra: str = "") -> str:
    plan = core.load(root)
    return "\n".join(x for x in (extra, "План: " + core.status_line(plan), next_hint(root, plan)) if x)


# ───────────────────────── план ─────────────────────────

def init(project_dir: str, title: str, goal: str, items, review="auto", track="auto", passport: dict | None = None,
         docs="auto") -> str:
    root = core.project_root(project_dir)
    if any(j["kind"] != "scout" for j in workers.active_jobs(root)):
        raise core.PlanError("В проекте ещё работают воркеры прошлого плана: plan_jobs(wait=true, mode=\"all\").")
    plan = core.init(root, title, goal, items, review=review, track=track, docs=docs)
    _prune(root)
    notes = core.advice(plan)
    sync = plane.sync(root, passport)
    waves = core.compute_waves(plan["items"])
    head = (f"План создан в {root}/{core.DIR}: {len(plan['items'])} пунктов, волн {max(waves.values())}, "
            f"приёмка {'нужна' if core.needs_review(plan) else 'не нужна'}, "
            f"вики {'нужна' if core.needs_docs(plan) else 'не требуется'}.")
    return _tail(root, "\n".join([head, *[f"Замечание: {n}" for n in notes], sync]).strip())


def _prune(root: str) -> None:
    """Новый план начинается с чистого листа: логи проверок и задания прошлого плана удаляются,
    свежая разведка (она идёт до plan_init) остаётся и попадает в стоимость плана."""
    shutil.rmtree(core.pdir(root, "evidence"), ignore_errors=True)
    for job in workers.list_jobs(root):
        if job["kind"] != "scout" or time.time() - job.get("started", 0) > 6 * 3600:
            workers.cleanup_session(job)
            shutil.rmtree(os.path.join(workers.jobs_dir(root), job["id"]), ignore_errors=True)


def update(project_dir: str, add=None, changes=None, hold=None, title=None, goal=None, remove=None,
           user_approval=None, branch=None) -> str:
    root = core.project_root(project_dir)
    plan = core.update(root, add=add, changes=changes, hold=hold, title=title, goal=goal, remove=remove,
                       user_approval=user_approval, branch=branch)
    notes = [f"Замечание: {n}" for n in core.advice(plan)] if add or changes or remove else []
    return _tail(root, "\n".join(["План обновлён.", *notes, plane.sync(root)]).strip())


def show(project_dir: str, item_id=None) -> str:
    root = core.project_root(project_dir)
    plan = core.load(root)
    if item_id not in (None, ""):
        return core.item_detail(plan, item_id)
    jobs = [workers.view(j) for j in workers.active_jobs(root)]
    return "\n".join(x for x in (core.render(plan).rstrip(), "Воркеры:\n" + workers.table(jobs) if jobs else "",
                                 next_hint(root, plan)) if x)


def skip(project_dir: str, item_id, reason: str, user_approval: str, item_ids=None, outcome: str = "skipped") -> str:
    root = core.project_root(project_dir)
    ids = [str(i).strip() for i in ([item_id] if item_id not in (None, "") else []) + list(item_ids or [])]
    core.approval_quote(user_approval, "Закрыть пункт без проверки")  # до остановки воркеров
    plan = core.load(root)
    for iid in ids:
        core.find(plan, iid)
    for job in workers.active_jobs(root):  # иначе надзиратель вернёт пункту статус уже после закрытия
        if str(job.get("item_id")) in ids:
            workers.kill(root, job, "пункт закрыт решением пользователя")
    core.skip(root, ids, reason, user_approval, outcome)
    what = "приняты пользователем без проверки" if outcome == "done" else "пропущены"
    return _tail(root, f"Пункты {', '.join(ids)}: {what}. {plane.sync(root)}".strip())


# ───────────────────────── исполнение ─────────────────────────

def _start_item(root: str, plan: dict, item: dict, fresh: bool, carry: str = "") -> str:
    """Стартует пункт: main — помечает в работе, воркер — запускает. Возвращает строку отчёта."""
    if item["tier"] == "main":
        core.set_status(root, item["id"], "in_progress", job="")
        return f"{item['id']}: main — делаешь сам"
    prev = [j for j in workers.list_jobs(root) if str(j.get("item_id")) == item["id"]]
    last_ev = item["evidence"][-1] if item["evidence"] else None
    resume = (not fresh and prev and prev[-1]["status"] == "FAILED" and last_ev and not last_ev["passed"]
              and str(prev[-1].get("reason", "")).startswith("проверки не прошли"))
    job = workers.spawn(
        root, "item", item["tier"], f"i{item['id']}",
        workers.fix_prompt(item, last_ev) if resume else workers.item_prompt(plan, item) + carry, workers.SYS_CODE,
        item_id=item["id"],
        resume_session=prev[-1]["session_id"] if resume else None,
        on_created=lambda j: core.set_status(root, item["id"], "in_progress", job=j["id"]))
    return f"{item['id']}: {item['tier']} ({job['model']}) → {job['id']}" + (" (доработка в той же сессии)" if resume else "")


def _finished_report(root: str, jobs: list[dict]) -> list[str]:
    lines = []
    for job in jobs:
        if job["status"] in workers.ACTIVE or job["kind"] != "item":
            continue
        v = workers.view(job)
        if job["status"] == "DONE":
            lines.append(f"Пункт {job['item_id']}: DONE, проверки прошли ({v['sec']}s, ${v['cost']})."
                         + (f" Правил вне пункта: {', '.join(v['out_of_scope'][:5])}." if v["out_of_scope"] else ""))
        else:
            lines.append(f"Пункт {job['item_id']}: {job['status']} — {v['reason']}\n  отчёт воркера: "
                         + (v["report"][:500].replace("\n", "\n  ") or "нет"))
    return lines


def _escalate(root: str, finished: list[dict]) -> list[str]:
    """Пункт на младшей модели не прошёл проверки — без участия оркестратора перезапускается ярусом выше."""
    lines = []
    for job in finished:
        if job["kind"] != "item" or job["status"] != "FAILED" or "BLOCKED" in str(job.get("reason", "")):
            continue
        was = job["tier"]
        up = core.escalate(root, str(job["item_id"]))
        if not up:
            continue
        plan = core.load(root)
        item = core.find(plan, job["item_id"])
        ev = item["evidence"][-1] if item["evidence"] else None
        carry = ("\n\nПРЕДЫДУЩАЯ ПОПЫТКА младшей модели не прошла проверки, её правки остались в файлах — проверь "
                 "их и доведи:\n" + core.fail_report(item, ev, 800)) if ev and not ev["passed"] else ""
        try:
            lines.append(f"Пункт {item['id']}: {was} не справился — повышен: " + _start_item(root, plan, item, True, carry))
        except core.PlanError as exc:
            lines.append(f"Пункт {item['id']}: повышен до {up}, старт отложен ({exc}) — plan_run(item_ids=[\"{item['id']}\"]).")
    return lines


def run(project_dir: str, item_ids=None, wait: bool | None = None, timeout: int = 600, fresh: bool = False,
        progress=None) -> str:
    root = core.project_root(project_dir)
    plan = core.load(root)
    if plan["status"] == "finished":
        raise core.PlanError("План закрыт. Новая работа — plan_init или plan_update(add=[...]).")
    explicit = [str(i) for i in item_ids] if item_ids else None
    targets = [core.find(plan, i) for i in explicit] if explicit else core.ready_items(plan)
    started, refused = [], []
    for item in targets:
        plan = core.load(root)
        item = core.find(plan, item["id"])
        gate = core.start_gate(plan, item)
        if gate:
            refused.append(f"{item['id']}: {gate}")
            continue
        try:
            started.append(_start_item(root, plan, item, fresh))
        except core.PlanError as exc:
            refused.append(f"{item['id']}: {exc}")
    before = {j["id"] for j in workers.active_jobs(root)}
    finished = []
    if wait is None:  # свои пункты main оркестратор делает параллельно с воркерами, а не после них
        wait = not any(i["status"] == "in_progress" and i["tier"] == "main" for i in core.load(root)["items"])
    if wait and before:
        jobs = workers.wait(root, sorted(before), "any", timeout, progress)
        finished = [j for j in jobs if j["status"] not in workers.ACTIVE]
    out = []
    if started:
        out.append("Запущено: " + "; ".join(started))
    if refused:
        out.append("Не запущено: " + "; ".join(refused))
    out += _finished_report(root, finished)
    out += _escalate(root, finished)
    live = [workers.view(j) for j in workers.active_jobs(root)]
    if live:
        out.append("Работают:\n" + workers.table(live))
    if finished:
        out.append(plane.sync(root))
    return _tail(root, "\n".join(x for x in out if x))


def jobs(project_dir: str, wait: bool = False, mode: str = "any", timeout: int = 600, progress=None) -> str:
    root = core.project_root(project_dir)
    if mode not in ("any", "all"):
        raise core.PlanError("mode: any (первый завершившийся) или all.")
    watched = {j["id"] for j in workers.active_jobs(root)}
    finished = []
    if wait and watched:
        finished = [j for j in workers.wait(root, sorted(watched), mode, timeout, progress)
                    if j["status"] not in workers.ACTIVE]
    recent = [j for j in workers.list_jobs(root) if j["status"] in workers.ACTIVE or j["id"] in watched
              or time.time() - j.get("ended", 0) < 900]
    has_plan = core.load(root, required=False) is not None
    out = _finished_report(root, finished) + (_escalate(root, finished) if has_plan else [])
    out.append(workers.table([workers.view(j) for j in recent[-10:]]))
    return _tail(root, "\n".join(out)) if has_plan else "\n".join(out)


def verify(project_dir: str, item_ids=None, note: str | None = None) -> str:
    root = core.project_root(project_dir)
    plan = core.load(root)
    busy = {str(j["item_id"]) for j in workers.active_jobs(root) if j.get("item_id")}
    if item_ids:
        ids = [str(i) for i in item_ids]
        clash = [i for i in ids if i in busy]
        if clash:
            raise core.PlanError(f"Пункты {', '.join(clash)} ещё у воркеров — их проверит надзиратель. Жди plan_run().")
    else:
        ids = [i["id"] for i in plan["items"] if i["id"] not in busy and
               (i["status"] in ("in_progress", "failed") or (note and i.get("light") and i["status"] == "todo"))]
    if not ids:
        raise core.PlanError("Нечего проверять: нет пунктов в работе без воркера.")
    lines = []
    for item, ev in core.verify_items(root, ids, note=note):
        if item.get("light"):
            lines.append(f"Пункт {item['id']}: закрыт без проверки ({core.LIGHT[item['light']]}) — {ev['note'][:160]}")
            continue
        lines.append(f"Пункт {item['id']}: PASS → done (лог {ev['log']})" if ev["passed"] else core.fail_report(item, ev))
    return _tail(root, "\n".join(lines + [plane.sync(root)]).strip())


def worker_ctl(project_dir: str, action: str, ref, hint: str | None = None, full: bool = False) -> str:
    root = core.project_root(project_dir)
    if action not in ("inspect", "logs", "kill", "retry", "steer"):
        raise core.PlanError("action: inspect | logs | kill | retry | steer.")
    job = workers.resolve(root, ref)
    if action == "inspect":
        v = workers.view(job, trail=12)
        return "\n".join([workers.table([v]), "Правил: " + (", ".join(v["touched"]) or "—"),
                          "Последние шаги:\n" + "\n".join(f"  {s}" for s in v["trail"])])
    if action == "logs":
        v = workers.view(job, trail=200 if full else 30)
        return "\n".join([workers.table([v]), "Отчёт:\n" + (v["report"][:8000 if full else REPORT_CAP] or "—"),
                          "Шаги:\n" + "\n".join(f"  {s}" for s in v["trail"])])
    if action == "kill":
        job = workers.kill(root, job)
        return _tail(root, f"{job['id']}: {job['status']}.")
    if job["kind"] != "item":
        raise core.PlanError("retry и steer работают только для пунктов плана.")
    if action == "steer" and not (hint or "").strip():
        raise core.PlanError("steer требует hint: что воркер делает не так и как надо.")
    job = workers.kill(root, job, "перезапуск оркестратором")
    plan = core.load(root)
    item = core.find(plan, job["item_id"])
    if item["status"] in core.CLOSED:
        raise core.PlanError(f"Пункт {item['id']} уже {item['status']}; переделать — plan_update(update=[{{id, reopen:true}}]).")
    gate = core.start_gate(plan, {**item, "status": "todo"})
    if gate:
        raise core.PlanError(f"Пункт {item['id']} не стартует: {gate}.")
    started = bool(workers.telemetry(root, job["id"])["ctx"])
    resume = action == "steer" and started
    if hint and not resume:
        with core.mutate(root) as plan:
            it = core.find(plan, item["id"])
            it["context"] = (it["context"] + f"\n[STEER] {hint.strip()}").strip()
            item = dict(it)
    prompt = (f"Корректировка от оркестратора: {hint.strip()}\nПродолжи пункт с её учётом и заверши отчётом "
              "в том же формате.") if resume else workers.item_prompt(plan, item)
    new = workers.spawn(root, "item", item["tier"], f"i{item['id']}", prompt, workers.SYS_CODE, item_id=item["id"],
                        resume_session=job["session_id"] if resume else None,
                        on_created=lambda j: core.set_status(root, item["id"], "in_progress", job=j["id"]))
    return _tail(root, f"Пункт {item['id']}: {'корректировка в той же сессии' if resume else 'перезапуск с нуля'} → {new['id']}.")


def scout(project_dir: str, questions, wait: bool = True, timeout: int = 300, progress=None) -> str:
    root = core.project_root(project_dir)
    if isinstance(questions, str):
        questions = [questions]
    if not isinstance(questions, list) or not 1 <= len(questions) <= workers.MAX_PARALLEL \
            or not all(isinstance(q, str) and len(q.strip()) >= 10 for q in questions):
        raise core.PlanError(f"questions: от 1 до {workers.MAX_PARALLEL} конкретных вопросов (каждый ≥ 10 символов), "
                             "по одному на подсистему.")
    core._ensure_dir(root)
    started = [workers.spawn(root, "scout", "scout", f"scout{n}", q.strip(), workers.SYS_SCOUT)
               for n, q in enumerate(questions, 1)]
    if not wait:
        return "Разведчики запущены: " + ", ".join(j["id"] for j in started) + ". Отчёты: plan_jobs(wait=true, mode=\"all\")."
    done = workers.wait(root, [j["id"] for j in started], "all", timeout, progress)
    out = []
    for job, question in zip(done, questions):
        v = workers.view(job)
        head = f"## {question.strip()[:120]}\n({v['model']}, {v['status']}, {v['sec']}s, шагов {v['steps']}, ${v['cost']})"
        body = v["report"][:REPORT_CAP] or f"отчёта нет: {v['reason'] or 'ещё работает — plan_jobs(wait=true)'}"
        core.atomic_write(core.pdir(root, "scouts", job["id"] + ".md"), f"{head}\n\n{v['report']}\n")
        out.append(f"{head}\n{body}")
    return "\n\n".join(out)


# ───────────────────────── приёмка ─────────────────────────

def _git(root: str, *args: str) -> str:
    try:
        return subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def git_summary(root: str, base_rev: str, cap: int = DIFF_CAP) -> str:
    if not base_rev:
        return ""
    stat = _git(root, "diff", "--stat", base_rev, "--", ".", f":(exclude){core.DIR}")
    diff = _git(root, "diff", base_rev, "--", ".", f":(exclude){core.DIR}")
    parts = []
    if stat.strip():
        parts.append("ИЗМЕНЕНО:\n" + stat.strip())
    budget = cap
    if diff.strip():
        cut = diff[:budget]
        parts.append("DIFF" + (f" (обрезан до {cap} символов, остальное: git diff {base_rev[:12]} -- <файл>)"
                               if len(diff) > budget else "") + ":\n" + cut)
        budget -= len(cut)
    new = [f for f in _git(root, "ls-files", "--others", "--exclude-standard").splitlines()
           if f.strip() and "__pycache__/" not in f and "node_modules/" not in f and not f.endswith((".pyc", ".log"))]
    if new:
        parts.append("НОВЫЕ ФАЙЛЫ (вне git, читай сам): " + ", ".join(new[:40]))
        for rel in new[:12]:
            try:
                with open(os.path.join(root, rel), encoding="utf-8") as fh:
                    text = fh.read(6000)
            except (OSError, UnicodeDecodeError):
                continue
            if budget <= 500:
                break
            parts.append(f"--- {rel}\n{text[:budget]}")
            budget -= len(text)
    return "\n\n".join(parts)


def review_prompt(plan: dict, notes: str | None, screenshots: list | None) -> str:
    rounds = core.review_rounds(plan)
    parts = [f"ЦЕЛЬ ПЛАНА: {plan['goal']}", "ПУНКТЫ:"]
    for it in plan["items"]:
        ev = it["evidence"][-1] if it["evidence"] else None
        stamp = it.get("manual") or (it.get("skip") if it["status"] == "skipped" else None)
        parts.append(f"- {it['id']} [{it['status']}] {it['title']}\n  приёмка: {it['acceptance']}\n  файлы: "
                     f"{', '.join(it['files']) or '—'}\n  проверки: "
                     + "; ".join(core.check_label(c) for c in it["verify"])
                     + (f"\n  закрыт решением пользователя без проверки ({stamp['reason']}) — это не дефект"
                        if stamp else
                        f"\n  проверки перезапущены только что: {'PASS' if ev['passed'] else 'FAIL'}" if ev else ""))
    if rounds and rounds[-1]["verdict"] == "rejected":
        parts.append("ПРОШЛЫЙ РАУНД ОТКЛОНЁН, блокеры были:\n" + "\n".join(
            f"- {i.get('file', '')}:{i.get('line', '')} {i['problem']}" for i in rounds[-1]["issues"]
            if i.get("severity") == "blocker") + "\nПроверь, что они устранены, и не выдвигай заново снятые.")
    if notes:
        parts.append("ЗАМЕЧАНИЯ ОРКЕСТРАТОРА: " + notes.strip())
    if screenshots:
        parts.append("СКРИНШОТЫ (открой Read и оцени глазами: наложения, обрезки, соответствие задаче): "
                     + ", ".join(str(s) for s in screenshots))
    summary = git_summary(plan["root"], plan.get("base_rev") or "")
    parts.append(summary or "Диффа нет (проект вне git): читай файлы пунктов сам.")
    return "\n\n".join(parts)


def _collect_review(root: str, job: dict) -> str:
    v = workers.view(job)
    data = v["structured"] if isinstance(v["structured"], dict) else None
    with core.mutate(root) as plan:
        rev = plan.setdefault("review", {})
        rev.pop("job", None)
        if job["status"] != "DONE" or data is None:
            core.event(plan, "review_error", note=v["reason"] or "нет структурного вердикта")
            return (f"Приёмщик не дал вердикт ({job['status']}: {v['reason'] or 'ответ не по схеме'}). Раунд не "
                    "засчитан — повтори plan_review().")
        issues = [i for i in data.get("issues") or [] if isinstance(i, dict) and i.get("problem")]
        blockers = [i for i in issues if i.get("severity") == "blocker"]
        verdict = "rejected" if blockers else "approved"
        rounds = rev.setdefault("rounds", [])
        rounds.append({"n": len(rounds) + 1, "ts": core.now(), "job": job["id"], "verdict": verdict, "issues": issues})
        if verdict == "approved":
            rev["approved_at"] = core.now()
        core.event(plan, "review_" + verdict)
    workers.cleanup_session(job)

    def fmt(i):
        where = f"{i.get('file', '')}:{i.get('line', '')}".strip(":")
        return (f"- [{i.get('severity')}] " + (f"п.{i['item_id']} " if i.get("item_id") else "")
                + (f"{where} — " if where else "") + i["problem"] + (f" → {i['fix']}" if i.get("fix") else ""))
    head = f"Приёмка, раунд {len(rounds)}: {verdict.upper()} ({v['model']}, {v['sec']}s, ${v['cost']})."
    return "\n".join([head, *[fmt(i) for i in blockers], *[fmt(i) for i in issues if i not in blockers][:8]])


def review(project_dir: str, wait: bool = True, timeout: int = 900, notes: str | None = None,
           screenshots: list | None = None, progress=None) -> str:
    root = core.project_root(project_dir)
    plan = core.load(root)
    pending = (plan.get("review") or {}).get("job")
    job = workers.read_job(root, pending) if pending else None
    if job is None:
        opened = [i["id"] for i in plan["items"] if i["status"] in core.OPEN]
        if opened or not plan["items"]:
            raise core.PlanError(f"Приёмка возможна, когда все пункты закрыты. Открыты: {', '.join(opened) or 'нет пунктов'}.")
        if core.review_approved(plan):
            raise core.PlanError("Приёмка уже пройдена и после неё ничего не менялось. Дальше: plan_finish(summary).")
        if core.review_stuck(plan):
            raise core.PlanError(next_hint(root, plan))
        rounds = core.review_rounds(plan)
        if rounds and rounds[-1]["verdict"] == "rejected" and rounds[-1]["ts"] >= core.last_closed_at(plan) \
                and not (notes or "").strip():
            raise core.PlanError(next_hint(root, plan))
        done_ids = core.recheckable(plan)
        broken = [(it, ev) for it, ev in core.verify_items(root, done_ids, by="review", recheck=True)
                  if not ev["passed"]] if done_ids else []
        if broken:
            raise core.PlanError("Перед приёмкой проверки прогнаны заново — есть регрессия, пункты вернулись в failed:\n"
                                 + "\n".join(core.fail_report(it, ev, 600) for it, ev in broken))
        plan = core.load(root)
        job = workers.spawn(root, "review", "review", "review", review_prompt(plan, notes, screenshots),
                            workers.SYS_REVIEW, schema=workers.REVIEW_SCHEMA)
        with core.mutate(root) as plan:
            plan.setdefault("review", {})["job"] = job["id"]
            core.event(plan, "review_start")
    if job["status"] in workers.ACTIVE:
        if not wait:
            return f"Приёмщик работает: {job['id']}. Результат: plan_review()."
        (job,) = workers.wait(root, [job["id"]], "all", timeout, progress) or (job,)
        if job["status"] in workers.ACTIVE:
            return f"Приёмщик ещё работает ({job['id']}). Повтори plan_review() позже."
    return _tail(root, _collect_review(root, job) + "\n" + plane.sync(root))


def _outline():
    conn = outline.connect()
    if conn is None:
        raise core.PlanError("Outline не настроен: нужен ~/.config/claude-planner/outline.env (или agy-planner) с "
                             "OUTLINE_URL, OUTLINE_API_TOKEN, OUTLINE_COLLECTION.")
    return conn


def wiki_pages(project_dir: str, include_text: bool = False) -> str:
    root = core.project_root(project_dir)
    client, collection = _outline()
    project = os.path.basename(root)
    try:
        found = outline.list_pages(client, collection, project, include_text=include_text)
    except outline.OutlineError as exc:
        raise core.PlanError(str(exc)) from None
    if found is None:
        return (f"В коллекции {collection} ещё нет страницы «{outline.root_title(project)}». wiki_publish создаст её "
                "вместе с первой страницей проекта.")
    lines = [f"Страницы проекта в коллекции {collection}:"]
    for page in found["pages"]:
        lines.append(f"{'  ' * page['depth']}- {page['title']} — {page['url']} (обновлена {page.get('updatedAt') or '?'})"
                     + (" [защищена: правки уйдут в журнал]" if page.get("protected") else ""))
        if include_text and page.get("text"):
            lines.append("\n".join("    " + ln for ln in page["text"][:WIKI_TEXT_CAP].splitlines()))
    if found.get("truncated"):
        lines.append("… список обрезан")
    return "\n".join(lines)


def docs_prompt(root: str, plan: dict, title: str, notes, current: str) -> str:
    parts = [f"СТРАНИЦА: {title}", f"ПЛАН: {plan['title']}\nЦЕЛЬ: {plan['goal']}", "ПУНКТЫ:"]
    for it in plan["items"]:
        ev = it["evidence"][-1] if it["evidence"] else None
        proof = (f"без проверки ({core.LIGHT[it['light']]}): {it.get('done_note') or ''}" if it.get("light") else
                 "проверки: " + "; ".join(core.check_label(c) for c in it["verify"])
                 + (f" — {'PASS' if ev['passed'] else 'FAIL'}" if ev else ""))
        parts.append(f"- {it['id']} [{it['status']}] {it['title']}\n  результат: {it['acceptance']}\n  файлы: "
                     f"{', '.join(it['files']) or '—'}\n  {proof}")
    reports = [f"- пункт {v['item']}: {v['report'][:1200]}" for v in map(workers.view, workers.list_jobs(root))
               if v["kind"] == "item" and v["status"] == "DONE" and v["report"].strip()]
    if reports:
        parts.append("ОТЧЁТЫ ИСПОЛНИТЕЛЕЙ:\n" + "\n".join(reports[-12:]))
    if isinstance(notes, (list, tuple)):
        notes = "\n".join(f"- {n}" for n in notes if str(n).strip())
    if isinstance(notes, str) and notes.strip():
        parts.append("ЗАМЕТКИ ОРКЕСТРАТОРА (решения и грабли из разговора с пользователем):\n" + notes.strip()[:4000])
    parts.append(git_summary(root, plan.get("base_rev") or "", cap=20000) or "Диффа нет (проект вне git): читай файлы пунктов.")
    if current.strip():
        parts.append("ТЕКУЩИЙ ТЕКСТ СТРАНИЦЫ (обнови его, а не пиши заново):\n" + current[:WIKI_TEXT_CAP])
    return "\n\n".join(parts)


def _write_docs(root: str, plan: dict, title: str, notes, client, collection: str, timeout: int, progress) -> tuple[str, str]:
    """Страницу пишет дешёвый воркер по материалам плана; оркестратор получает ссылку и оглавление."""
    current = ""
    try:
        found = outline.list_pages(client, collection, os.path.basename(root), include_text=True) or {"pages": []}
        current = next((p.get("text") or "" for p in found["pages"] if p.get("title") == title), "")
    except outline.OutlineError:
        pass
    job = workers.spawn(root, "docs", "docs", "docs", docs_prompt(root, plan, title, notes, current), workers.SYS_DOCS)
    (job,) = workers.wait(root, [job["id"]], "all", timeout, progress) or (job,)
    v = workers.view(job)
    text = re.sub(r"^```(?:markdown|md)?\s*\n|\n```\s*$", "", v["report"].strip())
    if job["status"] != "DONE" or len(text) < 200 or "## " not in text:
        why = v["reason"] or "ответ не похож на страницу"
        raise core.PlanError(f"Воркер документации не справился ({job['status']}: {why}). Повтори wiki_publish или "
                             "передай markdown сам.")
    workers.cleanup_session(job)
    heads = [ln[3:].strip() for ln in text.splitlines() if ln.startswith("## ")]
    return text, (f"Страницу написал воркер ({v['model']}, {v['sec']}s, ${v['cost']}), {len(text)} символов; "
                  f"разделы: {', '.join(heads[:10])}.")


def wiki_publish(project_dir: str, title: str, markdown: str | None = None, mode: str = "upsert",
                 parent_title: str | None = None, notes=None, timeout: int = 600, progress=None) -> str:
    root = core.project_root(project_dir)
    if not isinstance(title, str) or len(title.strip()) < 3:
        raise core.PlanError("title обязателен: заголовок страницы.")
    auto = not (isinstance(markdown, str) and markdown.strip())
    if not auto and len(markdown.strip()) < 80:
        raise core.PlanError("markdown слишком короткий: страница должна объяснять, что сделано, как устроено и как "
                             "проверять (≥ 80 символов).")
    if mode not in ("upsert", "append"):
        raise core.PlanError("mode: upsert (создать или заменить страницу) или append (дописать в конец).")
    plan = core.load(root, required=False)
    if plan and plan["status"] != "finished" and any(i["status"] in core.OPEN for i in plan["items"]):
        raise core.PlanError("Сначала закрой пункты плана: документация публикуется после последней проверки, "
                             "иначе она опишет не то, что получилось.")
    client, collection = _outline()
    wrote = ""
    if auto:
        if plan is None:
            raise core.PlanError("Без markdown страницу пишет воркер по материалам плана, а плана в проекте нет: "
                                 "передай markdown.")
        markdown, wrote = _write_docs(root, plan, title.strip(), notes, client, collection, timeout, progress)
    meta = plane.enrich_meta(root, plan)
    passport = (f"# {outline.root_title(os.path.basename(root))}\n\n- Назначение: {meta.get('features')}\n"
                f"- Стек: {meta.get('stack')}\n- Путь: `{root}`\n- Деплой и проверка: {meta.get('deploy_rules')}\n")
    try:
        res = outline.publish(client, collection, os.path.basename(root), title, markdown, mode=mode,
                              parent_title=parent_title, passport=passport)
    except outline.OutlineError as exc:
        raise core.PlanError(str(exc)) from None
    entry = {"title": res["title"], "url": res["url"], "mode": res["mode"], "doc_id": res["doc_id"]}
    meta = plane.load_meta(root)
    docs = [d for d in meta.get("outline_docs") or [] if d.get("url") != entry["url"]] + [entry]
    meta["outline_docs"] = docs
    plane.save_meta(root, meta)
    head = f"Опубликовано ({res['mode']}): {res['title']} — {res['url']}" + (f"\n{wrote}" if wrote else "")
    if plan is None:
        return head
    core.record_publication(root, entry)
    return _tail(root, "\n".join(x for x in (head, plane.sync(root)) if x))


def snapshot(project_dir: str) -> dict | None:
    """Снимок плана и воркеров для панели прогресса; None — плана нет."""
    try:
        root = core.project_root(project_dir)
        plan = core.load(root, required=False)
    except core.PlanError:
        return None
    if plan is None:
        return None
    waves = core.compute_waves(plan["items"]) if plan["items"] else {}
    jobs = [workers.view(j) for j in workers.list_jobs(root)
            if j["status"] in workers.ACTIVE or time.time() - j.get("ended", 0) < 120]
    done = sum(i["status"] in core.CLOSED for i in plan["items"])
    return {"root": root, "project": plan["project"], "title": plan["title"], "status": plan["status"],
            "done": done, "total": len(plan["items"]), "hold": plan.get("hold") or "",
            "issue": (plan.get("plane") or {}).get("issue_key") or "",
            "isActive": plan["status"] != "finished" or any(j["status"] in workers.ACTIVE for j in jobs),
            "items": [{"id": i["id"], "title": i["title"], "tier": i["tier"], "status": i["status"],
                       "wave": waves.get(i["id"], 0)} for i in plan["items"]],
            "jobs": [{"id": j["job"], "item": j["item"] or "", "kind": j["kind"], "tier": j["tier"],
                      "model": j["model"] or "", "status": j["status"], "sec": j["sec"], "steps": j["steps"],
                      "ctxK": j["ctx"] // 1000, "cost": j["cost"], "last": j["last"][:120],
                      "isRunning": j["status"] in workers.ACTIVE} for j in jobs],
            "next": next_hint(root, plan)}


def finish(project_dir: str, summary: str, user_approval: str | None = None, force: bool = False) -> str:
    root = core.project_root(project_dir)
    live = workers.active_jobs(root)
    if force:
        core.approval_quote(user_approval, "Закрыть план как есть (force)")
        for job in live:
            workers.kill(root, job, "план закрыт как есть")
    elif live:
        raise core.PlanError(f"Ещё работают воркеры ({len(live)}). Дождись plan_jobs(wait=true, mode=\"all\") или останови.")
    plan = core.finish(root, summary, user_approval, force)
    for job in workers.list_jobs(root):
        workers.cleanup_session(job)
    cost = sum(workers.view(j)["cost"] for j in workers.list_jobs(root))
    how = ("Закрыт как есть решением пользователя, проверки не гонялись." if force else
           ("Финальный прогон проверок прошёл." if core.recheckable(plan) else
            "Проверок в плане нет: пункты закрыты записью.")
           + (" Гейты сняты решением пользователя: " + "; ".join(plan["user_override"]["gates"]) + "."
              if plan.get("user_override") else ""))
    lines = [f"План «{plan['title']}» закрыт: {core.counts(plan)}. {how} Воркеры за план: ${round(cost, 3)}.",
             plane.sync(root)]
    return "\n".join(x for x in lines if x)
