import glob
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from helpers import ProjectCase, core, flow, item, workers  # noqa: F401
from planner import hub, report

NOTE = "поправил a() и убедился, что она возвращает пять"


class ReportFlowTest(ProjectCase):
    def run_plan(self, note=NOTE):
        flow.init(self.dir, "План отчёта", "после закрытия лежит HTML-отчёт",
                  [item("Поправить функцию", files=["a.py"], verify=["grep -q 'return 5' a.py"])])
        self.write("a.py", "def a():\n    return 5\n")
        flow.run(self.dir, wait=False)
        flow.verify(self.dir, note=note)
        return flow.finish(self.dir, "всё сделано, проверка прошла")

    def reports(self):
        return glob.glob(os.path.join(self.dir, ".cplan", "reports", "*.html"))

    def test_report_written_and_linked(self):
        out = self.run_plan()
        files = self.reports()
        self.assertEqual(len(files), 1)
        with open(files[0], encoding="utf-8") as fh:
            page = fh.read()
        self.assertIn("Поправить функцию", page)
        self.assertIn(NOTE, page)
        self.assertIn("return 5", page)
        self.assertIn(f"Отчёт: {files[0]}", out)
        plan = self.plan()
        self.assertEqual(plan["report"], "reports/" + os.path.basename(files[0]))
        self.assertIn(f"Отчёт: {files[0]}", flow.show(self.dir))

    def test_details_and_snapshot_carry_report(self):
        self.run_plan()
        snap = flow.snapshot(self.dir)
        self.assertEqual(hub._details(snap)["report"], self.plan()["report"])

    def test_main_item_keeps_note(self):
        flow.init(self.dir, "План отчёта", "заметка обычного пункта сохраняется", [item(verify=["test -f a.py"])])
        flow.run(self.dir, wait=False)
        flow.verify(self.dir, note=NOTE)
        it = core.find(self.plan(), "1")
        self.assertEqual((it["status"], it["done_note"]), ("done", NOTE))

    def test_main_item_not_closed_by_note_alone(self):
        flow.init(self.dir, "План отчёта", "заметка не закрывает пункт без проверок", [item(verify=["test -f nope.txt"])])
        flow.run(self.dir, wait=False)
        flow.verify(self.dir, note=NOTE)
        self.assertEqual(self.status("1"), "failed")

    def test_force_finish_writes_report(self):
        flow.init(self.dir, "План отчёта", "force тоже оставляет отчёт", [item(verify=["test -f nope.txt"])])
        out = flow.finish(self.dir, "закрыт как есть решением пользователя", user_approval="закрывай как есть", force=True)
        self.assertEqual(len(self.reports()), 1)
        self.assertIn("Отчёт:", out)

    def test_no_git_project(self):
        plain = os.path.realpath(tempfile.mkdtemp(prefix="cplan-nogit-"))
        self.addCleanup(shutil.rmtree, plain, True)
        flow.init(plain, "План без git", "отчёт пишется и без репозитория", [item(verify=["test -d ."])])
        self.assertEqual(core.load(plain)["base_snap"], {})
        flow.run(plain, wait=False)
        flow.verify(plain)
        out = flow.finish(plain, "закрыто без git, проверка прошла")
        files = glob.glob(os.path.join(plain, ".cplan", "reports", "*.html"))
        self.assertEqual(len(files), 1)
        self.assertIn("Отчёт:", out)
        with open(files[0], encoding="utf-8") as fh:
            self.assertNotIn("diff", fh.read().split("</style>")[1])

    def test_report_failure_does_not_block_finish(self):
        flow.init(self.dir, "План отчёта", "сбой отчёта не мешает закрытию", [item(verify=["test -f a.py"])])
        flow.run(self.dir, wait=False)
        flow.verify(self.dir)
        with mock.patch.object(report, "write", side_effect=RuntimeError("диск полон")):
            out = flow.finish(self.dir, "закрыто, отчёт сломался")
        self.assertEqual(self.plan()["status"], "finished")
        self.assertIn("отчёт не собран: RuntimeError: диск полон", out)
        self.assertNotIn("report", self.plan())

    def test_snapshot_failure_does_not_block_init(self):
        with mock.patch.object(report, "snapshot", side_effect=RuntimeError("git")):
            flow.init(self.dir, "План отчёта", "сбой снимка не роняет init", [item()])
        self.assertEqual(self.plan()["base_snap"], {})


if __name__ == "__main__":
    unittest.main()
