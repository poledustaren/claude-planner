import json
import os
import subprocess
import sys
import unittest

from helpers import ROOT, ProjectCase, core, flow, item, workers

HOOKS = os.path.join(ROOT, "planner", "hooks.py")
SERVER = os.path.join(ROOT, "planner", "server.py")


def hook(mode, data, env=None):
    proc = subprocess.run([sys.executable, HOOKS, mode], input=json.dumps(data), capture_output=True, text=True,
                          env={**os.environ, **(env or {})}, timeout=20)
    return json.loads(proc.stdout) if proc.stdout.strip() else {}


class Hooks(ProjectCase):
    def test_read_cap(self):
        self.write("big.py", "x = 1\n" * 800)
        self.write("big.md", "x\n" * 800)
        big = os.path.join(self.dir, "big.py")
        out = hook("read_cap", {"tool_input": {"file_path": big}})
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("800", out["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertEqual(hook("read_cap", {"tool_input": {"file_path": big, "offset": 100, "limit": 50}}), {})
        self.assertEqual(hook("read_cap", {"tool_input": {"file_path": os.path.join(self.dir, "big.md")}}), {})
        self.assertEqual(hook("read_cap", {"tool_input": {"file_path": os.path.join(self.dir, "a.py")}}), {})
        self.assertEqual(hook("read_cap", {"tool_input": {"file_path": big}}, {"CPLAN_READ_CAP": "0"}), {})

    def test_stop_gate_follows_plan_driven_in_this_turn(self):
        core.init(self.dir, "План", "цель плана достаточно длинная", [item()])
        base = {"session_id": "s1"}
        self.assertEqual(hook("stop", base), {})  # сессия план не вела
        hook("track", {**base, "tool_name": "mcp__planner__plan_init", "tool_input": {"project_dir": self.dir}})
        out = hook("stop", base)
        self.assertEqual(out["decision"], "block")
        self.assertIn("открыты пункты 1", out["reason"])
        self.assertEqual(hook("stop", base)["decision"], "block")
        self.assertEqual(hook("stop", base), {})  # анти-петля: состояние не меняется
        hook("prompt", base)
        self.assertEqual(hook("stop", base), {})  # новый ход пользователя без работы по плану
        hook("track", {**base, "tool_name": "mcp__planner__plan_show", "tool_input": {"project_dir": self.dir}})
        core.update(self.dir, hold="нужен ответ пользователя")
        self.assertEqual(hook("stop", base), {})

    def test_stop_gate_ready_plan_asks_to_finish(self):
        core.init(self.dir, "План", "цель плана достаточно длинная", [item()])
        self.write("out.txt", "x")
        core.verify_items(self.dir, ["1"])
        hook("track", {"session_id": "s2", "tool_name": "mcp__planner__plan_verify", "tool_input": {"project_dir": self.dir}})
        self.assertIn("plan_finish", hook("stop", {"session_id": "s2"})["reason"])

    def test_subagent_gets_active_plan_and_rules(self):
        sub = os.path.join(self.dir, "pkg")
        os.makedirs(sub)
        start = {"session_id": "s5", "cwd": sub, "agent_id": "a1", "agent_type": "worker-medium"}
        self.assertEqual(hook("subagent", start), {})  # плана нет — субагенту нечего сообщать
        core.init(self.dir, "План", "цель плана достаточно длинная",
                  [item("Править a", tier="code", files=["a.py"], verify=["python3 a.py"]), item("Править b")])
        core.set_status(self.dir, "1", "in_progress", job="i1-000000-abcd")
        out = hook("subagent", start)["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "SubagentStart")
        text = out["additionalContext"]
        self.assertIn("План «План»", text)
        self.assertIn("1 [in_progress, у воркера — файлы не трогать] Править a | файлы: a.py", text)
        self.assertIn("План не меняй и пункты не закрывай", text)
        hook("track", {"session_id": "s6", "tool_name": "mcp__planner__plan_show", "tool_input": {"project_dir": self.dir}})
        self.assertIn("План «План»", hook("subagent", {"session_id": "s6", "cwd": self.home, "agent_id": "a2"})
                      ["hookSpecificOutput"]["additionalContext"])  # план сессии виден и из чужой папки
        core.finish(self.dir, "план снят пользователем", user_approval="бросай", force=True)
        self.assertEqual(hook("subagent", start), {})

    def test_subagent_cannot_change_plan(self):
        call = lambda tool, **kw: hook("agent_gate", {"tool_name": f"mcp__planner__{tool}", "tool_input": {}, **kw})  # noqa: E731
        for tool in ("plan_skip", "plan_finish", "plan_update", "plan_init", "plan_verify"):
            out = call(tool, agent_id="a1")["hookSpecificOutput"]
            self.assertEqual(out["permissionDecision"], "deny")
            self.assertIn("основная сессия", out["permissionDecisionReason"])
            self.assertEqual(call(tool), {})  # основной сессии можно
        for tool in ("plan_show", "plan_jobs", "plan_scout", "wiki_pages"):
            self.assertEqual(call(tool, agent_id="a1"), {})
        self.assertEqual(hook("agent_gate", {"tool_name": "mcp__planner__plan_finish", "agent_id": "a1"},
                              {"CPLAN_AGENT_GATE": "0"}), {})

    def test_edit_in_project_needs_open_plan(self):
        edit = lambda path, **kw: hook("plan_gate", {"session_id": "s7", "tool_name": "Edit",  # noqa: E731
                                                     "tool_input": {"file_path": path}, **kw})
        target = os.path.join(self.dir, "pkg", "new.py")
        out = edit(target)["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertIn(f'plan_init(project_dir="{self.dir}"', out["permissionDecisionReason"])
        self.assertIn("один пункт", out["permissionDecisionReason"])  # лёгкий план — тоже план
        sub = edit(target, agent_id="a1")["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("основная сессия", sub)
        self.assertNotIn("plan_init", sub)
        self.assertEqual(edit("a.py", cwd=self.dir)["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(hook("plan_gate", {"tool_name": "Edit", "tool_input": {"file_path": target}},
                              {"CPLAN_PLAN_GATE": "0"}), {})
        core.init(self.dir, "План", "цель плана достаточно длинная", [item()])
        self.assertEqual(edit(target), {})  # открытый план накрывает весь проект, а не только файлы пунктов
        self.assertEqual(edit(target, agent_id="a1"), {})
        self.write("out.txt", "x")
        core.verify_items(self.dir, ["1"])
        core.finish(self.dir, "готово, проверки прошли")
        self.assertEqual(edit(target)["hookSpecificOutput"]["permissionDecision"], "deny")  # план закрыт — нужен новый

    def test_plan_gate_leaves_non_project_files_alone(self):
        edit = lambda path, env=None: hook("plan_gate", {"tool_name": "Write", "tool_input": {"file_path": path}}, env)  # noqa: E731
        self.assertEqual(edit(os.path.join(self.home, "notes.txt")), {})  # файл вне проектов
        self.assertEqual(edit(os.path.join(self.home, ".claude", "projects", "x", "memory", "m.md")), {})
        self.assertEqual(edit(os.path.join(self.dir, ".cplan", "notes.md")), {})
        self.assertEqual(edit(os.path.join(self.dir, ".claude", "settings.json")), {})
        self.assertEqual(edit(os.path.join(ROOT, "planner", "core.py")), {})  # сам планер правят напрямую
        base = os.path.join(self.home, "projects")
        fresh = edit(os.path.join(base, "fresh", "src", "main.py"))["hookSpecificOutput"]  # новый проект без git
        self.assertIn(f'project_dir="{os.path.realpath(base)}/fresh"', fresh["permissionDecisionReason"])
        self.assertEqual(edit(os.path.join(base, "scratch.py")), {})  # не внутри проекта

    def test_nudge_once_after_several_files_without_plan(self):
        outs = [hook("track", {"session_id": "s3", "tool_name": "Edit",
                               "tool_input": {"file_path": os.path.join(self.dir, f"f{n}.py")}}) for n in range(6)]
        self.assertEqual([bool(o) for o in outs], [False, False, False, True, False, False])
        self.assertIn("plan_init", outs[3]["hookSpecificOutput"]["additionalContext"])

    def test_broken_input_fails_open(self):
        proc = subprocess.run([sys.executable, HOOKS, "stop"], input="{не json", capture_output=True, text=True)
        self.assertEqual((proc.returncode, proc.stdout), (0, ""))


class Server(ProjectCase):
    def rpc(self, *msgs):
        payload = "\n".join(json.dumps(m) for m in msgs) + "\n"
        proc = subprocess.run([sys.executable, SERVER], input=payload, capture_output=True, text=True, timeout=60)
        return {m.get("id"): m for m in map(json.loads, proc.stdout.splitlines())}

    def test_protocol_and_tools(self):
        args = {"project_dir": self.dir, "title": "План", "goal": "цель плана достаточно длинная", "items": [item()]}
        out = self.rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                       {"jsonrpc": "2.0", "method": "notifications/initialized"},
                       {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                       {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "plan_init", "arguments": args}})
        self.assertEqual(out[1]["result"]["protocolVersion"], "2025-06-18")
        names = [t["name"] for t in out[2]["result"]["tools"]]
        self.assertEqual(len(names), 13)
        self.assertLess(len(json.dumps(out[2]["result"], ensure_ascii=False)), 11000)  # схемы не раздуваем
        self.assertFalse(out[3]["result"]["isError"])
        self.assertIn("План создан", out[3]["result"]["content"][0]["text"])
        out = self.rpc({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "plan_show", "arguments": {"project_dir": self.dir}}},
                       {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "plan_skip", "arguments": {"project_dir": self.dir, "item_id": "1", "reason": "не нужно", "user_approval": ""}}})
        self.assertIn("[ ] 1", out[4]["result"]["content"][0]["text"])
        self.assertTrue(out[5]["result"]["isError"])
        self.assertIn("цитата", out[5]["result"]["content"][0]["text"])


class WorkerInternals(ProjectCase):
    def job(self, tier):
        return {"tier": tier, "session_id": "sid", "system": "s", "schema": None}

    def test_read_tier_cannot_edit_and_edit_tier_cannot_push(self):
        read = workers.build_cmd(self.job("scout"), resume=False)
        self.assertEqual(read[read.index("--tools") + 1], "Read,Grep,Glob,Bash")
        self.assertIn("Bash(rg:*)", read)
        self.assertNotIn("Bash", read[read.index("--allowedTools"):read.index("--settings")])
        self.assertNotIn("--effort", read)
        edit = workers.build_cmd(self.job("code-high"), resume=True)
        self.assertEqual((edit[edit.index("--model") + 1], edit[edit.index("--effort") + 1]), ("sonnet", "high"))
        self.assertIn("--resume", edit)
        self.assertIn("Bash(git push:*)", edit[edit.index("--disallowedTools"):])
        self.assertEqual(edit[edit.index("--setting-sources") + 1], "")

    def test_tier_override_from_config(self):
        path = os.path.join(os.environ["HOME"], ".config", "claude-planner", "config.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            json.dump({"tiers": {"code": {"model": "claude-sonnet-5", "effort": "low"}}}, fh)
        self.assertEqual(workers.tiers()["code"]["model"], "claude-sonnet-5")
        self.assertEqual(workers.tiers()["scout"]["model"], "haiku")

    def test_hydrate_cuts_exact_slices(self):
        self.write("long.py", "".join(f"line{n}\n" for n in range(1, 400)))
        out = workers.hydrate(self.dir, "правь long.py:L100-L102 и смотри нет/файла.py:5")
        self.assertEqual(out, "--- long.py:100-102\n100\tline100\n101\tline101\n102\tline102")


if __name__ == "__main__":
    unittest.main()
