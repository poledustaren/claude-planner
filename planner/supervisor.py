"""Надзиратель одного воркера: отдельный процесс, переживает рестарт MCP-сервера.

Цикл для пункта плана: запустить claude → дождаться → прогнать проверки пункта →
при провале вернуть лог в ту же сессию воркера (доработка) → проверить снова.
Оркестратор в этом цикле не участвует и токенов на него не тратит.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

from . import core, workers

VERIFY_WAIT_CAP = 1800


def _update(job: dict, **fields) -> dict:
    fresh = workers.read_job(job["root"], job["id"]) or job
    fresh.update(fields)
    workers.write_job(fresh)
    return fresh


def run_claude(job: dict, prompt: str, resume: bool) -> str | None:
    """Запускает одну сессию. Возвращает None при успехе или причину провала."""
    jdir = os.path.join(workers.jobs_dir(job["root"]), job["id"])
    cmd = workers.build_cmd(job, resume)
    with open(os.path.join(jdir, "stream.jsonl"), "ab") as out, open(os.path.join(jdir, "stderr.txt"), "ab") as err:
        try:
            proc = subprocess.Popen(cmd, cwd=job["root"], stdin=subprocess.PIPE, stdout=out, stderr=err,
                                    env=workers.child_env())
        except OSError as exc:
            return f"claude не запустился: {exc}"
        try:
            proc.communicate(prompt.encode("utf-8"), timeout=job["timeout"])
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            return f"таймаут {job['timeout']}s"
    tel = workers.telemetry(job["root"], job["id"])
    if proc.returncode != 0 or not tel["finished"] or tel["is_error"]:
        tail = ""
        try:
            with open(os.path.join(jdir, "stderr.txt"), encoding="utf-8", errors="replace") as fh:
                tail = fh.read()[-300:].strip()
        except OSError:
            pass
        return f"claude завершился с ошибкой (rc={proc.returncode})" + (f": {tail}" if tail else "")
    return None


def _others_editing(job: dict) -> bool:
    return any(j["id"] != job["id"] and j["kind"] == "item" and j["status"] in workers.EDITING
               for j in workers.list_jobs(job["root"]))


def supervise(job: dict) -> None:
    root, item_id = job["root"], job.get("item_id")
    prompt, resume = job["prompt"], bool(job.get("resume"))
    while True:
        job = _update(job, status="RUNNING", pid=os.getpid())
        failure = run_claude(job, prompt, resume)
        tel = workers.telemetry(root, job["id"])
        if failure:
            _update(job, status="FAILED", reason=failure, ended=time.time())
            if item_id:
                core.set_status(root, item_id, "failed", f"воркер: {failure}", job=job["id"])
            return
        if job["kind"] != "item":
            _update(job, status="DONE", ended=time.time())
            if job["kind"] == "scout":
                workers.cleanup_session(job)
            return
        plan = core.load(root)
        item = core.find(plan, item_id)
        scope = workers.out_of_scope(item, tel["touched"])
        job = _update(job, out_of_scope=scope)
        if "STATUS: BLOCKED" in tel["text"].upper():
            _update(job, status="FAILED", reason="воркер сообщил BLOCKED", ended=time.time())
            core.set_status(root, item_id, "failed", "BLOCKED: " + tel["text"][:300], job=job["id"])
            return
        job = _update(job, status="VERIFYING")
        (item, ev), = core.verify_items(root, [item_id], by=job["id"])
        if not ev["passed"] and _others_editing(job):
            # Провал мог быть из-за незаконченных правок соседнего воркера: ждём их и перепроверяем.
            job = _update(job, status="VERIFY_WAIT")
            waited = time.time()
            while _others_editing(job) and time.time() - waited < VERIFY_WAIT_CAP:
                time.sleep(2)
            job = _update(job, status="VERIFYING")
            (item, ev), = core.verify_items(root, [item_id], by=job["id"])
        if ev["passed"]:
            _update(job, status="DONE", ended=time.time())
            workers.cleanup_session(job)
            return
        if job.get("fix_round", 0) >= job.get("max_fix", workers.MAX_FIX_ROUNDS):
            _update(job, status="FAILED", reason="проверки не прошли: " + core.fail_summary(ev)[:300], ended=time.time())
            return
        job = _update(job, fix_round=job.get("fix_round", 0) + 1)
        core.set_status(root, item_id, "in_progress", job=job["id"])
        prompt, resume = workers.fix_prompt(item, ev), True


def main(argv: list[str]) -> int:
    job_dir = argv[1]
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(job_dir))))
    job = workers.read_job(root, os.path.basename(os.path.abspath(job_dir)))
    if not job:
        return 2
    signal.signal(signal.SIGTERM, lambda *_: os._exit(143))
    try:
        supervise(job)
    except Exception as exc:  # noqa: BLE001 — надзиратель обязан оставить честный статус
        _update(job, status="FAILED", reason=f"надзиратель упал: {type(exc).__name__}: {exc}"[:300], ended=time.time())
        if job.get("item_id"):
            try:
                core.set_status(root, job["item_id"], "failed", "надзиратель упал", job=job["id"])
            except core.PlanError:
                pass
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
