import json
import os
import time
import unittest

from helpers import ProjectCase, core, flow, item, outline, plane, workers

OK = "def a():\n    return 42\n"
CHECK = ["python3 -c 'import a,sys; sys.exit(0 if a.a()==42 else 1)'"]


def code(files=("a.py",), verify=None, **kw):
    return item(tier="code", files=list(files), verify=verify or CHECK, **kw)


class Workers(ProjectCase):
    def init(self, items, **kw):
        return flow.init(self.dir, "План", "функция a возвращает 42", items, **kw)

    def test_worker_done_then_finish(self):
        self.scenario({"items": {"1": {"first": [["write", "a.py", OK]]}}})
        self.init([code(context="смотри a.py:L1-L2")])
        out = flow.run(self.dir)
        self.assertIn("Пункт 1: DONE", out)
        self.assertIn("plan_finish", out)
        call = self.calls()[0]
        self.assertEqual((call["model"], call["effort"], call["worker"], call["leak"]), ("sonnet", "medium", "1", None))
        self.assertIn("1\tdef a():", call["prompt"])  # срез кода вшит в промпт
        self.assertIn("закрыт", flow.finish(self.dir, "a() возвращает 42, проверено запуском"))

    def test_failed_check_goes_back_to_same_session(self):
        self.scenario({"items": {"1": {"first": [["write", "a.py", "def a():\n    return 0\n"]],
                                       "resume": [["write", "a.py", OK]]}}})
        self.init([code()])
        self.assertIn("Пункт 1: DONE", flow.run(self.dir))
        calls = self.calls()
        self.assertEqual([c["resume"] for c in calls], [False, True])
        self.assertEqual(calls[0]["sid"], calls[1]["sid"])
        self.assertIn("проверки не прошли", calls[1]["prompt"])
        self.assertEqual(workers.list_jobs(self.dir)[0]["fix_round"], 1)

    def test_fix_limit_then_manual_rerun_resumes(self):
        bad = [["write", "a.py", "def a():\n    return 0\n"]]
        self.scenario({"items": {"1": {"first": bad, "resume": bad}}})
        self.init([code()])
        out = flow.run(self.dir)
        self.assertIn("FAILED", out)
        self.assertEqual(self.status("1"), "failed")
        self.scenario({"items": {"1": {"resume": [["write", "a.py", OK]]}}})
        self.assertIn("доработка в той же сессии", flow.run(self.dir, item_ids=["1"]))
        self.assertEqual(self.status("1"), "done")
        self.assertEqual(len({c["sid"] for c in self.calls()}), 1)

    def test_blocked_and_crash_are_honest_failures(self):
        self.scenario({"items": {"1": {"first": [["blocked"]]}, "2": {"first": [["fail"]]}}})
        self.init([code(), code(files=["b.py"], verify=["python3 -c 'import b'"])])
        flow.run(self.dir, wait=False)
        self.drain()
        jobs = {j["item_id"]: j for j in workers.list_jobs(self.dir)}
        self.assertIn("BLOCKED", jobs["1"]["reason"])
        self.assertIn("ошибкой", jobs["2"]["reason"])
        self.assertEqual((self.status("1"), self.status("2")), ("failed", "failed"))
        self.assertEqual(len(self.calls()), 2)  # BLOCKED не проверяем и не дорабатываем

    def test_wave_runs_parallel_then_dependent(self):
        self.scenario({"items": {"1": {"first": [["sleep", 1], ["write", "a.py", OK]]},
                                 "2": {"first": [["sleep", 1], ["write", "b.py", "def b():\n    return 7\n"]]},
                                 "3": {"first": [["write", "c.txt", "ok"]]}}})
        self.init([code(), code(files=["b.py"], verify=["python3 -c 'import b,sys; sys.exit(b.b()!=7)'"]),
                   code(files=["new:c.txt"], verify=["test -f c.txt"], depends_on=["1", "2"])])
        t0 = time.time()
        out = flow.run(self.dir, wait=False)
        self.assertIn("1: code", out)
        self.assertIn("2: code", out)
        self.assertNotIn("3: code", out)
        self.drain()
        self.assertLess(time.time() - t0, 6)
        self.assertEqual((self.status("1"), self.status("2"), self.status("3")), ("done", "done", "todo"))
        self.assertIn("Пункт 3: DONE", flow.run(self.dir))

    def test_file_lock_between_running_items(self):
        self.scenario({"items": {"1": {"first": [["sleep", 2], ["write", "a.py", OK]]}}})
        self.init([code(), code()])
        out = flow.run(self.dir, wait=False)
        self.assertIn("сейчас правит пункт 1", out)
        self.assertEqual(len(workers.active_jobs(self.dir)), 1)

    def test_kill_and_steer_resumes_session(self):
        self.scenario({"items": {"1": {"first": [["write", "a.py", "x = 1\n"], ["sleep", 30]],
                                       "resume": [["write", "a.py", OK]]}}})
        self.init([code()])
        flow.run(self.dir, wait=False)
        deadline = time.time() + 10
        while time.time() < deadline and not workers.telemetry(self.dir, workers.list_jobs(self.dir)[0]["id"])["ctx"]:
            time.sleep(0.1)
        out = flow.worker_ctl(self.dir, "steer", "1", hint="функция должна вернуть 42")
        self.assertIn("в той же сессии", out)
        self.drain()
        jobs = workers.list_jobs(self.dir)
        self.assertEqual([j["status"] for j in jobs], ["KILLED", "DONE"])
        self.assertEqual(jobs[0]["session_id"], jobs[1]["session_id"])
        self.assertIn("должна вернуть 42", self.calls()[-1]["prompt"])
        self.assertEqual(self.status("1"), "done")

    def test_out_of_scope_edit_is_reported(self):
        self.scenario({"items": {"1": {"first": [["write", "a.py", OK], ["write", "b.py", "x = 1\n"]]}}})
        self.init([code()])
        self.assertIn("Правил вне пункта: b.py", flow.run(self.dir))

    def test_main_item_and_manual_verify(self):
        self.init([item()])
        self.assertIn("делаешь сам", flow.run(self.dir))
        self.assertIn("exit 1", flow.verify(self.dir))
        self.write("out.txt", "x")
        self.assertIn("PASS", flow.verify(self.dir))

    def test_scout_uses_haiku_read_only_without_plan(self):
        self.scenario({"scout": "ГДЕ: a.py:1 функция a"})
        out = flow.scout(self.dir, ["Где определена функция a и кто её вызывает?"])
        self.assertIn("a.py:1", out)
        call = self.calls()[0]
        self.assertEqual((call["model"], call["effort"], call["tools"]), ("haiku", None, "Read,Grep,Glob,Bash"))
        with self.assertRaises(core.PlanError):
            flow.scout(self.dir, ["коротко"])

    def test_orchestrator_works_on_main_while_workers_run(self):
        self.scenario({"items": {"1": {"first": [["sleep", 4], ["write", "a.py", OK]]}}})
        self.init([code(), item()])
        t0 = time.time()
        out = flow.run(self.dir)  # не ждём воркера: у оркестратора есть свой пункт
        self.assertLess(time.time() - t0, 3)
        self.assertIn("пока работают воркеры (1), делай свои пункты main 2", out)
        self.assertEqual(len(workers.active_jobs(self.dir)), 1)
        self.write("out.txt", "x")
        self.assertIn("Пункт 2: PASS", flow.verify(self.dir))
        self.assertIn("Пункт 1: DONE", flow.run(self.dir))  # своих пунктов нет — ждём воркера

    def test_skip_stops_worker_of_the_item(self):
        self.scenario({"items": {"1": {"first": [["sleep", 30]]}, "2": {"first": [["sleep", 30]]}}})
        self.init([code(), code(files=["b.py"], verify=["python3 b.py"])])
        flow.run(self.dir, wait=False)
        with self.assertRaises(core.PlanError):
            flow.skip(self.dir, "1", "задача снята", "")
        self.assertEqual(len(workers.active_jobs(self.dir)), 2)  # без согласия никого не останавливаем
        out = flow.skip(self.dir, "1", "задача снята", "да, первый не делаем")
        self.assertIn("пропущены", out)
        self.assertEqual(self.status("1"), "skipped")
        self.assertEqual([str(j["item_id"]) for j in workers.active_jobs(self.dir)], ["2"])

    def test_force_finish_stops_workers_and_closes_as_is(self):
        self.scenario({"items": {"1": {"first": [["sleep", 30]]}}})
        self.init([code(), item()])
        flow.run(self.dir, wait=False)
        with self.assertRaises(core.PlanError):
            flow.finish(self.dir, "обстоятельства изменились, план снят", force=True)
        self.assertEqual(len(workers.active_jobs(self.dir)), 1)
        out = flow.finish(self.dir, "обстоятельства изменились, план снят", user_approval="бросай", force=True)
        self.assertIn("как есть", out)
        self.assertEqual(workers.active_jobs(self.dir), [])
        self.assertEqual([i["status"] for i in self.plan()["items"]], ["skipped", "skipped"])
        self.assertIn("План создан", self.init([item()]))

    def test_parallel_limit(self):
        self.scenario({"items": {str(n): {"first": [["sleep", 5]]} for n in range(1, 6)}})
        for n in range(1, 6):
            self.write(f"f{n}.py", "x = 1\n")
        self.init([code(files=[f"f{n}.py"], verify=[f"python3 f{n}.py"]) for n in range(1, 6)], review=False)
        out = flow.run(self.dir, wait=False)
        self.assertIn("предел 4", out)
        self.assertEqual(len(workers.active_jobs(self.dir)), 4)


class Review(ProjectCase):
    def closed_plan(self, reviews):
        self.scenario({"reviews": reviews})
        flow.init(self.dir, "План", "функция a возвращает 42", [item(), item(), item(), item()])
        self.write("out.txt", "x")
        flow.verify(self.dir, ["1", "2", "3", "4"])

    def test_approved_opens_finish(self):
        self.closed_plan([{"verdict": "approved", "issues": [{"severity": "minor", "problem": "можно короче"}]}])
        out = flow.review(self.dir)
        self.assertIn("APPROVED", out)
        self.assertIn("plan_finish", out)
        self.assertEqual(self.calls()[0]["model"], "opus")
        self.assertIn("НОВЫЕ ФАЙЛЫ", self.calls()[0]["prompt"])
        flow.finish(self.dir, "всё сделано и принято приёмщиком")
        self.assertEqual(self.plan()["status"], "finished")

    def test_blocker_rejects_even_if_model_says_approved(self):
        self.closed_plan([{"verdict": "approved", "issues": [{"severity": "blocker", "file": "a.py", "line": 2,
                                                              "problem": "возвращает не то"}]}])
        out = flow.review(self.dir)
        self.assertIn("REJECTED", out)
        self.assertIn("a.py:2", out)
        with self.assertRaises(core.PlanError):  # без изменений и без возражений повтор запрещён
            flow.review(self.dir)

    def test_rework_then_rounds_cap_needs_user(self):
        reject = {"verdict": "rejected", "issues": [{"severity": "blocker", "problem": "не то"}]}
        self.closed_plan([reject])
        for n in range(core.MAX_REVIEW_ROUNDS):
            self.assertIn("REJECTED", flow.review(self.dir, notes="возражение" if n else None))
        self.assertTrue(core.review_stuck(self.plan()))
        with self.assertRaises(core.PlanError):
            flow.review(self.dir, notes="ещё раз")
        with self.assertRaises(core.PlanError):
            flow.finish(self.dir, "итог работы по плану")
        flow.finish(self.dir, "итог работы по плану", user_approval="закрывай так")
        self.assertEqual(self.plan()["status"], "finished")

    def test_review_is_told_about_user_closed_items(self):
        self.scenario({"reviews": [{"verdict": "approved", "issues": []}]})
        flow.init(self.dir, "План", "функция a возвращает 42",
                  [item(), item(), item(), item(verify=["test -f never.txt"])])
        self.write("out.txt", "x")
        flow.verify(self.dir, ["1", "2", "3"])
        flow.skip(self.dir, None, "проверка потеряла смысл", "принимаю как есть", item_ids=["4"], outcome="done")
        self.assertIn("APPROVED", flow.review(self.dir))  # never.txt нет, но пункт 4 не перепроверяется
        self.assertIn("закрыт решением пользователя без проверки (проверка потеряла смысл)", self.calls()[0]["prompt"])

    def test_review_recheck_catches_regression(self):
        self.closed_plan([{"verdict": "approved", "issues": []}])
        os.remove(os.path.join(self.dir, "out.txt"))
        with self.assertRaises(core.PlanError) as ctx:
            flow.review(self.dir)
        self.assertIn("регрессия", str(ctx.exception))
        self.assertEqual(self.calls(), [])


class FakePlane:
    def __init__(self, project_name):
        self.log, self.pages, self.issues, self.project_name = [], {}, {}, project_name

    def __call__(self, method, path, payload=None):
        tail = path.split("/workspaces/ws/")[-1]
        self.log.append((method, tail))
        if tail == "projects/" and method == "GET":
            return {"results": [{"id": "p1", "name": self.project_name, "identifier": "TST"}]}
        if tail.endswith("/states/") and method == "GET":
            return [{"id": "s1", "name": "In Progress", "group": "started"}, {"id": "s2", "name": "В проверке", "group": "started"}]
        if path.endswith("/users/me/"):
            return {"id": "u1"}
        if tail.endswith("/pages/"):
            if method == "GET":
                return [{"id": k, "name": v["name"]} for k, v in self.pages.items()]
            self.pages["pg1"] = payload
            return {"id": "pg1"}
        if "/pages/" in tail:
            self.pages[tail.split("/pages/")[1].strip("/")] = payload
            return {"id": tail.split("/pages/")[1].strip("/")}
        if tail.endswith("/work-items/") and method == "POST":
            self.issues["i1"] = payload
            return {"id": "i1", "sequence_id": 7}
        self.issues["i1"] = payload
        return {"id": "i1", "sequence_id": 7}


class FakeOutline:
    def __init__(self):
        self.docs, self.n = {}, 0

    def __call__(self, method, payload):
        if method == "collections.list":
            return {"data": [{"id": "c1", "name": "hermes"}] if payload["offset"] == 0 else []}
        if method == "collections.documents":
            def node(doc_id):
                d = self.docs[doc_id]
                return {"id": doc_id, "title": d["title"], "url": f"/doc/{doc_id}",
                        "children": [node(k) for k, v in self.docs.items() if v["parent"] == doc_id]}
            return {"data": [node(k) for k, v in self.docs.items() if v["parent"] is None]}
        if method == "documents.create":
            self.n += 1
            doc_id = f"d{self.n}"
            self.docs[doc_id] = {"title": payload["title"], "text": payload["text"], "parent": payload.get("parentDocumentId")}
            return {"data": {"id": doc_id, "title": payload["title"], "url": f"/doc/{doc_id}"}}
        if method == "documents.update":
            d = self.docs[payload["id"]]
            d["text"] = d["text"] + payload["text"] if payload.get("append") else payload["text"]
            return {"data": {"id": payload["id"], "title": d["title"], "url": f"/doc/{payload['id']}"}}
        if method == "documents.info":
            d = self.docs[payload["id"]]
            return {"data": {"id": payload["id"], "title": d["title"], "text": d["text"], "url": f"/doc/{payload['id']}", "updatedAt": "2026-11-11"}}
        raise AssertionError(method)


DOC = "## Цель\nФункция a возвращает 42.\n## Что сделано\nИсправлена a.py.\n## Как проверять\npython3 -c 'import a'\n"


class Escalation(ProjectCase):
    def test_mini_tier_fails_checks_and_is_rerun_one_tier_up(self):
        self.scenario({"items": {"1": {"first:haiku": [], "resume": [], "first:sonnet": [["write", "out.txt", "x"]]}}})
        flow.init(self.dir, "План", "файл out.txt появляется в проекте",
                  [item("Создать out.txt", tier="mini", files=["new:out.txt"])])
        self.assertEqual(self.plan()["items"][0]["tier"], "code-mini")
        out = flow.run(self.dir, timeout=60)
        self.assertIn("code-mini (haiku)", out)
        self.assertIn("code-mini не справился — повышен: 1: code-low (sonnet)", out)
        self.drain()
        it = self.plan()["items"][0]
        self.assertEqual((it["tier"], it["escalated_from"], it["status"]), ("code-low", "code-mini", "done"))
        models = [(c["model"], c["resume"]) for c in self.calls()]
        self.assertEqual(models, [("haiku", False), ("haiku", True), ("sonnet", False)])  # попытка, доработка, повышение
        self.assertIn("ПРЕДЫДУЩАЯ ПОПЫТКА младшей модели", self.calls()[-1]["prompt"])

    def test_blocked_worker_is_not_escalated(self):
        self.scenario({"items": {"1": {"first": [["blocked"]]}}})
        flow.init(self.dir, "План", "файл out.txt появляется в проекте",
                  [item("Создать out.txt", tier="code-mini", files=["new:out.txt"])])
        out = flow.run(self.dir, timeout=60)
        self.assertNotIn("повышен", out)
        self.assertEqual(self.plan()["items"][0]["tier"], "code-mini")  # не хватает файла — модель сильнее не поможет


class PlaneAndWiki(ProjectCase):
    def setUp(self):
        super().setUp()
        self.pl = FakePlane(os.path.basename(self.dir))
        self.ol = FakeOutline()
        plane.FACTORY = lambda: plane.Client("https://plane.test", "k", "ws", transport=self.pl)
        outline.FACTORY = lambda: (outline.Outline("https://wiki.test", "t", post=self.ol), "hermes")
        self.policy("always")

    def policy(self, value):
        path = os.path.join(os.environ["HOME"], ".config", "claude-planner", "config.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            json.dump({"docs": value} if value else {}, fh)

    def test_every_plan_gets_issue_and_passport(self):
        out = flow.init(self.dir, "План", "функция a возвращает 42", [item()],
                        passport={"stack": "Python 3.14", "deploy_rules": "systemd-юнит на vidak", "bogus": "x"})
        self.assertIn("TST-7", out)
        self.assertIn("паспорт: https://plane.test/ws/projects/p1/pages/pg1", out)
        self.assertEqual(self.pl.issues["i1"]["state"], "s1")
        page = self.pl.pages["pg1"]
        self.assertIn("Паспорт и документация проекта", page["name"])
        for needle in ("Python 3.14", "systemd-юнит на vidak", self.dir, "функция a возвращает 42"):
            self.assertIn(needle, page["description_html"])
        self.assertIn("Page проекта", self.pl.issues["i1"]["description_html"])
        self.assertIn("Критерий приёмки", self.pl.issues["i1"]["description_html"])

    def test_passport_is_reused_and_not_rewritten_when_unchanged(self):
        flow.init(self.dir, "План", "функция a возвращает 42", [item()], passport={"stack": "Python 3.14"})
        writes = [x for x in self.pl.log if "pages" in x[1] and x[0] != "GET"]
        flow.update(self.dir, hold="ждём")  # статус и цель те же — страница не переписывается
        self.assertEqual([x for x in self.pl.log if "pages" in x[1] and x[0] != "GET"], writes)
        self.assertEqual(len([x for x in self.pl.log if x == ("POST", "projects/p1/work-items/")]), 1)

    def test_agy_passport_is_adopted(self):
        os.makedirs(os.path.join(self.dir, ".planner"))
        with open(os.path.join(self.dir, ".planner", "project_meta.json"), "w") as fh:
            json.dump({"plane_project_id": "p9", "plane_project_identifier": "OLD", "plane_page_id": "pg9",
                       "stack": "Go", "outline_docs": [{"title": "Старая дока", "url": "https://wiki.test/doc/old"}]}, fh)
        out = flow.init(self.dir, "План", "функция a возвращает 42", [item()])
        self.assertIn("OLD-7", out)
        self.assertIn(("PATCH", "projects/p9/pages/pg9/"), self.pl.log)
        self.assertIn("Старая дока", self.pl.pages["pg9"]["description_html"])
        self.assertIn("Go", self.pl.pages["pg9"]["description_html"])

    def test_docs_gate_then_publish_links_everywhere(self):
        out = flow.init(self.dir, "План", "функция a возвращает 42", [item()])
        self.assertIn("вики нужна", out)
        with self.assertRaises(core.PlanError) as ctx:
            flow.wiki_publish(self.dir, "Как устроено", DOC)
        self.assertIn("Сначала закрой пункты", str(ctx.exception))
        self.write("out.txt", "x")
        out = flow.verify(self.dir, ["1"])
        self.assertIn("docs_pending", out)
        self.assertIn("wiki_publish", out)
        with self.assertRaises(core.PlanError) as ctx:
            flow.finish(self.dir, "итог работы по плану")
        self.assertIn("документация не опубликована", str(ctx.exception))
        self.assertIn("ещё нет страницы", flow.wiki_pages(self.dir))
        out = flow.wiki_publish(self.dir, "Как устроено", DOC + "token=supersecret123\n")
        self.assertIn("Опубликовано (create)", out)
        self.assertIn("plan_finish", out)
        titles = [d["title"] for d in self.ol.docs.values()]
        self.assertIn("Проект · " + os.path.basename(self.dir), titles)
        self.assertNotIn("supersecret123", json.dumps(self.ol.docs, ensure_ascii=False))
        self.assertIn("https://wiki.test/doc/", self.pl.pages["pg1"]["description_html"])
        self.assertIn("Как устроено", self.pl.issues["i1"]["description_html"])
        self.assertIn("Как устроено", flow.wiki_pages(self.dir))
        flow.finish(self.dir, "a() возвращает 42, проверено и задокументировано")
        self.assertEqual(self.pl.issues["i1"]["state"], "s2")

    def test_user_closes_without_wiki(self):
        flow.init(self.dir, "План", "функция a возвращает 42", [item()])
        self.write("out.txt", "x")
        self.assertIn("user_approval", flow.verify(self.dir, ["1"]))
        out = flow.finish(self.dir, "сделано, вики недоступна", user_approval="без вики, закрывай")
        self.assertIn("Гейты сняты решением пользователя: документация", out)
        self.assertEqual(self.plan()["status"], "finished")

    def test_docs_become_stale_after_new_work(self):
        flow.init(self.dir, "План", "функция a возвращает 42", [item()])
        self.write("out.txt", "x")
        flow.verify(self.dir, ["1"])
        flow.wiki_publish(self.dir, "Как устроено", DOC)
        self.assertEqual(self.plan()["status"], "ready")
        flow.update(self.dir, add=[item(verify=["test -f two.txt"])])
        self.write("two.txt", "x")
        flow.verify(self.dir, ["2"])
        self.assertEqual(self.plan()["status"], "docs_pending")
        self.assertIn("Опубликовано (update)", flow.wiki_publish(self.dir, "Как устроено", DOC))
        self.assertEqual(self.plan()["status"], "ready")

    def test_docs_needed_from_four_items_by_default(self):
        self.policy(None)  # политика по умолчанию — big
        self.assertIn("вики не требуется", flow.init(self.dir, "План", "функция a возвращает 42", [item()]))
        core.finish(self.dir, "план снят пользователем", user_approval="бросай", force=True)
        self.assertIn("вики нужна", flow.init(self.dir, "План", "функция a возвращает 42", [item() for _ in range(4)]))
        core.finish(self.dir, "план снят пользователем", user_approval="бросай", force=True)
        self.assertIn("вики не требуется", flow.init(self.dir, "Багфикс", "функция a снова возвращает 42",
                                                    [item() for _ in range(4)], docs=False))
        core.finish(self.dir, "план снят пользователем", user_approval="бросай", force=True)
        self.assertIn("вики нужна", flow.init(self.dir, "Новый запуск", "сервис стартует юнитом systemd",
                                             [item()], docs=True))  # меняется запуск — вики и для малого плана

    def test_cheap_worker_writes_the_page_from_plan_materials(self):
        flow.init(self.dir, "План", "функция a возвращает 42", [item()])
        self.write("out.txt", "x")
        flow.verify(self.dir, ["1"])
        out = flow.wiki_publish(self.dir, "Как устроено", notes="Решили не трогать сериализатор: его правит другой отдел.")
        self.assertIn("Опубликовано (create)", out)
        self.assertIn("Страницу написал воркер (haiku", out)
        self.assertIn("разделы: Цель, Что сделано", out)
        call = [c for c in self.calls() if "ЗАМЕТКИ ОРКЕСТРАТОРА" in c["prompt"]][-1]
        self.assertEqual((call["model"], call["tools"]), ("haiku", "Read,Grep,Glob,Bash"))  # дешёвый и только читает
        for piece in ("СТРАНИЦА: Как устроено", "ЦЕЛЬ: функция a возвращает 42", "test -f out.txt — PASS",
                      "не трогать сериализатор"):
            self.assertIn(piece, call["prompt"])
        page = next(d for d in self.ol.docs.values() if d["title"] == "Как устроено")
        self.assertIn("## Грабли", page["text"])
        self.assertNotIn("supersecret123", page["text"])  # секреты маскируются и в тексте воркера
        self.assertEqual(self.plan()["status"], "ready")
        self.scenario({"docs": "не получилось"})
        with self.assertRaisesRegex(core.PlanError, "передай markdown сам"):
            flow.wiki_publish(self.dir, "Вторая страница")

    def test_plane_failure_does_not_break_plan(self):
        def boom(*_a, **_k):
            raise plane.PlaneError("нет связи")
        plane.FACTORY = lambda: plane.Client("https://plane.test", "k", "ws", transport=boom)
        out = flow.init(self.dir, "План", "функция a возвращает 42", [item()])
        self.assertIn("не удалась", out)
        self.assertEqual(self.plan()["status"], "active")

    def test_snapshot_for_progress_panel(self):
        flow.init(self.dir, "План", "функция a возвращает 42", [item(), item(depends_on=["1"])])
        snap = flow.snapshot(self.dir)
        self.assertEqual((snap["done"], snap["total"], snap["isActive"], snap["issue"]), (0, 2, True, "TST-7"))
        self.assertEqual([i["wave"] for i in snap["items"]], [1, 2])
        self.assertIsNone(flow.snapshot(os.environ["HOME"]))


if __name__ == "__main__":
    unittest.main()
