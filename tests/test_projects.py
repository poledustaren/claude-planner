"""Правило «папка → проект Plane»: таблица, временные папки, архив, исполнитель."""
import json
import os
import subprocess
import tempfile
import unittest

from helpers import ProjectCase, flow, item, plane, projects
from test_flow import FakePlane

TABLE = {"default": "исследования", "skip": ["*/scratchpad/*"],
         "projects": {"fers": {"aliases": ["fers-*"], "paths": ["~/work/fers-migration"]},
                      "agent hub v2": ["agent-hub", "agent-hub-*"], "hub analytics": ["hub-analytics*"],
                      "task lab": ["task-lab*"]}}
LIVE = [{"id": "p-fers", "name": "fers", "identifier": "FERS"},
        {"id": "p-hub", "name": "agent hub v2", "identifier": "AGENTHUBV"},
        {"id": "p-res", "name": "исследования", "identifier": "PROJ"},
        {"id": "p-old", "name": "fers cr", "identifier": "FERSCR", "archived_at": "2026-11-15T10:00:00Z"}]


class Rule(unittest.TestCase):
    def setUp(self):
        self.base = os.path.realpath(tempfile.mkdtemp(prefix="cplan-rule-"))
        self._scratch, projects.SCRATCH_ROOTS = projects.SCRATCH_ROOTS, ()

    def tearDown(self):
        projects.SCRATCH_ROOTS = self._scratch

    def folder(self, name):
        path = os.path.join(self.base, name)
        os.makedirs(path, exist_ok=True)
        return path

    def rule(self, name):
        got = projects.resolve(self.folder(name), LIVE, TABLE)
        return got["rule"], (got["project"] or {}).get("id")

    def test_table_alias_and_exact_name(self):
        self.assertEqual(self.rule("fers-cr"), ("table", "p-fers"))  # архивный «fers cr» не выбирается
        self.assertEqual(self.rule("agent-hub-v2-dev"), ("table", "p-hub"))
        self.assertEqual(self.rule("agent-hub"), ("table", "p-hub"))

    def test_longest_pattern_wins(self):
        table = {"projects": {"a": ["agent-*"], "b": ["agent-hub-*"]}}
        self.assertEqual(projects.lookup(self.folder("agent-hub-x"), ["agent-hub-x"], table), "b")

    def test_declared_but_missing_and_unknown(self):
        self.assertEqual(self.rule("task-lab-t32"), ("declared", None))  # в таблице есть, в Plane нет
        self.assertEqual(self.rule("T-366"), ("default", "p-res"))        # незнакомая папка — общий проект
        self.assertEqual(projects.resolve(self.folder("T-1"), LIVE[:2], TABLE)["rule"], "none")

    def test_name_match_without_table(self):
        self.assertEqual(projects.resolve(self.folder("fers"), LIVE, {})["rule"], "name")

    def test_parent_folder_names_the_project(self):
        # ~/work/fers-perenos-19-208/src: папка «src» проект не называет, называет папка выше
        self.assertEqual(self.rule("work/all/fers-perenos-19-208/src"), ("table", "p-fers"))
        self.assertEqual(projects.names(self.folder("work/all/fers-perenos-19-208/src"))[:2], ["src", "fers-perenos-19-208"])
        self.assertEqual(projects._parents(os.path.expanduser("~/work/fers-perenos/src")), ["fers-perenos"])
        self.assertEqual(projects._parents(os.path.expanduser("~/work/fers-perenos")), [])
        self.assertEqual(projects._parents("/mnt/datadisk/Projects/foo"), [])

    def test_scratch_is_skipped(self):
        projects.SCRATCH_ROOTS = (self.base,)
        self.assertEqual(self.rule("qa-zu5bg2tf"), ("skip", None))
        projects.SCRATCH_ROOTS = ()
        self.assertEqual(projects.resolve(self.folder("x/scratchpad/y"), LIVE, TABLE)["rule"], "skip")

    def test_worktree_and_origin_name_the_main_repo(self):
        main = self.folder("fers")
        run = lambda *a, cwd=main: subprocess.run(a, cwd=cwd, check=True, capture_output=True)
        run("git", "init", "-q", ".")
        run("git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init")
        tree = os.path.join(self.base, "wt-8235")
        run("git", "worktree", "add", "-q", tree)
        self.assertEqual(projects.names(tree)[0], "fers")
        self.assertEqual(projects.resolve(tree, LIVE, TABLE)["project"]["id"], "p-fers")
        clone = self.folder("copy-for-work")
        run("git", "init", "-q", ".", cwd=clone)
        run("git", "remote", "add", "origin", "git@git.test:group/agent-hub-v2.git", cwd=clone)
        self.assertIn("agent-hub-v2", projects.names(clone))
        self.assertEqual(projects.resolve(clone, LIVE, TABLE)["project"]["id"], "p-hub")


class PlaneBinding(ProjectCase):
    """Сквозная проверка: что уходит в Plane при plan_init."""

    def setUp(self):
        super().setUp()
        self.pl = FakePlane("unrelated")
        self.pl.extra_projects = [dict(p) for p in LIVE]
        plane.FACTORY = lambda: plane.Client("https://plane.test", "k", "ws", transport=self.pl)
        self.table({"default": "исследования", "projects": {}})

    def tearDown(self):
        os.environ.pop(projects.TABLE_ENV, None)
        super().tearDown()

    def table(self, data):
        path = os.path.join(os.environ["HOME"], "plane-projects.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        os.environ[projects.TABLE_ENV] = path

    def meta(self):
        with open(os.path.join(self.dir, ".cplan", "meta.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def init(self, **passport):
        return flow.init(self.dir, "План", "функция a возвращает 42", [item()], passport=passport or None)

    def created(self):
        return [x for x in self.pl.log if x == ("POST", "projects/")]

    def test_unknown_folder_goes_to_default_and_creates_nothing(self):
        out = self.init()
        self.assertIn("PROJ-7", out)
        self.assertEqual(self.created(), [])
        self.assertEqual((self.meta()["plane_project_id"], self.meta()["plane_project_rule"]), ("p-res", "default"))

    def test_scratch_folder_is_not_tracked(self):
        projects.SCRATCH_ROOTS = (os.path.dirname(self.dir),)
        out = self.init()
        self.assertIn("задача не заводится", out)
        self.assertEqual([x for x in self.pl.log if x[0] != "GET"], [])
        self.assertNotIn("plane_project_id", self.meta())

    def test_table_names_project_and_declared_one_is_created(self):
        name = os.path.basename(self.dir)
        self.table({"projects": {"fers": [name]}})
        self.assertIn("FERS-7", self.init())
        self.assertEqual(self.meta()["plane_project_rule"], "table")

    def test_declared_project_is_created_once(self):
        self.table({"projects": {"task lab": [os.path.basename(self.dir)]}})
        self.pl.created_project = {"id": "p-new", "identifier": "TASKLAB"}
        self.assertIn("TASKLAB-7", self.init())
        self.assertEqual(len(self.created()), 1)

    def test_archived_binding_is_resolved_again(self):
        os.makedirs(os.path.join(self.dir, ".cplan"), exist_ok=True)
        with open(os.path.join(self.dir, ".cplan", "meta.json"), "w") as fh:
            json.dump({"plane_project_id": "p-old", "plane_project_identifier": "FERSCR", "plane_page_id": "pg9"}, fh)
        self.table({"projects": {"fers": [os.path.basename(self.dir)]}})
        self.assertIn("FERS-7", self.init())
        self.assertEqual(self.meta()["plane_project_id"], "p-fers")

    def test_legacy_binding_yields_to_table_but_survives_without_it(self):
        os.makedirs(os.path.join(self.dir, ".cplan"), exist_ok=True)
        with open(os.path.join(self.dir, ".cplan", "meta.json"), "w") as fh:
            json.dump({"plane_project_id": "p-res", "plane_project_identifier": "PROJ"}, fh)
        self.table({"projects": {"fers": [os.path.basename(self.dir)]}})
        self.assertIn("FERS-7", self.init())

    def test_explicit_project_wins_and_unknown_is_reported(self):
        self.assertIn("AGENTHUBV-7", self.init(plane_project="agent hub v2"))
        self.assertEqual(self.meta()["plane_project_rule"], "explicit")

    def test_explicit_unknown_project_is_an_error_not_a_new_project(self):
        out = self.init(plane_project="нет такого")
        self.assertIn("в Plane нет", out)
        self.assertEqual(self.created(), [])

    def test_assignee_is_claude_member(self):
        self.pl.members = [{"id": "u-agy", "display_name": "agy"}, {"id": "u-claude", "display_name": "claude"}]
        self.init()
        self.assertEqual(self.pl.issues["i1"]["assignees"], ["u-claude"])

    def test_assignee_falls_back_to_key_owner(self):
        self.init()
        self.assertEqual(self.pl.issues["i1"]["assignees"], ["u1"])


if __name__ == "__main__":
    unittest.main()
