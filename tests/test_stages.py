import json

from helpers import ProjectCase, core, flow, item, outline, workers
from planner import doclint
from test_flow import FakeOutline

CHECK = ["python3 -c 'import a,sys; sys.exit(0 if a.a()==42 else 1)'"]
STAGES = [
    {"name": "design", "paths": ["design/**"], "required_for": ["web/**"], "gate": "user"},
    {"name": "dev", "tiers": ["main", "code-mini", "code-low", "code"], "gate": "review"},
    {"name": "deliver", "paths": ["infra/**", ".gitlab-ci.yml"], "tiers": ["main", "code"], "mcp": ["nodes"],
     "bash": ["tofu validate"], "verify": ["cmd", "node"], "fix_rounds": 0, "gate": "user"},
]
PAGE = """---
type: howto
audience: support
title: Клиент не видит начисления
checks:
  - q: В какой ситуации применять эту инструкцию?
    a: Клиент открыл счёт и не видит начисления за месяц.
  - q: Что сказать клиенту сразу?
    a: Проверим за две минуты, оставайтесь на линии.
  - q: Когда передавать разработчикам и что приложить?
    a: Если список услуг пуст и после шага 1; приложить лицевой счёт.
---
<!-- Правила для автора (в страницу не попадают) -->

# Клиент не видит начисления — что делать

**Коротко:** клиент открыл счёт и не видит суммы. Инструкция возвращает её на экран.

## Как узнать эту ситуацию

- Клиент говорит: «Счёт пустой».

## Схема

```mermaid
flowchart TD
  A["Клиент сообщает: счёт пустой"] --> B{"Признаки совпали?"}
  B -- да --> C["Шаг 1"]
```

## Шаги

1. Откройте «Счета» → «Текущий месяц».
   На экране появится: список услуг.

## Если не помогло — передайте разработчикам

Передавайте, если список услуг пуст.
"""


def allowed(call):
    argv = call["argv"]
    return argv[argv.index("--allowedTools") + 1:argv.index("--disallowedTools")]


class Limits(ProjectCase):
    def job(self, tier):
        return {"tier": tier, "session_id": "sid", "system": "s", "schema": None}

    def test_limits_come_from_config_and_reach_the_worker(self):
        self.config({"tiers": {"code": {"max_turns": 40, "max_usd": 2.5}, "scout": {"max_turns": 0, "max_usd": "много"}}})
        cmd = workers.build_cmd(self.job("code"), resume=False)
        self.assertEqual(cmd[cmd.index("--max-turns") + 1], "40")
        self.assertEqual(cmd[cmd.index("--max-budget-usd") + 1], "2.5")
        scout = workers.build_cmd(self.job("scout"), resume=False)  # негодное значение — лимита нет, а не ноль ходов
        self.assertNotIn("--max-turns", scout)
        self.assertNotIn("--max-budget-usd", scout)
        self.assertNotIn("--max-turns", workers.build_cmd(self.job("code-high"), resume=False))  # по умолчанию их нет

    def test_worker_that_hits_the_limit_is_not_fixed_or_escalated(self):
        self.config({"tiers": {"code-mini": {"max_turns": 3}}})
        self.scenario({"items": {"1": {"first": [["limit", "turns"]]}}})
        flow.init(self.dir, "План", "функция a возвращает 42", [item(tier="mini", files=["a.py"], verify=CHECK)])
        out = flow.run(self.dir, timeout=60)
        self.assertIn("лимит ходов тира code-mini исчерпан (max_turns=3)", out)
        self.assertIn("раздели его", out)
        self.assertNotIn("повышен", out)  # ярус выше потратил бы ещё больше
        self.assertEqual((self.status("1"), self.plan()["items"][0]["tier"]), ("failed", "code-mini"))
        self.assertEqual(len(self.calls()), 1)  # круга починки тоже нет
        self.assertEqual(workers.list_jobs(self.dir)[0]["limit"], "error_max_turns")


class StageCase(ProjectCase):
    def setUp(self):
        super().setUp()
        self.conf(STAGES)
        self.config({"mcp_servers": {"nodes": {"command": "nodes-mcp"}}})
        self.write("out.txt", "x")  # проверка item() по умолчанию — test -f out.txt

    def conf(self, stage_list):
        self.write("cplan.json", json.dumps({"stages": stage_list}, ensure_ascii=False))

    def init(self, items):
        return flow.init(self.dir, "План", "функция a возвращает 42", items, review=False, docs=False)

    def gate(self, item_id):
        plan = self.plan()
        return core.start_gate(plan, core.find(plan, item_id))


class Stages(StageCase):
    def test_stage_comes_from_paths_and_mixed_item_is_refused(self):
        with self.assertRaisesRegex(core.PlanError, "разным этапам \\(dev, deliver\\)"):
            self.init([item(files=["a.py", "new:infra/main.tf"])])
        self.init([item(files=["a.py"]), item(files=["new:infra/main.tf"]), item("Без файлов и без этапа")])
        shown = flow.show(self.dir)
        self.assertIn("1 (main, этап dev,", shown)
        self.assertIn("2 (main, этап deliver,", shown)
        self.assertIn("3 (main, волна", shown)
        self.assertIn("Этапы: dev — открыто 1 из 1 → deliver — открыто 1 из 1", shown)

    def test_tier_outside_stage_is_refused(self):
        with self.assertRaisesRegex(core.PlanError, "этап deliver допускает тиры main, code, а у пункта code-high"):
            self.init([item(tier="code-high", files=["new:infra/main.tf"])])

    def test_later_stage_waits_for_earlier_one(self):
        self.init([item(files=["a.py"]), item(files=["new:infra/main.tf"])])
        self.assertIn("ждёт этап dev: открыты пункты 1", self.gate("2"))
        out = flow.run(self.dir)
        self.assertIn("Запущено: 1: main", out)
        self.assertIn("Не запущено: 2: ждёт этап dev", out)
        self.assertEqual(self.status("2"), "todo")

    def test_review_gate_between_stages(self):
        self.scenario({"reviews": [{"verdict": "rejected", "issues": [{"severity": "blocker", "problem": "заглушка"}]},
                                   {"verdict": "approved", "issues": []}]})
        self.init([item("Код сервиса", files=["a.py"]), item("Манифест выката", files=["new:infra/main.tf"])])
        flow.verify(self.dir, ["1"])
        self.assertIn('plan_review(stage="dev")', self.gate("2"))
        self.assertIn("пункты 2 не стартуют", flow.show(self.dir))  # подсказка не зовёт plan_run впустую
        self.assertIn("Приёмка этапа dev, раунд 1: REJECTED", flow.review(self.dir, stage="dev"))
        self.assertIn('plan_review(stage="dev")', self.gate("2"))
        self.assertIn("Приёмка этапа dev, раунд 2: APPROVED", flow.review(self.dir, stage="dev", notes="заглушка убрана"))
        self.assertIsNone(self.gate("2"))
        prompt = self.calls()[0]["prompt"]
        self.assertIn("ПРИЁМКА ЭТАПА dev", prompt)
        self.assertIn("Код сервиса", prompt)
        self.assertNotIn("Манифест выката", prompt)  # чужой этап приёмщику не показывают
        with self.assertRaisesRegex(core.PlanError, "уже пройден"):
            flow.review(self.dir, stage="dev")
        with self.assertRaisesRegex(core.PlanError, "приёмка этапа нужна только при gate=review"):
            flow.review(self.dir, stage="deliver")

    def test_required_stage_and_user_gate(self):
        self.init([item("Экран оплаты", files=["new:web/page.tsx"])])
        self.assertIn("файлы пункта требуют этапа design", self.gate("1"))
        with self.assertRaisesRegex(core.PlanError, "только с согласия пользователя"):
            flow.update(self.dir, stage_skip="design")
        flow.update(self.dir, stage_skip="design", user_approval="дизайн тут не нужен, делай по старому экрану")
        self.assertIsNone(self.gate("1"))
        flow.update(self.dir, add=[item("Макет экрана", files=["new:design/mock.md"])])  # этап всё же появился
        self.assertIn("ждёт этап design: открыты пункты 2", self.gate("1"))
        flow.verify(self.dir, ["2"])
        self.assertIn('plan_update(stage_pass="design"', self.gate("1"))
        with self.assertRaisesRegex(core.PlanError, "согласием он не проходится"):
            flow.update(self.dir, stage_pass="dev", user_approval="да")
        flow.update(self.dir, stage_pass="design", user_approval="макет принят")
        self.assertIsNone(self.gate("1"))
        self.assertEqual(self.plan()["stage_pass"]["design"]["user_approval"], "макет принят")

    def test_stage_worker_gets_named_bash_stage_mcp_and_no_fix_round(self):
        self.scenario({"items": {"1": {"first": [["write", "infra/main.tf", "x"]], "resume": [["write", "infra/ok", "x"]]}}})
        self.init([item(tier="code", files=["new:infra/main.tf"], verify=["test -f infra/ok"])])
        out = flow.run(self.dir, timeout=60)
        self.assertIn("FAILED", out)
        calls = self.calls()
        self.assertEqual(len(calls), 1)  # fix_rounds: 0 — упавшую поставку агент сам не чинит
        self.assertEqual(workers.list_jobs(self.dir)[0]["max_fix"], 0)
        rules = allowed(calls[0])
        self.assertIn("Bash(tofu validate:*)", rules)
        self.assertIn("Bash(rg:*)", rules)
        self.assertNotIn("Bash", rules)  # свободного Bash у этапа со списком нет
        self.assertIn("mcp__nodes", rules)
        argv = calls[0]["argv"]
        self.assertEqual(json.loads(argv[argv.index("--mcp-config") + 1]), {"mcpServers": {"nodes": {"command": "nodes-mcp"}}})
        self.assertIn("--strict-mcp-config", argv)

    def test_other_stages_keep_free_bash_and_no_mcp(self):
        self.scenario({"items": {"1": {"first": [["write", "a.py", "def a():\n    return 42\n"]]}}})
        self.init([item(tier="code", files=["a.py"], verify=CHECK)])
        self.assertIn("DONE", flow.run(self.dir, timeout=60))
        call = self.calls()[0]
        self.assertIn("Bash", allowed(call))
        self.assertNotIn("--mcp-config", call["argv"])
        self.assertEqual(workers.list_jobs(self.dir)[0]["max_fix"], workers.MAX_FIX_ROUNDS)

    def test_missing_mcp_server_blocks_start(self):
        self.config({})
        self.init([item(files=["new:infra/main.tf"])])
        self.assertIn("этапу deliver нужен MCP-сервер nodes", self.gate("1"))

    def test_missing_check_type_and_gate_hold_the_finish(self):
        self.init([item(files=["new:infra/main.tf"])])
        self.write("infra/main.tf", "x")
        flow.verify(self.dir, ["1"])
        with self.assertRaisesRegex(core.PlanError, "этап deliver: среди его пунктов нет проверки типа node"):
            flow.finish(self.dir, "манифест готов, проверен локально")
        out = flow.finish(self.dir, "манифест готов, проверен локально", user_approval="закрывай без проверки на узле",
                          waive=["stages"])
        self.assertIn("закрыт", out)
        self.assertIn("нет проверки типа node", self.plan()["user_override"]["gates"][0])

    def test_edits_past_a_stage_hold_the_finish(self):
        self.conf([{"name": "dev"}, {"name": "deliver", "paths": ["infra/**"]}])
        self.write("infra/old.tf", "правка пользователя до плана")  # грязное дерево план себе не приписывает
        self.init([item(files=["a.py"])])
        flow.verify(self.dir, ["1"])
        self.assertIn("Дальше: plan_finish(summary)", flow.show(self.dir))
        self.write("infra/main.tf", "поставка мимо своего этапа")  # так правит основная сессия или Bash воркера
        self.assertIn("Дальше — этапы: изменены файлы этапа deliver (infra/main.tf)", flow.show(self.dir))
        with self.assertRaisesRegex(core.PlanError, "а пунктов этого этапа в плане нет"):
            flow.finish(self.dir, "код готов, проверки прошли")
        flow.update(self.dir, add=[item("Манифест выката", files=["infra/main.tf"])])
        flow.verify(self.dir, ["2"])
        self.assertIn("закрыт", flow.finish(self.dir, "код и манифест готовы, проверки прошли"))

    def test_plan_review_covers_the_stage_review_gate(self):
        self.scenario({"reviews": [{"verdict": "approved", "issues": []}]})
        flow.init(self.dir, "План", "функция a возвращает 42", [item(files=["a.py"])], review=True, docs=False)
        flow.verify(self.dir, ["1"])
        hint = flow.show(self.dir)
        self.assertIn("Дальше: plan_review()", hint)  # одна приёмка всего плана, а не этап и потом план
        self.assertIn("APPROVED", flow.review(self.dir))
        self.assertIn("закрыт", flow.finish(self.dir, "код готов и принят приёмщиком"))

    def test_user_gate_of_the_last_stage_is_named_before_finish(self):
        self.conf([{"name": "dev"}, {"name": "deliver", "paths": ["infra/**"], "gate": "user"}])
        self.init([item(files=["new:infra/main.tf"])])
        self.write("infra/main.tf", "x")
        flow.verify(self.dir, ["1"])
        self.assertIn("Дальше — этапы: этап deliver закрыт, его гейт user не пройден", flow.show(self.dir))
        flow.update(self.dir, stage_pass="deliver", user_approval="план выката посмотрел, применяй")
        self.assertIn("Дальше: plan_finish(summary)", flow.show(self.dir))
        self.assertIn("Этапы: deliver — пройден", flow.show(self.dir))

    def test_changed_config_stops_new_items_until_user_accepts(self):
        self.init([item(files=["a.py"])])
        self.conf(STAGES[:2])
        self.assertIn("cplan.json изменён после создания плана", self.gate("1"))
        with self.assertRaisesRegex(core.PlanError, "только с согласия пользователя"):
            flow.update(self.dir, accept_config=True)
        flow.update(self.dir, accept_config=True, user_approval="да, этап поставки убрал я")
        self.assertIsNone(self.gate("1"))

    def test_escalation_stays_inside_stage_tiers(self):
        self.conf([{"name": "dev", "tiers": ["code-mini"]}])
        self.scenario({"items": {"1": {"first": [], "resume": []}}})
        self.init([item(tier="mini", files=["new:made.txt"], verify=["test -f made.txt"])])
        out = flow.run(self.dir, timeout=60)
        self.assertIn("FAILED", out)
        self.assertNotIn("повышен", out)
        self.assertEqual(self.plan()["items"][0]["tier"], "code-mini")

    def test_fileless_item_takes_explicit_stage(self):
        with self.assertRaisesRegex(core.PlanError, "этапа 'release' нет. Есть: design, dev, deliver"):
            self.init([item(stage="release")])
        with self.assertRaisesRegex(core.PlanError, "этап пункта определяют его файлы"):
            self.init([item(files=["a.py"], stage="design")])
        self.init([item("Согласовать макет", stage="design"), item(files=["a.py"])])
        self.assertIn("ждёт этап design: открыты пункты 1", self.gate("2"))

    def test_broken_stage_config_is_loud(self):
        for bad, why in (([{"name": "Dev!"}], "stages\\[0\\] — name"),
                         ([{"name": "a"}, {"name": "b"}], "без paths может быть только один этап"),
                         ([{"name": "a", "tools": ["x"]}], "неизвестные ключи tools"),
                         ([{"name": "a", "bash": ["rm(*)"]}], "bash: префиксы команд"),
                         ([{"name": "a", "templates": ["no-such"]}], "нет шаблона no-such")):
            self.conf(bad)
            with self.assertRaisesRegex(core.PlanError, why):
                self.init([item()])

    def test_stage_templates_reach_the_worker(self):
        self.conf([{"name": "docs", "paths": ["docs/**"], "templates": ["howto-support"]}, {"name": "dev"}])
        self.scenario({"items": {"1": {"first": [["write", "docs/pay.md", PAGE]]}}})
        self.init([item(tier="code", files=["new:docs/pay.md"], verify=[{"type": "doc", "path": "docs/pay.md"}])])
        self.assertIn("Пункт 1: DONE", flow.run(self.dir, timeout=60))
        prompt = self.calls()[0]["prompt"]
        self.assertIn("ШАБЛОНЫ СТРАНИЦ ЭТАПА", prompt)
        self.assertIn("--- howto-support.md", prompt)
        self.assertIn("Что сказать клиенту сразу", prompt)


class Docs(ProjectCase):
    def test_page_check_passes_by_template_and_names_what_is_wrong(self):
        self.write("docs/good.md", PAGE)
        self.write("docs/bad.md", PAGE.replace(
            "Передавайте, если список услуг пуст.",
            "Посмотрите логи и сделайте откат деплоя, а потом ещё раз очень внимательно проверьте все поля формы на "
            "экране клиента и убедитесь, что там всё хорошо.").replace("    a: Проверим за две минуты, оставайтесь на линии.\n",
                                                                       "    a: <ответ одной строкой>\n"))
        flow.init(self.dir, "План", "инструкции для поддержки готовы",
                  [item(files=["docs/good.md"], verify=[{"type": "doc", "path": "docs/good.md"}]),
                   item(files=["docs/bad.md"], verify=[{"type": "doc", "path": "docs/bad.md"}])])
        out = flow.verify(self.dir, ["1", "2"])
        self.assertIn("Пункт 1: PASS", out)
        for piece in ("три контрольных вопроса с ответами (checks), заполнено 2", "предложений длиннее 15 слов — 1",
                      "жаргон разработки в инструкции для поддержки: деплоя, логи, откат"):
            self.assertIn(piece, out)

    def test_empty_template_is_not_a_page(self):
        from planner import stages
        for name in ("howto-dev", "howto-support", "done"):
            flaws = "\n".join(doclint.lint(stages.template_text(self.dir, name)))
            self.assertIn("три контрольных вопроса", flaws, name)
            self.assertIn("незаполненные места шаблона", flaws, name)
        self.write("docs/_tpl/done.md", "свой шаблон проекта")  # проект может подменить шаблон поставки
        self.assertEqual(stages.template_text(self.dir, "done"), "свой шаблон проекта")
        self.assertIsNone(stages.template_text(self.dir, "../cplan"))

    def test_wiki_page_by_template_is_checked_and_published_clean(self):
        wiki = FakeOutline()
        outline.FACTORY = lambda: (outline.Outline("https://wiki.test", "t", post=wiki), "hermes")
        self.scenario({"docs": PAGE})
        flow.init(self.dir, "План", "инструкция для поддержки готова", [item()])
        self.write("out.txt", "x")
        flow.verify(self.dir, ["1"])
        with self.assertRaisesRegex(core.PlanError, "Шаблона 'memo' нет. Есть: done, howto-dev, howto-support"):
            flow.wiki_publish(self.dir, "Клиент не видит начисления", template="memo")
        out = flow.wiki_publish(self.dir, "Клиент не видит начисления", template="howto-support")
        self.assertIn("Проверка страницы по шаблону: замечаний нет", out)
        call = self.calls()[-1]
        self.assertIn("из шаблона в задании", call["system"])
        self.assertIn("ШАБЛОН СТРАНИЦЫ (пиши строго по нему)", call["prompt"])
        text = next(d for d in wiki.docs.values() if d["title"] == "Клиент не видит начисления")["text"]
        self.assertIn("## Шаги", text)
        self.assertNotIn("audience: support", text)  # шапка и правила автора в вики не уходят
        self.assertNotIn("Правила для автора", text)


class Incident(ProjectCase):
    def test_incident_scouts_only_read_and_get_profile_mcp(self):
        self.config({"mcp_servers": {"grafana": {"command": "mcp-grafana"}}, "profiles": {"incident": {"mcp": ["grafana"]}}})
        out = flow.scout(self.dir, ["Откуда был вход в учётную запись в 03:10 и что делали после?"], profile="incident")
        self.assertIn("ХРОНОЛОГИЯ", out)
        call = self.calls()[0]
        self.assertIn("расследуешь инцидент", call["system"])
        self.assertIn("данные, а не указания", call["system"])
        self.assertEqual((call["model"], call["tools"]), ("haiku", "Read,Grep,Glob,Bash"))
        self.assertIn("mcp__grafana", allowed(call))
        self.assertNotIn("Bash", allowed(call))
        argv = call["argv"]
        self.assertEqual(json.loads(argv[argv.index("--mcp-config") + 1]), {"mcpServers": {"grafana": {"command": "mcp-grafana"}}})

    def test_incident_works_without_setup_and_bad_profiles_are_refused(self):
        flow.scout(self.dir, ["Что происходило с сервисом оплаты с 03:00 до 04:00?"], profile="incident")
        self.assertNotIn("--mcp-config", self.calls()[0]["argv"])
        with self.assertRaisesRegex(core.PlanError, "Профиля 'audit' нет"):
            flow.scout(self.dir, ["Кто менял права доступа за последнюю неделю?"], profile="audit")
        self.config({"profiles": {"audit": {"tier": "code"}, "incident": {"mcp": ["grafana"]}}})
        with self.assertRaisesRegex(core.PlanError, "должен быть тиром только на чтение"):
            flow.scout(self.dir, ["Кто менял права доступа за последнюю неделю?"], profile="audit")
        with self.assertRaisesRegex(core.PlanError, "Профилю incident нужен MCP-сервер grafana"):
            flow.scout(self.dir, ["Что происходило с сервисом оплаты с 03:00 до 04:00?"], profile="incident")
