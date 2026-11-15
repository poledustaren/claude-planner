import json
import os
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from helpers import ProjectCase, core, flow, item, workers

from planner import hub

ENV = (hub.ENV_URL, hub.ENV_TOKEN_FILE, hub.ENV_TASK)


class Runner:
    """Подставной раннер хаба: принимает события плана и запоминает их."""

    def __init__(self, status=200):
        self.got = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.got.append({"path": self.path, "auth": self.headers.get("Authorization"),
                                  "task": self.headers.get("X-Hub2-Task"), "body": body})
                self.send_response(status)
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/api/runner/plan"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class HubEvents(ProjectCase):
    def setUp(self):
        super().setUp()
        self._hub_env = {k: os.environ.get(k) for k in (*ENV, "HTTP_PROXY", "http_proxy")}
        self.runner = Runner()
        token = os.path.join(self.home, "hook-token")
        with open(token, "w", encoding="utf-8") as fh:
            fh.write("secret-token\n")
        os.environ.update({hub.ENV_URL: self.runner.url, hub.ENV_TOKEN_FILE: token, hub.ENV_TASK: "task-1",
                           # Прокси из окружения сессии не должен уводить запрос с этого хоста.
                           "HTTP_PROXY": "http://127.0.0.1:9", "http_proxy": "http://127.0.0.1:9"})

    def tearDown(self):
        self.runner.close()
        for k, v in self._hub_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        super().tearDown()

    def last(self):
        return self.runner.got[-1]["body"]

    def test_plan_lifecycle_is_reported(self):
        flow.init(self.dir, "План хаба", "файл out.txt создан и проверен", [item(), item(title="Вторая штука")])
        first = self.runner.got[-1]
        self.assertEqual((first["path"], first["auth"], first["task"]),
                         ("/api/runner/plan", "Bearer secret-token", "task-1"))
        self.assertEqual({k: self.last()[k] for k in ("status", "hold", "done", "total", "title")},
                         {"status": "active", "hold": "", "done": 0, "total": 2, "title": "План хаба"})

        flow.update(self.dir, hold="какой вариант выбрать?")
        self.assertEqual((self.last()["hold"], self.last()["status"]), ("какой вариант выбрать?", "active"))
        flow.update(self.dir, hold="")
        self.assertEqual(self.last()["hold"], "")

        self.write("out.txt", "ok")
        flow.run(self.dir, wait=False)
        flow.verify(self.dir)
        self.assertEqual((self.last()["done"], self.last()["total"]), (2, 2))

        flow.finish(self.dir, "оба пункта закрыты, файл проверен")
        self.assertEqual((self.last()["status"], self.last()["done"]), ("finished", 2))
        self.assertEqual(self.last()["details"]["summary"], "оба пункта закрыты, файл проверен")

    def test_details_carry_process_structure(self):
        flow.init(self.dir, "План хаба", "файл out.txt создан и проверен",
                  [item(files=["new:out.txt"]),
                   {"title": "Вторая штука", "acceptance": "отлажено вручную", "light": "debug",
                    "depends_on": ["1"]}])
        details = self.last()["details"]
        self.assertEqual((details["goal"], details["next"] != "", details["updatedAt"] != ""),
                         ("файл out.txt создан и проверен", True, True))
        self.assertNotIn("summary", details)
        first, second = details["items"]
        self.assertEqual((first["id"], first["status"], first["tier"], first["wave"], first["acceptance"],
                          first["files"], first["dependsOn"]),
                         ("1", "todo", "main", 1, "файл содержит нужную строку", ["new:out.txt"], []))
        self.assertEqual((second["dependsOn"], second["light"]), (["1"], "debug"))
        self.assertNotIn("evidence", first)
        self.assertNotIn("agent", first)
        self.assertEqual((details["jobs"], details["scouts"]), ([], []))

        self.write("out.txt", "ok")
        flow.run(self.dir, wait=False)
        flow.verify(self.dir)
        evidence = self.last()["details"]["items"][0]["evidence"]
        self.assertEqual((evidence["passed"], evidence["label"]), (True, "test -f out.txt"))
        self.assertTrue(evidence["at"])

    def test_details_include_jobs_and_scouts(self):
        scout = {"id": "scout1-120000-abcd", "root": self.dir, "kind": "scout", "tier": "scout",
                 "item_id": None, "status": "DONE", "started": time.time() - 10, "ended": time.time() - 5,
                 "model": "haiku", "prompt": "Что внутри?", "reason": ""}
        workers.write_job(scout)
        core.atomic_write(workers.stream_path(self.dir, scout["id"]),
                          json.dumps({"type": "result", "result": "бриф по разведке"}) + "\n")
        flow.init(self.dir, "План хаба", "файл out.txt создан и проверен", [item()])
        details = self.last()["details"]
        self.assertEqual(details["scouts"], [{"id": "scout1-120000-abcd", "question": "Что внутри?",
                                              "model": "haiku", "status": "DONE", "sec": 5,
                                              "brief": "бриф по разведке"}])
        job = {"id": "i1-120000-abcd", "root": self.dir, "kind": "item", "tier": "main", "item_id": "1",
               "status": "RUNNING", "started": time.time(), "model": "sonnet", "prompt": "", "reason": ""}
        workers.write_job(job)
        flow.update(self.dir, hold="ждём ответа")
        details = self.last()["details"]
        view = next(j for j in details["jobs"] if j["id"] == job["id"])
        self.assertEqual((view["item"], view["kind"], view["running"], view["status"]),
                         ("1", "item", True, "RUNNING"))
        self.assertEqual(details["items"][0]["agent"], job["id"])

    def test_empty_plan_does_not_break_event(self):
        self.assertFalse(hub.notify(None))
        self.assertEqual(hub.event({}), {"status": "active", "hold": "", "done": 0, "total": 0, "title": ""})
        empty = {"goal": "", "next": "", "updatedAt": "", "items": [], "jobs": [], "scouts": []}
        self.assertEqual(hub.event({}, empty)["details"],
                         {"goal": "", "next": "", "updatedAt": "", "items": [], "jobs": [], "scouts": []})

    def test_silent_outside_hub_session(self):
        os.environ.pop(hub.ENV_TASK)
        flow.init(self.dir, "Обычный план", "файл out.txt создан и проверен", [item()])
        self.assertEqual(self.runner.got, [])
        self.assertIsNone(hub.target())

    def outside_hub(self, plane=True):
        """Сессия вне хаба на хосте с раннером: окружения HUB2_* нет, есть ~/.hub2 и задача Plane у плана."""
        for k in ENV:
            os.environ.pop(k, None)
        os.makedirs(os.path.join(self.home, ".hub2"))
        with open(os.path.join(self.home, ".hub2", "config.json"), "w") as fh:
            json.dump({"port": self.runner.server.server_port, "bind": ["127.0.0.1", "100.64.0.1"]}, fh)
        with open(os.path.join(self.home, ".hub2", "hook-token"), "w") as fh:
            fh.write("local-token\n")
        flow.init(self.dir, "План из терминала", "файл out.txt создан и проверен", [item()])
        if plane:
            with core.locked(self.dir):
                plan = core.load(self.dir)
                plan["plane"] = {"issue_id": "11111111-2222-4333-8444-555555555555", "issue_key": "FERS-7"}
                core.save(self.dir, plan)

    def test_session_outside_hub_is_reported_as_external(self):
        self.outside_hub()
        self.assertEqual(self.runner.got, [])  # у плана ещё не было задачи Plane — сообщать не о чем
        os.makedirs(os.path.dirname(hub.owner_path(self.dir)), exist_ok=True)
        with open(hub.owner_path(self.dir), "w") as fh:
            json.dump({"plan": core.load(self.dir)["created_at"], "session_id": "sess-1",
                       "transcript_path": "/home/u/.claude/projects/p/sess-1.jsonl"}, fh)
        flow.update(self.dir, hold="ждём ответа")
        got = self.runner.got[-1]
        self.assertEqual((got["auth"], got["task"]), ("Bearer local-token", "11111111-2222-4333-8444-555555555555"))
        self.assertEqual(got["body"]["external"], {
            "agent": "claude", "cwd": self.dir, "label": "FERS-7", "phase": "idle", "sessionId": "sess-1",
            "transcriptPath": "/home/u/.claude/projects/p/sess-1.jsonl"})
        self.assertEqual(got["body"]["details"]["planDir"], os.path.join(self.dir, ".cplan"))
        flow.update(self.dir, hold="")
        self.assertEqual(self.last()["external"]["phase"], "busy")
        self.assertTrue(hub.notify(core.load(self.dir), None, phase="idle"))  # так сообщает хук Stop
        self.assertEqual(self.last()["external"]["phase"], "idle")

    def test_hub_session_event_has_no_external_block(self):
        flow.init(self.dir, "План хаба", "файл out.txt создан и проверен", [item()])
        self.assertNotIn("external", self.last())
        self.assertEqual(self.last()["details"]["planDir"], os.path.join(self.dir, ".cplan"))

    def test_outside_hub_without_plane_issue_or_runner_is_silent(self):
        self.outside_hub(plane=False)
        flow.update(self.dir, hold="ждём")
        self.assertEqual(self.runner.got, [])
        os.remove(os.path.join(self.home, ".hub2", "config.json"))
        self.assertIsNone(hub.target({"plane": {"issue_id": "x"}, "items": []}))

    def test_no_token_file_means_no_event(self):
        os.environ[hub.ENV_TOKEN_FILE] = os.path.join(self.home, "missing")
        self.assertFalse(hub.notify({"title": "x", "status": "active", "items": []}))
        self.assertEqual(self.runner.got, [])

    def test_hub_down_does_not_break_planner(self):
        self.runner.close()
        out = flow.init(self.dir, "План без хаба", "файл out.txt создан и проверен", [item()])
        self.assertIn("План создан", out)
        self.runner = Runner()  # для tearDown

    def test_refusal_is_not_an_error(self):
        self.runner.close()
        self.runner = Runner(status=404)
        os.environ[hub.ENV_URL] = self.runner.url
        self.assertFalse(hub.notify({"title": "x", "status": "active", "items": []}))
        self.assertEqual(len(self.runner.got), 1)


if __name__ == "__main__":
    unittest.main()
