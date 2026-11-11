import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from planner import core, flow, outline, plane, workers  # noqa: E402,F401

FAKE = os.path.join(ROOT, "tests", "fake_claude.py")


def item(title="Сделать штуку", verify=None, **kw):
    base = {"title": title, "acceptance": "файл содержит нужную строку", "tier": "main",
            "verify": verify or ["test -f out.txt"]}
    base.update(kw)
    return base


class ProjectCase(unittest.TestCase):
    """Временный git-проект + подставной claude + выключенный Plane."""

    def setUp(self):
        self.dir = os.path.realpath(tempfile.mkdtemp(prefix="cplan-test-"))
        subprocess.run(["git", "init", "-q", self.dir], check=True)
        self.write("a.py", "def a():\n    return 1\n")
        self.write("b.py", "def b():\n    return 2\n")
        subprocess.run("git add -A && git -c user.email=t@t -c user.name=t commit -qm init", shell=True,
                       cwd=self.dir, check=True)
        self._env = {k: os.environ.get(k) for k in ("CPLAN_CLAUDE_BIN", "HOME")}
        os.environ["CPLAN_CLAUDE_BIN"] = FAKE
        self.home = tempfile.mkdtemp(prefix="cplan-home-")
        os.environ["HOME"] = self.home  # конфиги, кэш хуков и сессии — в песочнице
        plane.FACTORY = lambda: None
        outline.FACTORY = lambda: None
        self.scenario({})

    def tearDown(self):
        for job in workers.active_jobs(self.dir):
            workers.kill(self.dir, job)
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        plane.FACTORY = None
        outline.FACTORY = None
        shutil.rmtree(self.dir, ignore_errors=True)
        shutil.rmtree(self.home, ignore_errors=True)

    def write(self, rel, text):
        path = os.path.join(self.dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def scenario(self, data):
        self.write(".fake.json", json.dumps(data, ensure_ascii=False))

    def calls(self):
        try:
            with open(os.path.join(self.dir, ".fake_calls.jsonl"), encoding="utf-8") as fh:
                return [json.loads(line) for line in fh]
        except OSError:
            return []

    def plan(self):
        return core.load(self.dir)

    def status(self, item_id):
        return core.find(self.plan(), item_id)["status"]

    def drain(self):
        flow.jobs(self.dir, wait=True, mode="all", timeout=60)
