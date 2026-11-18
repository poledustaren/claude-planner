import datetime
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from planner import report  # noqa: E402


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


class RepoCase(unittest.TestCase):
    def setUp(self):
        self.dir = os.path.realpath(tempfile.mkdtemp(prefix="report-test-"))
        git(self.dir, "init", "-q")
        git(self.dir, "config", "user.name", "t")
        git(self.dir, "config", "user.email", "t@t")
        self.write("a.py", "one\ntwo\nthree\n")
        self.write("b.py", "alpha\nbeta\n")
        self.write("old_name.txt", "".join(f"line {i}\n" for i in range(20)))
        git(self.dir, "add", "-A")
        git(self.dir, "commit", "-qm", "init")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, rel, text, mode="w"):
        path = os.path.join(self.dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, mode) as f:
            f.write(text)

    def files(self, snap, **kw):
        return {f["path"]: f for f in report.git_files(self.dir, snap, **kw)}


class SnapshotTests(RepoCase):
    def test_prior_changes_are_excluded(self):
        self.write("a.py", "one\nTWO\nthree\n")      # правка до snapshot
        self.write("pre_new.txt", "x\n")             # неотслеживаемый до snapshot
        snap = report.snapshot(self.dir)
        self.assertTrue(snap["rev"])
        self.assertTrue(snap["tree"])
        self.assertIn("pre_new.txt", snap["untracked"])
        self.assertEqual(report.git_files(self.dir, snap), [])
        self.write("b.py", "alpha\nbeta\ngamma\n")   # правка после
        files = self.files(snap)
        self.assertEqual(list(files), ["b.py"])
        self.assertEqual(files["b.py"]["status"], "M")
        self.assertEqual((files["b.py"]["added"], files["b.py"]["removed"]), (1, 0))
        self.assertTrue(files["b.py"]["diff"].startswith("@@"))
        self.assertIn("+gamma", files["b.py"]["diff"])

    def test_snapshot_does_not_touch_tree(self):
        self.write("a.py", "changed\n")
        git(self.dir, "add", "a.py")
        self.write("n.txt", "n\n")
        before = subprocess.run(["git", "status", "--porcelain"], cwd=self.dir, capture_output=True, text=True).stdout
        report.snapshot(self.dir)
        after = subprocess.run(["git", "status", "--porcelain"], cwd=self.dir, capture_output=True, text=True).stdout
        self.assertEqual(before, after)

    def test_clean_tree_uses_rev(self):
        snap = report.snapshot(self.dir)
        self.assertEqual(snap["tree"], "")
        self.write("a.py", "one\ntwo\nthree\nfour\n")
        self.assertEqual(list(self.files(snap)), ["a.py"])

    def test_lost_tree_falls_back_to_rev(self):
        self.write("a.py", "one\nTWO\nthree\n")
        snap = report.snapshot(self.dir)
        snap["tree"] = "0" * 40                      # снимок убрал git gc
        self.write("b.py", "alpha\nbeta\ngamma\n")
        self.assertEqual(sorted(self.files(snap)), ["a.py", "b.py"])

    def test_new_file_after_snapshot(self):
        snap = report.snapshot(self.dir)
        self.write("sub/new.txt", "x\ny\nz\n")
        f = self.files(snap)["sub/new.txt"]
        self.assertEqual(f["status"], "A")
        self.assertEqual((f["added"], f["removed"]), (3, 0))
        self.assertEqual(f["diff"].split("\n"), ["@@ -0,0 +1,3 @@", "+x", "+y", "+z"])
        self.assertFalse(f["binary"])

    def test_delete(self):
        snap = report.snapshot(self.dir)
        os.remove(os.path.join(self.dir, "b.py"))
        f = self.files(snap)["b.py"]
        self.assertEqual(f["status"], "D")
        self.assertEqual((f["added"], f["removed"]), (0, 2))
        self.assertIn("-alpha", f["diff"])

    def test_rename(self):
        snap = report.snapshot(self.dir)
        os.rename(os.path.join(self.dir, "old_name.txt"), os.path.join(self.dir, "new_name.txt"))
        git(self.dir, "add", "-A")
        files = self.files(snap)
        self.assertEqual(list(files), ["new_name.txt"])
        self.assertEqual(files["new_name.txt"]["status"], "R")
        self.assertEqual(files["new_name.txt"]["old_path"], "old_name.txt")

    def test_binary(self):
        snap = report.snapshot(self.dir)
        self.write("blob.bin", b"\x00\x01\x02binary", mode="wb")
        self.write("a.py", b"\x00\x00", mode="wb")
        files = self.files(snap)
        for name in ("blob.bin", "a.py"):
            self.assertTrue(files[name]["binary"], name)
            self.assertEqual(files[name]["diff"], "")

    def test_truncate_file(self):
        snap = report.snapshot(self.dir)
        self.write("big.txt", "".join(f"row {i}\n" for i in range(500)))
        old, report.MAX_FILE_DIFF = report.MAX_FILE_DIFF, 100
        try:
            f = self.files(snap)["big.txt"]
        finally:
            report.MAX_FILE_DIFF = old
        self.assertTrue(f["truncated"])
        self.assertLessEqual(len(f["diff"]), 100)
        self.assertEqual(f["added"], 500)
        self.assertTrue(f["diff"].startswith("@@"))
        self.assertRegex(f["diff"].split("\n")[-1], r"^\+row \d+$")

    def test_truncate_total(self):
        snap = report.snapshot(self.dir)
        self.write("f1.txt", "a" * 50 + "\n")
        self.write("f2.txt", "b" * 50 + "\n")
        old, report.MAX_TOTAL_DIFF = report.MAX_TOTAL_DIFF, 80
        try:
            files = self.files(snap)
        finally:
            report.MAX_TOTAL_DIFF = old
        self.assertNotEqual(files["f1.txt"]["diff"], "")
        self.assertEqual(files["f2.txt"]["diff"], "")
        self.assertTrue(files["f2.txt"]["truncated"])

    def test_exclude_dirs(self):
        snap = report.snapshot(self.dir)
        self.write(".cplan/state.json", "{}\n")
        self.write(".planner/x.txt", "x\n")
        self.write("keep.txt", "k\n")
        self.assertEqual(list(self.files(snap)), ["keep.txt"])

    def test_exclude_tracked(self):
        self.write(".cplan/tracked.txt", "1\n")
        git(self.dir, "add", "-f", ".cplan/tracked.txt")
        git(self.dir, "commit", "-qm", "c")
        snap = report.snapshot(self.dir)
        self.write(".cplan/tracked.txt", "2\n")
        self.assertEqual(report.git_files(self.dir, snap), [])

    def test_sorted(self):
        snap = report.snapshot(self.dir)
        self.write("z.txt", "z\n")
        self.write("a.py", "changed\n")
        self.write("m/k.txt", "k\n")
        paths = [f["path"] for f in report.git_files(self.dir, snap)]
        self.assertEqual(paths, sorted(paths))
        self.assertEqual(len(paths), 3)

    def test_empty_base(self):
        self.assertEqual(report.git_files(self.dir, {}), [])
        self.assertEqual(report.git_files(self.dir, None), [])


class NoGitTests(unittest.TestCase):
    def test_outside_git(self):
        d = os.path.realpath(tempfile.mkdtemp(prefix="report-nogit-"))
        self.addCleanup(shutil.rmtree, d, True)
        env = os.environ.get("GIT_CEILING_DIRECTORIES")
        os.environ["GIT_CEILING_DIRECTORIES"] = os.path.dirname(d)
        try:
            self.assertEqual(report.snapshot(d), {})
            self.assertEqual(report.git_files(d, {}), [])
        finally:
            if env is None:
                os.environ.pop("GIT_CEILING_DIRECTORIES", None)
            else:
                os.environ["GIT_CEILING_DIRECTORIES"] = env


class AttributeTests(unittest.TestCase):
    def test_attribute(self):
        files = [{"path": "planner/report.py"}, {"path": "tests/test_report.py"},
                 {"path": "docs/a.md"}, {"path": "docs/b.md"}, {"path": "zzz.txt"}]
        items = [{"id": 1, "files": ["new:planner/report.py", "docs/"]},
                 {"id": 2, "files": ["docs/a.md", "tests/test_report.py"]},
                 {"id": 3}]
        by_item, other = report.attribute(files, items)
        self.assertEqual([f["path"] for f in by_item[1]], ["planner/report.py", "docs/a.md", "docs/b.md"])
        self.assertEqual([f["path"] for f in by_item[2]], ["tests/test_report.py"])
        self.assertNotIn(3, by_item)
        self.assertEqual([f["path"] for f in other], ["zzz.txt"])

    def test_dir_prefix_needs_slash(self):
        by_item, other = report.attribute([{"path": "docsx/a"}], [{"id": 1, "files": ["docs/"]}])
        self.assertEqual(by_item, {})
        self.assertEqual(len(other), 1)


EVIL = "<script>alert(1)</script>"


def sample_file(path, diff="@@ -1,2 +1,2 @@\n ctx\n-old\n+new"):
    return {"path": path, "status": "M", "added": 1, "removed": 1, "diff": diff,
            "binary": False, "truncated": False}


class RenderTests(unittest.TestCase):
    def data(self):
        return {
            "title": EVIL, "goal": "цель", "summary": "строка 1\nстрока 2", "project": "proj",
            "branch": "wip/x", "deploy": {"target": "test", "quote": "для теста"},
            "plane": {"key": "PL-1", "url": "https://plane.example/PL-1"},
            "created": "2026-01-01T10:00:00", "finished": "2026-01-01T11:00:00",
            "review": {"verdict": "ok", "summary": "норм", "issues": ["замечание " + EVIL]},
            "docs": [{"title": "Док", "url": "https://example.com/d"}],
            "items": [
                {"id": 1, "title": "Первый", "status": "done", "tier": "main", "light": None,
                 "acceptance": "критерий", "note": "сделано " + EVIL,
                 "checks": [{"cmd": "pytest", "ok": True}, {"cmd": "lint", "ok": False}],
                 "files": [sample_file("a.py", "@@ -1,2 +1,2 @@\n ctx\n-old\n+" + EVIL),
                           {"path": "bin.dat", "status": "A", "added": 0, "removed": 0, "diff": "",
                            "binary": True, "truncated": False}]},
                {"id": "x2", "title": "Второй", "status": "skipped", "light": "мелочь",
                 "files": [{"path": "n.py", "status": "R", "old_path": "o.py", "added": 0, "removed": 0,
                            "diff": "", "binary": False, "truncated": True}]},
            ],
            "other_files": [sample_file("other.py")],
        }

    def test_escape_and_structure(self):
        out = report.render(self.data())
        self.assertTrue(out.startswith("<!doctype html>"))
        self.assertNotIn("<script", out.lower())
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", out)
        self.assertEqual(out.count("<details"), 4)
        self.assertNotIn("<details open", out)
        self.assertIn('id="item-1"', out)
        self.assertIn('id="item-x2"', out)
        self.assertIn('href="#item-1"', out)
        self.assertIn('lang="ru"', out)
        self.assertIn("Деплой: для теста", out)
        self.assertIn("без проверки", out)
        self.assertIn("двоичный файл", out)
        self.assertIn("дифф обрезан", out)
        self.assertIn("Прочие изменения", out)
        self.assertIn("Приёмка", out)
        self.assertIn("Документация", out)
        self.assertIn('rel="noopener noreferrer"', out)
        self.assertEqual(out.count("<style>"), 1)
        self.assertNotIn("http://", out.replace("https://", ""))

    def test_diff_line_numbers(self):
        f = sample_file("a.py", "@@ -10,3 +20,3 @@\n ctx\n-old\n+new")
        out = report.render({"items": [{"id": 1, "files": [f]}]})
        self.assertIn('<tr class="del"><td class="n">11</td><td class="n"></td>', out)
        self.assertIn('<tr class="add"><td class="n"></td><td class="n">21</td>', out)
        self.assertIn('<tr><td class="n">10</td><td class="n">20</td>', out)

    def test_master_label_and_empty(self):
        out = report.render({"deploy": {"target": "master"}})
        self.assertIn("Деплой: в мастер", out)
        self.assertNotIn("<details", out)
        self.assertNotIn("Приёмка", out)

    def test_empty_inputs(self):
        for data in ({}, None, {"items": None, "other_files": None, "deploy": None, "review": None,
                                "docs": None, "plane": None},
                     {"items": [{}], "title": None}):
            self.assertTrue(report.render(data).startswith("<!doctype html>"))

    def test_plan_time_is_local(self):
        tokyo = "/usr/share/zoneinfo/Asia/Tokyo"
        if not os.path.exists(tokyo):
            self.skipTest("нет базы часовых поясов")
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        link = os.path.join(tmp, "localtime")
        os.symlink(tokyo, link)
        zone, path = os.environ.get("TZ"), report.LOCALTIME
        os.environ["TZ"] = "Etc/GMT+5"  # процессу подменили пояс: на пять часов позади UTC
        time.tzset()
        try:
            report.LOCALTIME = link  # пояс машины — Токио, TZ процесса не указ
            self.assertEqual(report._when("2026-11-17T18:31:05+00:00"), "2026-11-18 03:31")
            self.assertEqual(report._when("2026-11-17T18:31:05Z"), "2026-11-18 03:31")
            self.assertEqual(report._when("2026-11-17T18:31:05"), "2026-11-17 18:31")
            self.assertEqual(report._when("вчера"), "вчера")
            self.assertEqual(report._when(None), "")
            report.LOCALTIME = os.path.join(tmp, "нет")  # пояс машины не определился — пояс процесса
            self.assertEqual(report._when("2026-11-17T18:31:05+00:00"), "2026-11-17 13:31")
        finally:
            report.LOCALTIME = path
            if zone is None:
                del os.environ["TZ"]
            else:
                os.environ["TZ"] = zone
            time.tzset()

    def test_rich_comment(self):
        note = ("STATUS: DONE\n\nПоправил `core.py` и **важное**.\n- первый <b>x</b>\n- второй\n  с продолжением\n\n"
                "## Итог\n1. раз\n2) два")
        out = report.render({"summary": "итог с `кодом`", "items": [{"id": 1, "title": "t", "note": note}]})
        self.assertNotIn("STATUS: DONE", out)
        self.assertIn("Поправил <code>core.py</code> и <b>важное</b>.", out)
        self.assertIn("<ul><li>первый &lt;b&gt;x&lt;/b&gt;</li><li>второй с продолжением</li></ul>", out)
        self.assertIn("<p><b>Итог</b></p><ul><li>раз</li><li>два</li></ul>", out)
        self.assertIn("итог с <code>кодом</code>", out)
        out = report.render({"summary": "**`core.py`**: `a ** b` и `c ** d`"})
        self.assertIn("<b><code>core.py</code></b>: <code>a ** b</code> и <code>c ** d</code>", out)

    def test_item_without_files(self):
        out = report.render({"items": [{"id": 1, "title": "t", "status": "done", "note": "n"}]})
        self.assertIn('id="item-1"', out)
        self.assertNotIn("<details", out)


class WriteTests(unittest.TestCase):
    def test_write(self):
        d = tempfile.mkdtemp(prefix="report-write-")
        self.addCleanup(shutil.rmtree, d, True)
        now = datetime.datetime(2026, 3, 4, 5, 6, 7)
        path = report.write(d, {"title": "Отчёт по Плану #1"}, now=now)
        self.assertTrue(os.path.isabs(path))
        self.assertEqual(os.path.dirname(path), os.path.join(os.path.abspath(d), "reports"))
        self.assertRegex(os.path.basename(path), r"^20260304-050607-[a-z0-9-]+\.html$")
        with open(path, encoding="utf-8") as f:
            self.assertTrue(f.read().startswith("<!doctype html>"))

    def test_slug(self):
        self.assertEqual(report._slug(None), "plan")
        self.assertEqual(report._slug("???"), "plan")
        self.assertEqual(report._slug("Привет, Мир"), "privet-mir")
        self.assertLessEqual(len(report._slug("a" * 100)), 40)


if __name__ == "__main__":
    unittest.main()
