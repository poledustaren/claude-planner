#!/usr/bin/env python3
"""cplan — взгляд человека на план и воркеров из терминала.

  cplan [status] [dir]   план и воркеры одним снимком
  cplan watch [dir]      то же, обновляется раз в 2 секунды
  cplan logs <id> [dir]  отчёт и шаги воркера (id задания или пункта)
  cplan json <dir>...    снимок планов в JSON (для панели прогресса)
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from planner import core, flow, workers  # noqa: E402


def snapshot(root: str) -> str:
    plan = core.load(root, required=False)
    jobs = [workers.view(j) for j in workers.list_jobs(root)
            if j["status"] in workers.ACTIVE or time.time() - j.get("ended", 0) < 1800]
    head = core.render(plan).rstrip() if plan else f"В {root} плана нет."
    return head + "\n\nВоркеры:\n" + workers.table(jobs) + ("\n\n" + flow.next_hint(root, plan) if plan else "")


def main(argv: list[str]) -> int:
    args = argv[1:]
    cmd = args.pop(0) if args and args[0] in ("status", "watch", "logs", "json") else "status"
    if cmd == "json":
        seen, plans = set(), []
        for path in args or [os.getcwd()]:
            snap = flow.snapshot(path)
            if snap and snap["root"] not in seen:
                seen.add(snap["root"])
                plans.append(snap)
        print(json.dumps({"plans": plans}, ensure_ascii=False))
        return 0
    try:
        if cmd == "logs":
            if not args:
                print(__doc__)
                return 2
            print(flow.worker_ctl(args[1] if len(args) > 1 else os.getcwd(), "logs", args[0], full=True))
            return 0
        root = core.project_root(args[0] if args else os.getcwd())
        if cmd == "status":
            print(snapshot(root))
            return 0
        while True:
            sys.stdout.write("\033[2J\033[H" + snapshot(root) + f"\n\n{time.strftime('%H:%M:%S')}  Ctrl-C — выход\n")
            sys.stdout.flush()
            time.sleep(2)
    except core.PlanError as exc:
        print(exc, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
