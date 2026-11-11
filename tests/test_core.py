import os
import unittest

from helpers import ProjectCase, core, item


class Validation(ProjectCase):
    def bad(self, items, needle):
        with self.assertRaises(core.PlanError) as ctx:
            core.init(self.dir, "План", "цель плана достаточно длинная", items)
        self.assertIn(needle, str(ctx.exception))

    def test_trivial_checks_rejected(self):
        for cmd in ("echo ok", "true", "exit 0", "ls", "pytest -q || true", "ls -la && echo done"):
            self.bad([item(verify=[cmd])], "ничего не доказывает")

    def test_real_check_accepted(self):
        plan = core.init(self.dir, "План", "цель плана достаточно длинная", [item(verify=["python3 -m pytest -q"])])
        self.assertEqual(plan["items"][0]["verify"][0]["type"], "cmd")

    def test_code_item_needs_executable_check(self):
        self.bad([item(files=["a.py"], verify=[{"type": "file_exists", "paths": ["a.py"]}])], "правит код")

    def test_missing_file_and_new_prefix(self):
        self.bad([item(files=["nope.py"])], "нет на диске")
        plan = core.init(self.dir, "План", "цель плана достаточно длинная", [item(files=["new:fresh.py"], verify=["test -f fresh.py"])])
        self.assertEqual(plan["items"][0]["files"], ["new:fresh.py"])

    def test_worker_needs_files_and_known_tier(self):
        self.bad([item(tier="code")], "нужен files")
        self.bad([item(tier="gpt")], "не существует")
        self.bad([item(tier="scout")], "plan_scout")

    def test_acceptance_and_verify_required(self):
        self.bad([{"title": "Сделать", "acceptance": "ок", "verify": ["test -f a.py"]}], "acceptance")
        self.bad([{"title": "Сделать", "acceptance": "наблюдаемый результат"}], "verify")

    def test_dag_cycle_and_unknown_dep(self):
        self.bad([item(id="1", depends_on=["2"]), item(id="2", depends_on=["1"])], "Цикл")
        self.bad([item(depends_on=["9"])], "таких пунктов нет")

    def test_waves(self):
        plan = core.init(self.dir, "План", "цель плана достаточно длинная",
                         [item(), item(), item(depends_on=["1", "2"]), item(depends_on=["3"])])
        self.assertEqual(core.compute_waves(plan["items"]), {"1": 1, "2": 1, "3": 2, "4": 3})

    def test_project_root_guards(self):
        for path in ("/", os.environ["HOME"], "/nonexistent-dir-xyz"):
            with self.assertRaises(core.PlanError):
                core.project_root(path)
        self.assertEqual(core.project_root(os.path.join(self.dir)), self.dir)


class Lifecycle(ProjectCase):
    def init(self, items, **kw):
        return core.init(self.dir, "План", "цель плана достаточно длинная", items, **kw)

    def test_verify_is_the_only_way_to_done(self):
        self.init([item()])
        (it, ev), = core.verify_items(self.dir, ["1"])
        self.assertFalse(ev["passed"])
        self.assertEqual(it["status"], "failed")
        self.assertIn("exit 1", core.fail_summary(ev))
        self.write("out.txt", "x")
        (it, ev), = core.verify_items(self.dir, ["1"])
        self.assertTrue(ev["passed"])
        self.assertEqual(self.status("1"), "done")
        self.assertTrue(os.path.isfile(os.path.join(self.dir, ev["log"])))
        self.assertEqual(self.plan()["status"], "ready")

    def test_second_plan_refused_until_finished_then_archived(self):
        self.init([item()])
        with self.assertRaises(core.PlanError):
            self.init([item()])
        self.write("out.txt", "x")
        core.verify_items(self.dir, ["1"])
        core.finish(self.dir, "сделано и проверено тестом")
        self.init([item()])
        self.assertEqual(len(os.listdir(core.pdir(self.dir, "archive"))), 1)

    def test_skip_needs_user_quote(self):
        self.init([item()])
        with self.assertRaises(core.PlanError):
            core.skip(self.dir, "1", "не нужно", "")
        core.skip(self.dir, "1", "не нужно делать", "да, пропусти")
        self.assertEqual(self.status("1"), "skipped")

    def test_finish_gates_open_items_and_review(self):
        self.init([item(), item(), item(), item()])
        with self.assertRaises(core.PlanError) as ctx:
            core.finish(self.dir, "итог работы по плану")
        self.assertIn("открыты пункты", str(ctx.exception))
        self.write("out.txt", "x")
        core.verify_items(self.dir, ["1", "2", "3", "4"])
        self.assertEqual(self.plan()["status"], "review_pending")
        with self.assertRaises(core.PlanError) as ctx:
            core.finish(self.dir, "итог работы по плану")
        self.assertIn("приёмка", str(ctx.exception))

    def test_small_plan_needs_no_review(self):
        self.init([item(), item()])
        self.assertFalse(core.needs_review(self.plan()))
        self.init_big = None

    def test_finish_rechecks_and_catches_regression(self):
        self.init([item(), item(verify=["test -f other.txt"])])
        self.write("out.txt", "x")
        self.write("other.txt", "x")
        core.verify_items(self.dir, ["1", "2"])
        os.remove(os.path.join(self.dir, "other.txt"))
        with self.assertRaises(core.PlanError) as ctx:
            core.finish(self.dir, "итог работы по плану")
        self.assertIn("регрессию", str(ctx.exception))
        self.assertEqual(self.status("2"), "failed")
        self.assertEqual(self.status("1"), "done")

    def test_start_gate_deps_and_file_lock(self):
        self.init([item(files=["a.py"], verify=["python3 -c 'import a'"]),
                   item(files=["a.py"], verify=["python3 -c 'import a'"]), item(depends_on=["1"])])
        plan = self.plan()
        self.assertIn("ждёт", core.start_gate(plan, core.find(plan, "3")))
        core.set_status(self.dir, "1", "in_progress")
        plan = self.plan()
        self.assertIn("сейчас правит пункт 1", core.start_gate(plan, core.find(plan, "2")))

    def test_update_add_reopen_and_hold(self):
        self.init([item()])
        self.write("out.txt", "x")
        core.verify_items(self.dir, ["1"])
        with self.assertRaises(core.PlanError):
            core.update(self.dir, changes=[{"id": "1", "title": "Другое"}])
        core.update(self.dir, changes=[{"id": "1", "reopen": True, "title": "Другое название"}], add=[item()], hold="ждём ответа")
        plan = self.plan()
        self.assertEqual((plan["items"][0]["status"], plan["items"][0]["title"], len(plan["items"])), ("todo", "Другое название", 2))
        self.assertEqual(plan["hold"], "ждём ответа")
        self.assertTrue(os.path.isfile(core.pdir(self.dir, ".gitignore")))


class ChangedCircumstances(ProjectCase):
    """Выходы из клинча: всё, что снимает гейт, требует цитаты согласия пользователя."""

    def init(self, items, **kw):
        return core.init(self.dir, "План", "цель плана достаточно длинная", items, **kw)

    def test_user_accepts_item_without_check_and_recheck_skips_it(self):
        self.init([item(), item(verify=["test -f never.txt"])])
        self.write("out.txt", "x")
        core.verify_items(self.dir, ["1"])
        with self.assertRaises(core.PlanError):
            core.skip(self.dir, "2", "проверка устарела", "", outcome="done")
        with self.assertRaises(core.PlanError):
            core.skip(self.dir, "2", "проверка устарела", "да", outcome="выполнен")
        core.skip(self.dir, "2", "проверка устарела", "да, считай сделанным", outcome="done")
        it = core.find(self.plan(), "2")
        self.assertEqual((it["status"], it["manual"]["user_approval"]), ("done", "да, считай сделанным"))
        self.assertEqual(core.recheckable(self.plan()), ["1"])
        self.assertIn("принят решением пользователя", core.render(self.plan()))
        core.finish(self.dir, "первый проверен, второй принят пользователем")  # never.txt нет — финиш не падает
        self.assertEqual(self.plan()["status"], "finished")

    def test_skip_many_is_atomic(self):
        self.init([item(), item(), item()])
        with self.assertRaises(core.PlanError):
            core.skip(self.dir, ["1", "9"], "больше не нужно", "да")
        self.assertEqual(self.status("1"), "todo")
        core.skip(self.dir, ["1", "2"], "больше не нужно", "да, убирай")
        self.assertEqual([self.status(i) for i in "123"], ["skipped", "skipped", "todo"])

    def test_reopen_drops_manual_acceptance(self):
        self.init([item()])
        core.skip(self.dir, "1", "сделано иначе", "принимаю", outcome="done")
        core.update(self.dir, changes=[{"id": "1", "reopen": True}])
        it = core.find(self.plan(), "1")
        self.assertEqual((it["status"], "manual" in it), ("todo", False))

    def test_remove_needs_quote_and_unlinks_deps(self):
        self.init([item(), item(depends_on=["1"]), item()])
        with self.assertRaises(core.PlanError) as ctx:
            core.update(self.dir, remove=["1"])
        self.assertIn("согласия пользователя", str(ctx.exception))
        core.update(self.dir, remove=["1"], user_approval="да, первый убери")
        plan = self.plan()
        self.assertEqual([i["id"] for i in plan["items"]], ["2", "3"])
        self.assertEqual(plan["items"][0]["depends_on"], [])
        self.assertEqual([i["id"] for i in core.ready_items(plan)], ["2", "3"])
        self.assertEqual([i["id"] for i in core.update(self.dir, add=[item()])["items"]], ["2", "3", "4"])

    def test_retitle_and_regoal(self):
        self.init([item()])
        with self.assertRaises(core.PlanError):
            core.update(self.dir, goal="коротко")
        core.update(self.dir, title="Новое название", goal="новая цель после смены обстоятельств")
        plan = self.plan()
        self.assertEqual((plan["title"], plan["goal"]), ("Новое название", "новая цель после смены обстоятельств"))

    def test_user_closes_without_review(self):
        self.init([item(), item(), item(), item()])
        self.write("out.txt", "x")
        core.verify_items(self.dir, ["1", "2", "3", "4"])
        with self.assertRaises(core.PlanError) as ctx:
            core.finish(self.dir, "итог работы по плану")
        self.assertIn("решением пользователя", str(ctx.exception))
        plan = core.finish(self.dir, "итог работы по плану", user_approval="приёмку не надо, закрывай")
        self.assertEqual(plan["status"], "finished")
        self.assertIn("приёмка", plan["user_override"]["gates"][0])

    def test_quote_does_not_hide_open_items_or_regression(self):
        self.init([item(), item(verify=["test -f other.txt"])])
        self.write("out.txt", "x")
        core.verify_items(self.dir, ["1"])
        with self.assertRaises(core.PlanError) as ctx:
            core.finish(self.dir, "итог работы по плану", user_approval="закрывай")
        self.assertIn("открыты пункты", str(ctx.exception))
        self.write("other.txt", "x")
        core.verify_items(self.dir, ["2"])
        os.remove(os.path.join(self.dir, "other.txt"))
        with self.assertRaises(core.PlanError) as ctx:
            core.finish(self.dir, "итог работы по плану", user_approval="закрывай")
        self.assertIn("регрессию", str(ctx.exception))
        self.assertEqual(self.status("2"), "failed")

    def test_force_closes_as_is_and_frees_project(self):
        self.init([item(), item(verify=["test -f other.txt"]), item(), item()])
        self.write("out.txt", "x")
        core.verify_items(self.dir, ["1", "2"])
        with self.assertRaises(core.PlanError):
            core.finish(self.dir, "задача отменена заказчиком", force=True)
        plan = core.finish(self.dir, "задача отменена заказчиком", user_approval="бросай, неактуально", force=True)
        self.assertEqual([i["status"] for i in plan["items"]], ["done", "skipped", "skipped", "skipped"])
        self.assertEqual((plan["status"], plan["closed_as_is"]), ("finished", "бросай, неактуально"))
        self.assertIn("закрыт как есть", core.render(plan))
        self.init([item()])  # новый план заводится сразу
        self.assertEqual(len(os.listdir(core.pdir(self.dir, "archive"))), 1)

    def test_advice_on_lost_parallelism(self):
        chain = [item(tier="code", files=["a.py"], verify=["python3 a.py"]),
                 item(tier="code", files=["b.py"], verify=["python3 b.py"], depends_on=["1"]),
                 item(tier="code", files=["new:c.py"], verify=["python3 c.py"], depends_on=["2"])]
        self.assertTrue(any("по очереди" in n for n in core.advice(self.init(chain))))
        core.finish(self.dir, "закрываем цепочку", user_approval="да", force=True)
        shared = [item(tier="code", files=["a.py"], verify=["python3 a.py"]),
                  item(tier="code", files=["a.py"], verify=["python3 a.py"]),
                  item(tier="code", files=["b.py"], verify=["python3 b.py"])]
        notes = core.advice(self.init(shared))
        self.assertTrue(any("a.py" in n and "1, 2" in n for n in notes))
        self.assertFalse(any("строго по очереди" in n for n in notes))


if __name__ == "__main__":
    unittest.main()
