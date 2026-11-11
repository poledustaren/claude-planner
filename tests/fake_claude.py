#!/usr/bin/env python3
"""Подставной claude для тестов: читает сценарий из <cwd>/.fake.json и выдаёт stream-json.

Сценарий: {"items": {"<id>": {"first": [ops], "resume": [ops]}}, "reviews": [structured, ...], "scout": "текст"}
ops: ["write", path, content] | ["sleep", sec] | ["fail"] | ["blocked"]
"""
import json
import os
import re
import sys
import time

args = sys.argv[1:]
opt = lambda name: args[args.index(name) + 1] if name in args else None  # noqa: E731
prompt = sys.stdin.read()
cwd = os.getcwd()
with open(os.path.join(cwd, ".fake.json"), encoding="utf-8") as fh:
    scenario = json.load(fh)
resume = opt("--resume")
sid = resume or opt("--session-id")
memo_path = os.path.join(cwd, ".fake_sessions.json")
memo = json.load(open(memo_path)) if os.path.exists(memo_path) else {}
with open(os.path.join(cwd, ".fake_calls.jsonl"), "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"sid": sid, "resume": bool(resume), "model": opt("--model"), "effort": opt("--effort"),
                         "tools": opt("--tools"), "prompt": prompt, "worker": os.environ.get("CPLAN_WORKER"),
                         "leak": os.environ.get("CLAUDECODE")}, ensure_ascii=False) + "\n")


def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


emit({"type": "system", "subtype": "init", "session_id": sid})
text, structured, ops = "STATUS: DONE\nготово", None, []
if opt("--json-schema"):
    n = memo.get("_reviews", 0)
    structured = scenario["reviews"][min(n, len(scenario["reviews"]) - 1)]
    memo["_reviews"] = n + 1
    text = json.dumps(structured)
elif "разведчик" in (opt("--system-prompt") or ""):
    text = scenario.get("scout", "ГДЕ: a.py:1")
else:
    m = re.search(r"ПУНКТ (\S+):", prompt)
    item = m.group(1) if m else memo.get(sid)
    memo[sid] = item
    ops = (scenario.get("items", {}).get(item) or {}).get("resume" if resume else "first") or []
json.dump(memo, open(memo_path, "w"))
n = 0
for op in ops:
    if op[0] == "sleep":
        time.sleep(op[1])
    elif op[0] == "fail":
        emit({"type": "result", "subtype": "error_during_execution", "is_error": True, "session_id": sid})
        sys.exit(1)
    elif op[0] == "blocked":
        text = "STATUS: BLOCKED\nнужен другой файл"
    elif op[0] == "write":
        n += 1
        path = os.path.join(cwd, op[1])
        emit({"type": "assistant", "message": {"id": f"m{n}", "usage": {"input_tokens": 10, "cache_read_input_tokens": 5000,
              "output_tokens": 20}, "content": [{"type": "tool_use", "id": f"t{n}", "name": "Write",
                                                 "input": {"file_path": path}}]}})
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(op[2])
emit({"type": "assistant", "message": {"id": "mf", "usage": {"input_tokens": 5, "cache_read_input_tokens": 5100,
      "output_tokens": 30}, "content": [{"type": "text", "text": text}]}})
emit({"type": "result", "subtype": "success", "is_error": False, "num_turns": n + 1, "total_cost_usd": 0.01,
      "session_id": sid, "result": text, **({"structured_output": structured} if structured is not None else {})})
