import json
import os
import subprocess
import sys
import unittest

from helpers import ROOT, ProjectCase, core, flow, item

HOOKS = os.path.join(ROOT, "planner", "hooks.py")
APPROVED = {"verdict": "approved", "issues": []}


def hook(mode, data):
    proc = subprocess.run([sys.executable, HOOKS, mode], input=json.dumps(data), capture_output=True, text=True,
                          timeout=20)
    return json.loads(proc.stdout) if proc.stdout.strip() else {}


class Baseline(ProjectCase):
    def setUp(self):
        super().setUp()
        self.write("cplan.json", json.dumps({"baseline": ["test -f lint.ok"]}))
        self.git("add", "-A")
        self.git("commit", "-qm", "baseline")

    def done_plan(self, **kw):
        flow.init(self.dir, "План", "цель плана достаточно длинная", [item()], **kw)
        self.write("out.txt", "x")
        core.verify_items(self.dir, ["1"])

    def test_red_baseline_blocks_finish_until_fixed(self):
        self.done_plan()
        with self.assertRaises(core.PlanError) as ctx:
            flow.finish(self.dir, "готово, проверки пунктов прошли")
        self.assertIn("базовые проверки проекта не прошли", str(ctx.exception))
        self.assertIn("test -f lint.ok", str(ctx.exception))
        self.assertEqual(self.plan()["status"], "ready")
        self.assertFalse(self.plan()["baseline"]["passed"])
        self.assertTrue(os.path.isfile(os.path.join(self.dir, self.plan()["baseline"]["log"])))
        self.write("lint.ok", "")
        out = flow.finish(self.dir, "готово, проверки пунктов прошли")
        self.assertIn("Базовые проверки проекта: прошли", out)
        self.assertNotIn("user_override", self.plan())

    def test_plain_approval_does_not_waive_baseline(self):
        self.done_plan()
        with self.assertRaises(core.PlanError):
            flow.finish(self.dir, "закрываем как договорились", user_approval="да, закрывай без вики")
        with self.assertRaises(core.PlanError) as ctx:
            flow.finish(self.dir, "закрываем как договорились", waive=["baseline"])
        self.assertIn("user_approval", str(ctx.exception))
        out = flow.finish(self.dir, "линтер красный и до нас", user_approval="да, линтер чиним отдельно",
                          waive=["baseline"])
        self.assertIn("НЕ ПРОШЛИ", out)
        over = self.plan()["user_override"]
        self.assertEqual(over["user_approval"], "да, линтер чиним отдельно")
        self.assertIn("базовые проверки проекта не прошли", over["gates"][0])

    def test_naming_one_gate_does_not_waive_another(self):
        self.done_plan(review=True)
        self.write("lint.ok", "")
        with self.assertRaises(core.PlanError) as ctx:
            flow.finish(self.dir, "приёмку пропускать не разрешали", user_approval="да", waive=["baseline"])
        self.assertIn("не пройдена приёмка", str(ctx.exception))
        flow.finish(self.dir, "приёмку пользователь снял", user_approval="да, без приёмки", waive=["review"])
        self.assertEqual(self.plan()["user_override"]["gates"], ["не пройдена приёмка"])

    def test_checklist_plan_skips_baseline(self):
        flow.init(self.dir, "Мелочь", "поправить опечатку в сообщении", [{"title": "Опечатка", "light": "trivial"}])
        core.verify_items(self.dir, ["1"], note="исправил слово в строке сообщения")
        self.assertIn("закрыт", flow.finish(self.dir, "опечатка исправлена, смотрел глазами"))
        self.assertNotIn("baseline", self.plan())

    def test_changed_baseline_file_needs_the_user(self):
        self.done_plan()
        self.write("cplan.json", json.dumps({"baseline": ["test -f out.txt"]}))  # ослабили проверку в обход хука
        with self.assertRaises(core.PlanError) as ctx:
            flow.finish(self.dir, "готово, проверки пунктов прошли")
        self.assertIn("cplan.json изменён после создания плана", str(ctx.exception))

    def test_broken_baseline_file_is_loud(self):
        self.done_plan()
        self.write("cplan.json", "{не json")
        with self.assertRaises(core.PlanError) as ctx:
            flow.finish(self.dir, "готово, проверки пунктов прошли")
        self.assertIn("cplan.json не читается", str(ctx.exception))

    def test_review_does_not_start_on_red_baseline(self):
        self.scenario({"reviews": [APPROVED]})
        self.done_plan(review=True)
        with self.assertRaises(core.PlanError) as ctx:
            flow.review(self.dir)
        self.assertIn("Приёмку не запускаю", str(ctx.exception))
        self.assertEqual(self.calls(), [])  # приёмщик не стартовал — деньги на красную сборку не потрачены
        self.write("lint.ok", "")
        self.assertIn("APPROVED", flow.review(self.dir))

    def test_model_cannot_edit_or_claim_baseline_file(self):
        edit = hook("plan_gate", {"tool_name": "Edit", "tool_input": {"file_path": os.path.join(self.dir, "cplan.json")}})
        self.assertEqual(edit["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("только пользователь", edit["hookSpecificOutput"]["permissionDecisionReason"])
        self.done_plan()  # открытый план запрета не снимает
        edit = hook("plan_gate", {"tool_name": "Write", "tool_input": {"file_path": os.path.join(self.dir, "cplan.json")}})
        self.assertEqual(edit["hookSpecificOutput"]["permissionDecision"], "deny")
        nested = hook("plan_gate", {"tool_name": "Write",
                                    "tool_input": {"file_path": os.path.join(self.dir, "docs", "cplan.json")}})
        self.assertEqual(nested, {})  # одноимённый файл не в корне — обычный файл проекта
        with self.assertRaises(core.PlanError):
            core.normalize_files(["cplan.json"], self.dir, "пункт 2")


class Dependencies(ProjectCase):
    def setUp(self):
        super().setUp()
        self.write("requirements.txt", "requests==2.32.0\n")
        self.write("package.json", '{\n  "name": "x",\n  "version": "1.0.0",\n  "scripts": {\n    "test": "jest"\n  },\n'
                                   '  "dependencies": {\n    "left-pad": "^1.3.0"\n  }\n}\n')
        self.write("app/App.csproj", '<Project>\n  <PropertyGroup>\n    <Nullable>enable</Nullable>\n  </PropertyGroup>\n'
                                     '</Project>\n')
        self.git("add", "-A")
        self.git("commit", "-qm", "manifests")

    def done_plan(self):
        flow.init(self.dir, "План", "цель плана достаточно длинная", [item()])
        self.write("out.txt", "x")
        core.verify_items(self.dir, ["1"])

    def test_new_dependency_needs_user_approval(self):
        self.done_plan()
        self.write("requirements.txt", "requests==2.32.0\nreqeusts-toolbelt==1.0\n")
        with self.assertRaises(core.PlanError) as ctx:
            flow.finish(self.dir, "готово, проверки пунктов прошли")
        self.assertIn("изменены манифесты зависимостей: requirements.txt", str(ctx.exception))
        self.assertIn('waive=["baseline"] или ["deps"]', str(ctx.exception))
        with self.assertRaises(core.PlanError):  # согласие «на всё» зависимость не пропускает
            flow.finish(self.dir, "готово, проверки пунктов прошли", user_approval="да, закрывай")
        out = flow.finish(self.dir, "добавили toolbelt", user_approval="да, toolbelt нужен", waive=["deps"])
        self.assertIn("Манифесты зависимостей менялись: requirements.txt", out)
        self.assertEqual(self.plan()["deps_changed"], ["requirements.txt"])
        self.assertIn("requirements.txt", self.plan()["user_override"]["gates"][0])

    def test_mixed_manifests_are_judged_by_added_lines(self):
        base = core.git_head(self.dir)
        self.write("package.json", '{\n  "name": "x",\n  "version": "1.0.1",\n  "scripts": {\n    "test": "jest",\n'
                                   '    "lint": "eslint ."\n  },\n  "dependencies": {\n    "left-pad": "^1.3.0"\n  }\n}\n')
        self.write("app/App.csproj", '<Project>\n  <PropertyGroup>\n    <Nullable>disable</Nullable>\n  </PropertyGroup>\n'
                                     '</Project>\n')
        self.assertEqual(core.dep_changes(self.dir, base), [])  # версия, скрипт и свойство сборки — не зависимости
        self.write("package.json", '{\n  "name": "x",\n  "version": "1.0.1",\n  "dependencies": {\n'
                                   '    "left-pad": "^1.3.0",\n    "lodahs": "^4.17.21"\n  }\n}\n')
        self.write("app/App.csproj", '<Project>\n  <ItemGroup>\n    <PackageReference Include="Newtonsoft.Json" '
                                     'Version="13.0.3" />\n  </ItemGroup>\n</Project>\n')
        self.write("tools/go.mod", "module x\n\nrequire example.com/y v1.2.3\n")  # новый манифест целиком
        self.assertEqual(core.dep_changes(self.dir, base), ["app/App.csproj", "package.json", "tools/go.mod"])

    def test_policy_warn_and_off(self):
        self.done_plan()
        self.write("requirements.txt", "requests==2.32.0\nrich==13.7\n")
        self.config({"dep_gate": "warn"})
        out = flow.finish(self.dir, "готово, проверки пунктов прошли")
        self.assertIn("Манифесты зависимостей менялись", out)
        self.assertNotIn("user_override", self.plan())
        self.config({"dep_gate": "off"})
        self.done_plan()
        self.assertNotIn("Манифесты", flow.finish(self.dir, "готово, проверки пунктов прошли"))

    def test_project_can_name_its_own_manifests(self):
        base = core.git_head(self.dir)
        self.write("cplan.json", json.dumps({"dep_files": ["deps.edn"]}))
        self.write("deps.edn", "{:deps {}}\n")
        self.assertEqual(core.dep_changes(self.dir, base), ["deps.edn"])

    def test_reviewer_is_told_about_dependency_changes(self):
        self.done_plan()
        self.write("requirements.txt", "requests==2.32.0\nrich==13.7\n")
        prompt = flow.review_prompt(self.plan(), None, None)
        self.assertIn("ИЗМЕНЕНЫ МАНИФЕСТЫ ЗАВИСИМОСТЕЙ: requirements.txt", prompt)
        self.assertIn("blocker", prompt)

    def test_finish_tool_accepts_waive(self):
        from planner import server
        schema = next(t for t in server.TOOLS if t["name"] == "plan_finish")["inputSchema"]
        self.assertEqual(schema["properties"]["waive"]["items"]["enum"], ["review", "docs", "baseline", "deps"])
        with self.assertRaises(core.PlanError):
            core.normalize_waive(["tests"], "да")


if __name__ == "__main__":
    unittest.main()
