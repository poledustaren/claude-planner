import os
import unittest
from unittest import mock

from helpers import ProjectCase, core, flow, item, plane, workers  # noqa: F401
from planner import hub


def no_env():
    return mock.patch.dict(os.environ, {"HUB2_DEPLOY": ""})


class DeployTest(ProjectCase):
    def light(self):
        return {"title": "Мелочь", "light": "trivial", "tier": "main"}

    def init(self, items=None, **kw):
        return flow.init(self.dir, "План деплоя", "Проверяем поле деплоя в плане",
                         items or [item()], **kw)

    def test_refused_without_deploy(self):
        with no_env(), self.assertRaises(core.PlanError) as cm:
            self.init()
        self.assertIn("Куда деплоим", str(cm.exception))
        self.assertIsNone(core.load(self.dir, required=False))

    def test_refusal_does_not_archive_the_old_plan_twice(self):
        with no_env():
            self.init([self.light()])
            plan = self.plan()
            plan["status"] = "finished"
            core.save(self.dir, plan)
            archive = os.path.join(self.dir, ".cplan", "archive")
            for _ in range(2):
                with self.assertRaises(core.PlanError):
                    self.init()
            self.assertFalse(os.path.isdir(archive) and os.listdir(archive))
            self.init(deploy="test", deploy_quote="для теста")
            self.assertEqual(len(os.listdir(archive)), 1)

    def test_deploy_with_quote(self):
        with no_env():
            self.init(deploy="test", deploy_quote="для теста")
        self.assertEqual(self.plan()["deploy"], {"target": "test", "quote": "для теста", "source": "user"})

    def test_deploy_without_quote(self):
        with no_env(), self.assertRaises(core.PlanError) as cm:
            self.init(deploy="master")
        self.assertIn("deploy_quote", str(cm.exception))

    def test_light_plan_needs_no_deploy(self):
        with no_env():
            self.init([self.light()])
        self.assertNotIn("deploy", self.plan())

    def test_env_supplies_deploy(self):
        with mock.patch.dict(os.environ, {"HUB2_DEPLOY": "test"}):
            self.init()
        self.assertEqual(self.plan()["deploy"]["source"], "hub")
        self.assertEqual(self.plan()["deploy"]["target"], "test")

    def test_update_adding_first_checked_item(self):
        with no_env():
            self.init([self.light()])
            with self.assertRaises(core.PlanError) as cm:
                flow.update(self.dir, add=[item("Проверяемый")])
            self.assertIn("Куда деплоим", str(cm.exception))
            flow.update(self.dir, add=[item("Проверяемый")], deploy="master", deploy_quote="в мастер")
        self.assertEqual(self.plan()["deploy"]["target"], "master")

    def test_shown_everywhere(self):
        with no_env():
            self.init(deploy="test", deploy_quote="для теста")
        plan = self.plan()
        self.assertIn("Деплой: для теста", flow.show(self.dir))
        self.assertIn("Деплой: для теста", plane.issue_html(plan, {}))
        self.assertEqual(hub._details(flow.snapshot(self.dir))["deploy"], "test")
        self.assertIn("Деплой: для теста", workers.item_prompt(plan, plan["items"][0]))


if __name__ == "__main__":
    unittest.main()
