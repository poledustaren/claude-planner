"""Этапы конвейера из <проект>/cplan.json.

Этап пункта определяется по его файлам, а не по метке модели. Этап задаёт допустимые тиры, инструменты воркера
(Bash и MCP), обязательные типы проверок, число кругов починки и гейт перед следующим этапом. Файл правит только
пользователь (хук и отпечаток в плане), поэтому ослабить себе ограничения модель не может.
"""
from __future__ import annotations

import fnmatch
import os
import re

from . import core

GATES = ("none", "review", "user")
CHECK_TYPES = ("cmd", "node", "http", "file_exists", "file_contains", "doc")
KEYS = ("name", "paths", "required_for", "tiers", "mcp", "bash", "verify", "fix_rounds", "gate", "templates")
TEMPLATE_CAP = 5000
_NAME = re.compile(r"[a-z][a-z0-9_-]{0,23}$")
_OWN_TEMPLATES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "docs")


def _bad(n: int, text: str) -> core.PlanError:
    return core.PlanError(f"{core.PROJECT_FILE}: stages[{n}] — {text}. Файл правит пользователь — скажи ему.")


def _strs(st: dict, key: str, n: int) -> list[str]:
    value = st.get(key)
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise _bad(n, f"{key}: нужен список непустых строк")
    return [v.strip() for v in value]


def template_text(root: str, name: str) -> str | None:
    """Шаблон страницы: сначала свой в проекте (docs/_tpl/<имя>.md), иначе из поставки планера."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", name or ""):
        return None
    for path in (os.path.join(root, "docs", "_tpl", name + ".md"), os.path.join(_OWN_TEMPLATES, name + ".md")):
        try:
            with open(path, encoding="utf-8") as fh:
                return fh.read()
        except OSError:
            continue
    return None


def template_names(root: str) -> list[str]:
    found = set()
    for folder in (os.path.join(root, "docs", "_tpl"), _OWN_TEMPLATES):
        try:
            found.update(f[:-3] for f in os.listdir(folder) if f.endswith(".md"))
        except OSError:
            pass
    return sorted(found)


def load(root: str) -> list[dict]:
    """Этапы в порядке конвейера. Нет ключа stages — пусто; ошибка в описании — PlanError для пользователя."""
    raw = core.project_conf(root).get("stages")
    if raw in (None, []):
        return []
    if not isinstance(raw, list):
        raise core.PlanError(f"{core.PROJECT_FILE}: stages — список этапов в порядке конвейера. Файл правит "
                             "пользователь — скажи ему.")
    out, seen, rest = [], set(), 0
    for n, st in enumerate(raw):
        if not isinstance(st, dict):
            raise _bad(n, "нужен объект")
        unknown = sorted(set(st) - set(KEYS))
        if unknown:
            raise _bad(n, f"неизвестные ключи {', '.join(unknown)} (допустимо: {', '.join(KEYS)})")
        name = st.get("name")
        if not isinstance(name, str) or not _NAME.match(name) or name in seen:
            raise _bad(n, "name: уникальное имя латиницей, например design, dev, deliver, docs")
        seen.add(name)
        paths = _strs(st, "paths", n)
        rest += not paths
        tiers = [core.TIER_ALIASES.get(t.lower(), t.lower()) for t in _strs(st, "tiers", n)]
        if any(t not in core.TIERS for t in tiers):
            raise _bad(n, f"tiers: допустимо {', '.join(core.TIERS)}")
        verify = _strs(st, "verify", n)
        if any(v not in CHECK_TYPES for v in verify):
            raise _bad(n, f"verify: типы проверок из {', '.join(CHECK_TYPES)}")
        gate = st.get("gate", "none")
        if gate not in GATES:
            raise _bad(n, f"gate: {', '.join(GATES)}")
        fix = st.get("fix_rounds")
        if fix is not None and (isinstance(fix, bool) or not isinstance(fix, int) or not 0 <= fix <= 3):
            raise _bad(n, "fix_rounds: целое от 0 до 3")
        bash = _strs(st, "bash", n) if "bash" in st and st["bash"] is not None else None
        if bash and any(re.search(r"[():*]", b) for b in bash):
            raise _bad(n, "bash: префиксы команд без скобок, двоеточий и звёздочек, например «tofu validate»")
        templates = _strs(st, "templates", n)
        missing = [t for t in templates if template_text(root, t) is None]
        if missing:
            raise _bad(n, f"templates: нет шаблона {', '.join(missing)} (есть: {', '.join(template_names(root))}; "
                          "свой кладётся в docs/_tpl/<имя>.md)")
        out.append({"name": name, "paths": paths, "required_for": _strs(st, "required_for", n), "tiers": tiers,
                    "mcp": _strs(st, "mcp", n), "bash": bash, "verify": verify, "fix_rounds": fix, "gate": gate,
                    "templates": templates})
    if rest > 1:
        raise core.PlanError(f"{core.PROJECT_FILE}: stages — без paths может быть только один этап, он забирает все "
                             "остальные файлы. Файл правит пользователь — скажи ему.")
    return out


def get(stages: list[dict], name: str | None) -> dict | None:
    return next((s for s in stages if s["name"] == name), None)


def _match(rel: str, patterns: list[str]) -> bool:
    """Путь подходит под маску fnmatch («*» захватывает и «/»); маска-каталог покрывает всё внутри."""
    rel = rel.rstrip("/") + ("/x" if rel.endswith("/") else "")
    return any(fnmatch.fnmatchcase(rel, p) or fnmatch.fnmatchcase(rel, p.rstrip("/") + "/*") for p in patterns)


def of_path(stages: list[dict], rel: str) -> str | None:
    for st in stages:
        if st["paths"] and _match(rel, st["paths"]):
            return st["name"]
    return next((st["name"] for st in stages if not st["paths"]), None)


def of_item(stages: list[dict], item: dict) -> str | None:
    """Этап пункта: по файлам; у пункта без файлов — явное поле stage. Файлы двух этапов — ошибка."""
    if not stages:
        return None
    files = core.file_paths(item)
    if not files:
        return item.get("stage") if get(stages, item.get("stage")) else None
    found = list(dict.fromkeys(of_path(stages, f) for f in files))
    if len(found) > 1:
        raise core.PlanError(f"Пункт {item.get('id', '?')}: файлы относятся к разным этапам "
                             f"({', '.join(x or 'вне этапов' for x in found)}). Раздели его на пункты по этапам: "
                             f"этап задаётся путями в {core.PROJECT_FILE}.")
    return found[0]


def check_item(root: str, item: dict, where: str) -> None:
    """Проверка пункта при создании и правке: этап существует, совпадает с путями, тир этапу разрешён."""
    stages = load(root)
    asked = item.get("stage")
    if not stages:
        if asked:
            raise core.PlanError(f"{where}: stage задан, а этапов в {core.PROJECT_FILE} нет.")
        return
    if asked and not get(stages, asked):
        raise core.PlanError(f"{where}: этапа {asked!r} нет. Есть: {', '.join(s['name'] for s in stages)}.")
    name = of_item(stages, item)
    if asked and core.file_paths(item) and asked != name:
        raise core.PlanError(f"{where}: этап пункта определяют его файлы — по путям это {name or 'вне этапов'}, а не "
                             f"{asked}. Убери stage или поправь files.")
    st = get(stages, name)
    if st and st["tiers"] and item["tier"] not in st["tiers"]:
        raise core.PlanError(f"{where}: этап {name} допускает тиры {', '.join(st['tiers'])}, а у пункта {item['tier']}.")


def tier_allowed(root: str, item: dict, tier: str) -> bool:
    try:
        stages = load(root)
        st = get(stages, of_item(stages, item))
    except core.PlanError:
        return False
    return not st or not st["tiers"] or tier in st["tiers"]


def for_item(root: str, item: dict) -> dict | None:
    stages = load(root)
    return get(stages, of_item(stages, item))


def max_fix(root: str, item: dict) -> int | None:
    try:
        st = for_item(root, item)
    except core.PlanError:
        return None
    return st["fix_rounds"] if st else None


def templates_for(root: str, item: dict) -> list[tuple[str, str]]:
    try:
        st = for_item(root, item)
    except core.PlanError:
        return []
    return [(name, (template_text(root, name) or "")[:TEMPLATE_CAP]) for name in (st["templates"] if st else [])]


def mcp_servers() -> dict:
    servers = core.config().get("mcp_servers")
    return servers if isinstance(servers, dict) else {}


# ───────────────────────── порядок этапов и гейты ─────────────────────────

def items_of(plan: dict, stages: list[dict], name: str) -> list[dict]:
    out = []
    for it in plan["items"]:
        try:
            if of_item(stages, it) == name:
                out.append(it)
        except core.PlanError:
            continue
    return out


def passed(plan: dict, stages: list[dict], st: dict) -> bool:
    """Гейт этапа пройден: его нет, прошла приёмка всего плана либо есть запись новее последнего закрытого пункта."""
    items = items_of(plan, stages, st["name"])
    if st["gate"] == "none" or not items:
        return True
    if st["gate"] == "review" and (core.review_approved(plan) or (
            core.needs_review(plan) and not any(i["status"] in core.OPEN for i in plan["items"]))):
        return True  # приёмка всего плана покрывает и этап: дважды одно и то же не принимаем
    rec = (plan.get("stage_pass") or {}).get(st["name"]) or {}
    return bool(rec) and rec.get("ts", "") >= max(i.get("closed_at") or "" for i in items)


def gate_hint(st: dict) -> str:
    if st["gate"] == "user":
        return (f"этап {st['name']} закрыт, его гейт user не пройден: покажи пользователю результат этапа и "
                f"передай его согласие — plan_update(stage_pass=\"{st['name']}\", user_approval=<цитата>)")
    return f"этап {st['name']} закрыт, его приёмка не пройдена — plan_review(stage=\"{st['name']}\")"


def incomplete(plan: dict, stages: list[dict], st: dict) -> str | None:
    """Почему этап ещё не завершён: открытые пункты, нет обязательного типа проверки или не пройден гейт."""
    items = items_of(plan, stages, st["name"])
    if not items:
        return None
    opened = [i["id"] for i in items if i["status"] not in core.CLOSED]
    if opened:
        return f"ждёт этап {st['name']}: открыты пункты {', '.join(opened)}"
    have = {c["type"] for i in items for c in i["verify"]}
    lack = [t for t in st["verify"] if t not in have]
    if lack:
        return (f"этап {st['name']}: среди его пунктов нет проверки типа {', '.join(lack)} — добавь пункт с такой "
                "проверкой plan_update(add=[...])")
    return None if passed(plan, stages, st) else gate_hint(st)


def start_block(plan: dict, item: dict) -> str | None:
    """Причина, по которой пункт не стартует из-за этапов, или None."""
    root = plan.get("root") or ""
    stages = load(root)
    if not stages and not plan.get("staged"):
        return None
    if core.config_changed(root, plan):
        return (f"{core.PROJECT_FILE} изменён после создания плана, а этапы задаёт пользователь: покажи ему разницу. "
                "Согласен — plan_update(accept_config=true, user_approval=<цитата>)")
    name = of_item(stages, item)
    order = [s["name"] for s in stages]
    files = core.file_paths(item)
    for st in stages:
        if st["name"] == name:
            continue
        earlier = name is not None and order.index(st["name"]) < order.index(name)
        required = bool(st["required_for"]) and any(_match(f, st["required_for"]) for f in files)
        if not earlier and not required:
            continue
        if not items_of(plan, stages, st["name"]):
            if not required or (plan.get("stage_skip") or {}).get(st["name"]):
                continue
            return (f"файлы пункта требуют этапа {st['name']}: добавь пункт этого этапа plan_update(add=[...]) или "
                    f"передай отказ пользователя — plan_update(stage_skip=\"{st['name']}\", user_approval=<цитата>)")
        why = incomplete(plan, stages, st)
        if why:
            return why
    st = get(stages, name)
    lost = [m for m in (st["mcp"] if st else []) if m not in mcp_servers()]
    if lost:
        return (f"этапу {name} нужен MCP-сервер {', '.join(lost)}, а в config.json → mcp_servers его нет: скажи "
                "пользователю")
    return None


def changed_files(root: str, base_rev: str) -> dict[str, str]:
    """Файлы, отличающиеся от base_rev, и новые вне git — с отпечатками содержимого."""
    if not base_rev:
        return {}
    names = core._git_out(root, "diff", "--name-only", base_rev).splitlines() \
        + core._git_out(root, "ls-files", "--others", "--exclude-standard").splitlines()
    return {rel: core.file_digest(os.path.join(root, rel)) for rel in dict.fromkeys(n.strip() for n in names)
            if rel and rel != core.PROJECT_FILE and not rel.startswith(core.DIR + "/")}


def snapshot(root: str, base_rev: str) -> dict[str, str]:
    """Что было изменено в рабочем дереве ещё до плана: эти правки план себе не приписывает."""
    try:
        return changed_files(root, base_rev) if load(root) else {}
    except core.PlanError:
        return {}


def strays(plan: dict, stages: list[dict]) -> list[str]:
    """Изменены файлы этапа с явными путями, а пунктов этого этапа в плане нет: работа прошла мимо его правил.

    Ловит правки основной сессии и запись воркера через Bash — у них нет ограничений этапа по инструментам."""
    root, known, found = plan.get("root") or "", plan.get("stage_dirty") or {}, {}
    for rel, digest in changed_files(root, plan.get("base_rev") or "").items():
        name = next((s["name"] for s in stages if s["paths"] and _match(rel, s["paths"])), None)
        if name and known.get(rel) != digest:
            found.setdefault(name, []).append(rel)
    return [f"изменены файлы этапа {name} ({', '.join(files[:4])}), а пунктов этого этапа в плане нет — заведи пункт "
            "plan_update(add=[...]) или верни файлы" for name, files in found.items() if not items_of(plan, stages, name)]


def pending(plan: dict) -> list[str]:
    """Что не закрыто по этапам к моменту закрытия плана."""
    stages = load(plan.get("root") or "")
    if not stages:
        return []
    return [why for st in stages if (why := incomplete(plan, stages, st))] + strays(plan, stages)


def summary(plan: dict) -> str:
    try:
        stages = load(plan.get("root") or "")
    except core.PlanError as exc:
        return f"Этапы: {exc}"
    parts = []
    for st in stages:
        items = items_of(plan, stages, st["name"])
        if not items:
            continue
        opened = sum(i["status"] not in core.CLOSED for i in items)
        state = f"открыто {opened} из {len(items)}" if opened else \
            ("пройден" if not incomplete(plan, stages, st) else f"ждёт гейт {st['gate']}" if st["gate"] != "none"
             else "не хватает проверок")
        parts.append(f"{st['name']} — {state}")
    return "Этапы: " + " → ".join(parts) if parts else ""
