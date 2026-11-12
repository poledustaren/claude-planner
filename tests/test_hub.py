import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from helpers import ProjectCase, flow, item

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
        self.assertEqual(self.last(), {"status": "active", "hold": "", "done": 0, "total": 2, "title": "План хаба"})

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

    def test_silent_outside_hub_session(self):
        os.environ.pop(hub.ENV_TASK)
        flow.init(self.dir, "Обычный план", "файл out.txt создан и проверен", [item()])
        self.assertEqual(self.runner.got, [])
        self.assertIsNone(hub.target())

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
