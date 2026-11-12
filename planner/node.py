#!/usr/bin/env python3
"""Команда на узле через devopsmcp в профиле «только чтение».

Нужна проверкам инфраструктурных планов: результат живёт на сервере, а не в папке плана.
Адрес — CPLAN_NODE_URL, токен — файл CPLAN_NODE_TOKEN_FILE (по умолчанию
~/.config/claude-planner/devopsmcp-ro.token, права 600). Только stdlib.

    python3 node.py <узел> '<команда>' [--timeout 60]
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

URL = os.environ.get("CPLAN_NODE_URL", "http://100.93.170.126:9096/mcp")
TOKEN_FILE = os.environ.get("CPLAN_NODE_TOKEN_FILE", "~/.config/claude-planner/devopsmcp-ro.token")


class NodeError(Exception):
    pass


def _token() -> str:
    path = os.path.expanduser(TOKEN_FILE)
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError as exc:
        raise NodeError(f"нет токена devopsmcp-ro ({path}): {exc}") from None


def _post(body: dict, session: str | None, timeout: int) -> tuple[dict | None, str | None]:
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
               "Authorization": f"Bearer {_token()}"}
    if session:
        headers["Mcp-Session-Id"] = session
    req = urllib.request.Request(URL, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(4_000_000).decode("utf-8", "replace")
            sid = resp.headers.get("Mcp-Session-Id") or session
    except urllib.error.HTTPError as exc:
        raise NodeError(f"devopsmcp-ro ответил HTTP {exc.code}") from None
    except OSError as exc:
        raise NodeError(f"devopsmcp-ro недоступен ({URL}): {exc}") from None
    if "id" not in body:
        return None, sid
    # ответ приходит либо JSON, либо потоком событий: берём сообщение с нашим id
    lines = [ln[5:].strip() for ln in raw.splitlines() if ln.startswith("data:")] or [raw]
    for chunk in lines:
        try:
            msg = json.loads(chunk)
        except ValueError:
            continue
        if isinstance(msg, dict) and msg.get("id") == body["id"]:
            return msg, sid
    raise NodeError("devopsmcp-ro: в ответе нет результата")


def run(server: str, command: str, timeout: int = 60) -> tuple[int | None, str]:
    """(код возврата, вывод). Код None — узел не дал выполнить команду (отказ, нет связи с узлом)."""
    hello = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                        "clientInfo": {"name": "claude-planner", "version": "1"}}}
    _, sid = _post(hello, None, 20)
    _post({"jsonrpc": "2.0", "method": "notifications/initialized"}, sid, 20)
    call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "exec_on", "arguments": {"server": server, "command": command, "timeout": timeout}}}
    msg, _ = _post(call, sid, timeout + 30)
    if msg.get("error"):
        raise NodeError(f"devopsmcp-ro: {msg['error'].get('message', msg['error'])}")
    text = "\n".join(p.get("text", "") for p in msg.get("result", {}).get("content", []) if p.get("type") == "text")
    data: object = text
    for _ in range(2):  # ответ инструмента — JSON строкой, иногда обёрнутый в {"result": "..."}
        try:
            data = json.loads(data) if isinstance(data, str) else data
        except ValueError:
            break
        if isinstance(data, dict) and isinstance(data.get("result"), str):
            data = data["result"]
    if not isinstance(data, dict):
        return None, text
    out = (data.get("stdout") or "") + (("\n" + data["stderr"]) if data.get("stderr") else "")
    if "rc" not in data:
        return None, out or json.dumps(data, ensure_ascii=False)[:2000]
    return data["rc"], out


def main(argv: list[str]) -> int:
    timeout = 60
    if "--timeout" in argv:
        i = argv.index("--timeout")
        timeout = int(argv[i + 1])
        del argv[i:i + 2]
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 64
    try:
        code, out = run(argv[0], " ".join(argv[1:]), timeout)
    except NodeError as exc:
        print(exc, file=sys.stderr)
        return 69
    sys.stdout.write(out if out.endswith("\n") or not out else out + "\n")
    if code is None:
        print("узел не выполнил команду", file=sys.stderr)
        return 70
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
