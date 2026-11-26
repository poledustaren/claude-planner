import json
import os
import subprocess
import time
import unittest
from unittest import mock

from helpers import ProjectCase, core, flow, item, plane, workers
from planner import graph


OK = "def a():\n    return 42\n"
CHECK = ["python3 -c 'import a,sys; sys.exit(0 if a.a()==42 else 1)'"]


def code(files=("a.py",), verify=None, **kw):
    return item(tier=kw.pop("tier", "code"), files=list(files), verify=verify or CHECK, **kw)


class WorkerFlagsAndScope(ProjectCase):
    def job(self, tier, kind="item"):
        return {"tier": tier, "kind": kind, "session_id": "sid", "system": "s", "schema": None}

    def test_lean_flags_budget_and_ephemeral_sessions(self):
        scout = workers.build_cmd(self.job("scout", kind="scout"), resume=False)
        self.assertIn("--disable-slash-commands", scout)
        self.assertIn("--no-chrome", scout)
        self.assertIn("--no-session-persistence", scout)
        self.assertNotIn("--session-id", scout)
        self.assertEqual(scout[scout.index("--max-budget-usd") + 1], str(workers.DEFAULT_TIERS["scout"]["budget"]))

        edit = workers.build_cmd(self.job("code-low", kind="item"), resume=False)
        self.assertIn("--disable-slash-commands", edit)
        self.assertIn("--no-chrome", edit)
        self.assertNotIn("--no-session-persistence", edit)
        self.assertIn("--session-id", edit)
        self.assertEqual(edit[edit.index("--max-budget-usd") + 1], str(workers.DEFAULT_TIERS["code-low"]["budget"]))

        # Безопасные git branch --show-current и docker run не режутся DENY_BASH
        disallowed = edit[edit.index("--disallowedTools") + 1:edit.index("--settings")]
        self.assertNotIn("Bash(git branch:*)", disallowed)
        self.assertIn("Bash(git branch -D:*)", disallowed)
        self.assertNotIn("Bash(docker:*)", disallowed)
        self.assertIn("Bash(docker push:*)", disallowed)

    def test_budget_override_from_config(self):
        path = os.path.join(os.environ["HOME"], ".config", "claude-planner", "config.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"tiers": {"code-low": {"budget": 0.99}}}, fh)
        self.assertEqual(workers.tiers()["code-low"]["budget"], 0.99)

    def test_auto_hydrates_small_item_files_when_context_has_no_slices(self):
        plan = core.init(self.dir, "План", "функция a возвращает 42", [code(files=["a.py"])])
        prompt = workers.item_prompt(plan, plan["items"][0])
        self.assertIn("СРЕЗЫ КОДА", prompt)
        self.assertIn("1\tdef a():", prompt)

    def test_out_of_scope_matches_dir_prefix_and_catches_bash_writes(self):
        os.makedirs(os.path.join(self.dir, "pkg"), exist_ok=True)
        it = {"files": ["pkg/", "a.py", ".gitignore"]}
        self.assertEqual(workers.out_of_scope(it, ["pkg/sub/x.py", "a.py", ".gitignore"], root=self.dir), [])
        self.assertEqual(workers.out_of_scope(it, ["pkg/x.py", "b.py", "gitignore"], root=self.dir), ["b.py", "gitignore"])

        # Запись через Bash в чужой файл b.py должна ловиться надзирателем через git status
        self.scenario({"items": {"1": {"first": [["write", "a.py", OK], ["bash_write", "b.py", "x = 99\n"]]}}})
        flow.init(self.dir, "План", "функция a возвращает 42", [code(files=["a.py"])])
        out = flow.run(self.dir)
        self.assertIn("Правил вне пункта: b.py", out)


class IncrementalTelemetry(ProjectCase):
    def test_telemetry_reads_incrementally_and_resets_on_truncate(self):
        jid = "i1-000000-test"
        sp = workers.stream_path(self.dir, jid)
        os.makedirs(os.path.dirname(sp), exist_ok=True)
        ev1 = {"type": "assistant", "message": {"id": "m1", "usage": {"input_tokens": 100, "output_tokens": 20},
               "content": [{"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "a.py"}}]}}
        ev2 = {"type": "assistant", "message": {"id": "m2", "usage": {"input_tokens": 200, "output_tokens": 30},
               "content": [{"type": "tool_use", "id": "t2", "name": "Edit", "input": {"file_path": os.path.join(self.dir, "a.py")}}]}}
        res = {"type": "result", "subtype": "success", "is_error": False, "num_turns": 2, "total_cost_usd": 0.12, "result": "готово"}

        with open(sp, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(ev1) + "\n")
        t1 = workers.telemetry(self.dir, jid, trail=5)
        self.assertEqual((t1["steps"], t1["out_tokens"], t1["ctx"]), (1, 20, 100))

        # Повторное чтение без изменений использует telemetry.json
        cache_path = workers._telemetry_cache_path(self.dir, jid)
        self.assertTrue(os.path.isfile(cache_path))
        t1_again = workers.telemetry(self.dir, jid, trail=5)
        self.assertEqual(t1_again, t1)

        # Дозапись хвоста дочитывает только новые строки
        with open(sp, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(ev2) + "\n" + json.dumps(res) + "\n")
        t2 = workers.telemetry(self.dir, jid, trail=5)
        self.assertEqual((t2["steps"], t2["out_tokens"], t2["ctx"], t2["touched"], t2["finished"], t2["cost"]),
                         (2, 50, 200, ["a.py"], True, 0.12))

        # Если файл перезаписан и стал короче — перепарсит с начала
        with open(sp, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(ev1) + "\n")
        t3 = workers.telemetry(self.dir, jid)
        self.assertEqual((t3["steps"], t3["finished"]), (1, False))


class WaveRollupAndEscalation(ProjectCase):
    def test_plan_run_rolls_through_dependent_worker_waves_in_one_call(self):
        self.scenario({"items": {
            "1": {"first": [["write", "a.py", OK]]},
            "2": {"first": [["write", "b.py", "def b():\n    return 7\n"]]},
            "3": {"first": [["write", "c.txt", "ok"]]},
        }})
        flow.init(self.dir, "Волны", "три пункта в двух волнах закрываются за один run",
                  [code(files=["a.py"]),
                   code(files=["b.py"], verify=["python3 -c 'import b,sys; sys.exit(b.b()!=7)'"]),
                   code(files=["new:c.txt"], verify=["test -f c.txt"], depends_on=["1", "2"])])
        out = flow.run(self.dir, timeout=60)
        self.assertEqual((self.status("1"), self.status("2"), self.status("3")), ("done", "done", "done"))
        self.assertIn("Пункт 3: DONE", out)
        self.assertIn("plan_finish", out)

    def test_default_tier_is_code_low_and_escalates_to_code(self):
        self.scenario({"items": {"1": {"first": [], "resume": []}}})
        plan = flow.init(self.dir, "Дефолт", "пункт без tier идёт в code-low и повышается до code",
                         [{"title": "Править a", "acceptance": "функция a возвращает 42",
                           "files": ["a.py"], "verify": CHECK}])
        self.assertEqual(self.plan()["items"][0]["tier"], "code-low")
        # Проверяем, что code-low при провале повышается до code
        out = flow.run(self.dir, timeout=60)
        self.assertIn("code-low не справился — повышен: 1: code (sonnet)", out)
        self.assertEqual(self.plan()["items"][0]["tier"], "code")
        self.assertEqual(self.plan()["items"][0]["escalated_from"], "code-low")

    def test_advice_flags_parallel_mains_and_many_code_high(self):
        plan = core.init(self.dir, "Советы", "проверяем подсказки по делегированию и дорогим тирам",
                         [item("Первый", files=["a.py"], verify=["python3 -c 'import a'"]),
                          item("Второй", files=["b.py"], verify=["python3 -c 'import b'"])])
        notes = core.advice(plan)
        self.assertTrue(any("Пункты main 1, 2" in n for n in notes))
        core.finish(self.dir, "план снят пользователем", user_approval="ок", force=True)
        plan2 = core.init(self.dir, "Дорогие", "два пункта на code-high должны дать подсказку",
                          [code(tier="code-high", files=["a.py"]),
                           code(tier="code-high", files=["b.py"], verify=["python3 -c 'import b'"])])
        notes2 = core.advice(plan2)
        self.assertTrue(any("code-high" in n for n in notes2))


class ParallelRecheckAndGraphAndDiff(ProjectCase):
    def test_recheck_runs_independent_items_in_parallel_and_deduplicates_shared(self):
        counter = os.path.join(self.home, "shared_runs")
        shared = f"printf x >> {counter} && test -f out.txt"
        core.init(self.dir, "Параллельный recheck", "проверки разных пунктов идут параллельно",
                  [item("Пункт 1", verify=["sleep 0.5 && test -f out.txt", shared]),
                   item("Пункт 2", verify=["sleep 0.5 && test -f a.py", shared]),
                   item("Пункт 3", verify=["sleep 0.5 && test -f b.py", shared])])
        self.write("out.txt", "x")
        core.verify_items(self.dir, ["1", "2", "3"])
        # Меняем файл, чтобы check_ok не сработал и все 3 пункта реально перепроверялись
        self.write("b.py", "def b():\n    return 3\n")
        with open(counter, "w", encoding="utf-8") as fh:
            fh.write("")
        t0 = time.monotonic()
        core.verify_items(self.dir, ["1", "2", "3"], by="finish", recheck=True)
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 1.3)  # последовательно было бы >= 1.5s
        with open(counter, encoding="utf-8") as fh:
            self.assertEqual(len(fh.read()), 1)  # общая проверка прогналась ровно 1 раз

    def test_verifying_sibling_does_not_block_check_ok_cache(self):
        core.init(self.dir, "Кэш волны", "сосед в VERIFYING уже не правит дерево",
                  [item(verify=["test -f out.txt"]), item(verify=["test -f a.py"])])
        self.write("out.txt", "x")
        workers.write_job({"id": "j2", "root": self.dir, "kind": "item", "tier": "code",
                           "item_id": "2", "status": "VERIFYING", "started": time.time()})
        core.set_status(self.dir, "2", "in_progress", job="j2")
        core.verify_items(self.dir, ["1"])
        self.assertTrue(bool(self.plan().get("check_ok")))

    def test_graph_links_does_not_block_on_sync_and_caches(self):
        os.makedirs(os.path.join(self.dir, ".codegraph"), exist_ok=True)
        graph._CACHE.clear()
        graph._LAST_SYNC.clear()
        run_calls, popen_calls = [], []

        def fake_run(cmd, **kw):
            run_calls.append(cmd)
            r = mock.Mock()
            r.stdout = "a.py — used by 1 files: b.py\n"
            return r

        def fake_popen(cmd, **kw):
            popen_calls.append(cmd)
            return mock.Mock()

        with mock.patch("shutil.which", return_value="/usr/bin/codegraph"), \
             mock.patch("subprocess.run", side_effect=fake_run), \
             mock.patch("subprocess.Popen", side_effect=fake_popen):
            res1 = graph.links(self.dir, ["a.py"])
            res2 = graph.links(self.dir, ["a.py"])
        self.assertEqual(res1, {"a.py": ["b.py"]})
        self.assertEqual(res2, {"a.py": ["b.py"]})
        self.assertFalse(any("sync" in c for c in run_calls))  # синхронного sync нет
        self.assertEqual(len(popen_calls), 1)                  # фоновый sync запущен 1 раз
        self.assertEqual(len(run_calls), 1)                    # второй вызов взят из кэша

    def test_git_summary_excludes_lockfiles_from_diff_body(self):
        self.write("package-lock.json", '{"old": 1}\n')
        subprocess.run("git add -A && git -c user.email=t@t -c user.name=t commit -qm lock",
                       shell=True, cwd=self.dir, check=True)
        base = core.git_head(self.dir)
        self.write("package-lock.json", '{"new": 2}\n' * 20)
        self.write("a.py", OK)
        summary = flow.git_summary(self.dir, base)
        self.assertIn("package-lock.json", summary.split("DIFF")[0])  # в --stat виден
        self.assertNotIn('"new": 2', summary)                         # в теле DIFF шума нет
        self.assertIn("return 42", summary)

    def test_plane_skips_unchanged_issue_patch_and_async_bg_flushes(self):
        calls = []

        def transport(method, path, payload=None):
            tail = path.split("/workspaces/ws/")[-1]
            calls.append((method, tail))
            if tail.startswith("projects/?"):
                return {"results": [{"id": "p1", "name": os.path.basename(self.dir), "identifier": "TST"}]}
            if tail.endswith("/states/"):
                return [{"id": "s1", "name": "In Progress", "group": "started"},
                        {"id": "s2", "name": "В проверке", "group": "started"}]
            if tail == "members/":
                return []
            if path.endswith("/users/me/"):
                return {"id": "u1"}
            if tail.endswith("/pages/"):
                return [] if method == "GET" else {"id": "pg1"}
            if "/pages/" in tail:
                return {"id": "pg1"}
            return {"id": "i1", "sequence_id": 1}

        plane.FACTORY = lambda: plane.Client("https://plane.test", "k", "ws", transport=transport)
        flow.init(self.dir, "План", "функция a возвращает 42", [item()])
        patches_before = sum(1 for m, t in calls if m == "PATCH" and "work-items" in t)
        plane.sync(self.dir)  # ничего не поменялось — PATCH не должен лететь
        patches_after = sum(1 for m, t in calls if m == "PATCH" and "work-items" in t)
        self.assertEqual(patches_before, patches_after)

        # Проверяем ASYNC_BG + flush_bg
        old_bg = flow.ASYNC_BG
        flow.ASYNC_BG = True
        try:
            flow.update(self.dir, title="Новое имя плана")
            flow.flush_bg(self.dir, timeout=5.0)
        finally:
            flow.ASYNC_BG = old_bg
        self.assertGreater(sum(1 for m, t in calls if m == "PATCH" and "work-items" in t), patches_after)


if __name__ == "__main__":
    unittest.main()
