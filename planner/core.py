"""claude-planner: модель плана, валидация, DAG, хранилище и детерминированные проверки.

План плоский (без вложенности): пункты связаны через depends_on. Состояние живёт в
<root>/.cplan/plan.json, поэтому переживает компактацию контекста и рестарт сессии.
Единственный путь пункта в done — verify_items(): реальные проверки с логом в evidence/.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import json
import os
import re
import signal
import subprocess
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

DIR = ".cplan"
CLOSED = ("done", "skipped")
OPEN = ("todo", "in_progress", "failed")
WORKER_TIERS = ("code-mini", "code-low", "code", "code-high")
ESCALATE = {"code-mini": "code-low"}  # младшая модель не прошла проверки — пункт сам уходит ярусом выше
TIERS = ("main",) + WORKER_TIERS
TIER_ALIASES = {"mini": "code-mini", "haiku": "code-mini", "low": "code-low", "mid": "code", "medium": "code", "high": "code-high",
                "sonnet": "code", "self": "main", "orchestrator": "main"}
REVIEW_MIN = 4          # план от стольких пунктов по умолчанию идёт через приёмку и в трекер
MAX_REVIEW_ROUNDS = 3   # дальше — только решение пользователя
MAX_ITEMS = 40
DEFAULT_TIMEOUT = 300
MAX_TIMEOUT = 3600
HTTP_TIMEOUT = 20
TAIL = 1500
LIGHT = {"debug": "отладка", "spike": "эксперимент", "nocode": "не код", "trivial": "мелкая правка",
         "followup": "доделка"}  # пункты без проверки: закрываются записью «что сделал», а не прогоном verify
BOX = {"done": "[x]", "todo": "[ ]", "in_progress": "[~]", "failed": "[!]", "skipped": "[-]"}
CODE_EXT = {".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".cs", ".go", ".rs", ".java", ".kt",
            ".gd", ".php", ".rb", ".c", ".cc", ".cpp", ".h", ".hpp", ".swift", ".vue", ".svelte",
            ".scala", ".dart", ".sh", ".sql"}
_TRIVIAL = re.compile(r"(?:true|:|exit(?:\s+0)?|echo(?:\s.*)?|/(?:usr/)?bin/true|ls(?:\s.*)?|pwd)", re.S)
_SWALLOW = re.compile(r"\|\|\s*(?:true|:|exit\s+0|echo\b[^|&;]*)\s*$")


class PlanError(Exception):
    """Ошибка, которую показываем модели как есть: что не так и как исправить."""


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds")  # микросекунды: порядок событий внутри секунды


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9а-яё]+", "-", text.lower()).strip("-")
    return slug[:48] or "plan"


# ───────────────────────── корень проекта и хранилище ─────────────────────────

def project_root(project_dir: str) -> str:
    if not isinstance(project_dir, str) or not project_dir.strip():
        raise PlanError("project_dir обязателен: абсолютный путь к папке проекта.")
    path = os.path.realpath(os.path.expanduser(project_dir.strip()))
    if not os.path.isdir(path):
        raise PlanError(f"Папки {path} нет. Укажи существующую папку проекта.")
    try:
        top = subprocess.run(["git", "-C", path, "rev-parse", "--show-toplevel"], capture_output=True,
                             text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        top = ""
    root = os.path.realpath(top) if top else path
    home = os.path.realpath(os.path.expanduser("~"))
    if root in ("/", "/tmp", home) or (root + os.sep).startswith(os.path.join(home, ".claude") + os.sep):
        raise PlanError(f"{root} — не проект (корень, /tmp, $HOME или конфиг агента). Укажи папку проекта.")
    return root


def pdir(root: str, *parts: str) -> str:
    return os.path.join(root, DIR, *parts)


def atomic_write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}.{id(text) & 0xffff}"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def _ensure_dir(root: str) -> None:
    os.makedirs(pdir(root), exist_ok=True)
    ignore = pdir(root, ".gitignore")
    if not os.path.exists(ignore):
        atomic_write(ignore, "*\n")  # каталог сам себя прячет от git, репозиторий не трогаем


@contextlib.contextmanager
def locked(root: str):
    _ensure_dir(root)
    with open(pdir(root, ".lock"), "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def load(root: str, required: bool = True) -> dict | None:
    try:
        with open(pdir(root, "plan.json"), encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        if required:
            raise PlanError(f"В {root} нет плана. Создай: plan_init(project_dir, title, goal, items).") from None
        return None
    except (OSError, ValueError) as exc:
        raise PlanError(f"План в {root} не читается: {exc}") from None


def save(root: str, plan: dict) -> None:
    refresh_status(plan)
    plan["updated_at"] = now()
    atomic_write(pdir(root, "plan.json"), json.dumps(plan, ensure_ascii=False, indent=1))
    atomic_write(pdir(root, "PLAN.md"), render(plan))


@contextlib.contextmanager
def mutate(root: str):
    with locked(root):
        plan = load(root)
        yield plan
        save(root, plan)


def event(plan: dict, name: str, item: str = "", note: str = "") -> None:
    hist = plan.setdefault("history", [])
    hist.append({"ts": now(), "event": name, **({"item": item} if item else {}), **({"note": note[:200]} if note else {})})
    del hist[:-120]


def find(plan: dict, item_id) -> dict:
    wanted = str(item_id).strip()
    for it in plan["items"]:
        if it["id"] == wanted:
            return it
    known = ", ".join(i["id"] for i in plan["items"]) or "нет"
    raise PlanError(f"Пункта {wanted!r} нет. Есть: {known}.")


# ───────────────────────── статус плана и приёмка ─────────────────────────

def needs_review(plan: dict) -> bool:
    flag = plan.get("review_mode", "auto")
    if flag in (True, False):
        return flag
    return len(heavy(plan)) >= REVIEW_MIN


def heavy(plan: dict) -> list:
    """Пункты с проверкой. План из одних light-пунктов — чеклист: без приёмки, вики и задачи в трекере."""
    return [i for i in plan["items"] if not i.get("light")]


def last_closed_at(plan: dict) -> str:
    return max((i.get("closed_at") or "" for i in plan["items"]), default="")


def review_rounds(plan: dict) -> list:
    return (plan.get("review") or {}).get("rounds") or []


def review_approved(plan: dict) -> bool:
    approved_at = (plan.get("review") or {}).get("approved_at") or ""
    return bool(approved_at) and approved_at >= last_closed_at(plan)


def review_stuck(plan: dict) -> bool:
    rejected = [r for r in review_rounds(plan) if r.get("verdict") == "rejected"]
    return len(rejected) >= MAX_REVIEW_ROUNDS and not review_approved(plan)


def docs_policy() -> str:
    """big — вики обязательна от 4 пунктов с проверкой (по умолчанию); always — для каждого плана; off — выключена.

    Режим конкретного плана перекрывает политику: docs=true в plan_init — меняется запуск, контракт или схема;
    docs=false — багфикс или рефакторинг без смены поведения."""
    try:
        with open(os.path.expanduser("~/.config/claude-planner/config.json"), encoding="utf-8") as fh:
            value = json.load(fh).get("docs")
    except (OSError, ValueError):
        value = None
    return value if value in ("always", "big", "off") else "big"


def needs_docs(plan: dict) -> bool:
    flag = (plan.get("docs") or {}).get("mode", "auto")
    if flag in (True, False):
        return flag
    if not heavy(plan):
        return False
    from . import outline  # вики не настроена — гейта нет, план не должен упираться в недоступный сервис
    if outline.connect() is None:
        return False
    policy = docs_policy()
    return policy == "always" or (policy == "big" and len(heavy(plan)) >= REVIEW_MIN)


def docs_published(plan: dict) -> list:
    return (plan.get("docs") or {}).get("published") or []


def docs_ok(plan: dict) -> bool:
    """Документация считается актуальной, если опубликована после закрытия последнего пункта."""
    return any(p.get("ts", "") >= last_closed_at(plan) for p in docs_published(plan))


def record_publication(root: str, entry: dict) -> None:
    with mutate(root) as plan:
        docs = plan.setdefault("docs", {"mode": "auto"})
        docs.setdefault("published", []).append({**entry, "ts": now()})
        event(plan, "wiki_publish", note=entry.get("title", ""))


def refresh_status(plan: dict) -> None:
    if plan.get("status") == "finished":
        return
    items = plan["items"]
    if not items or any(i["status"] in OPEN for i in items):
        plan["status"] = "active"
    elif needs_review(plan) and not review_approved(plan):
        plan["status"] = "review_pending"
    elif needs_docs(plan) and not docs_ok(plan):
        plan["status"] = "docs_pending"
    else:
        plan["status"] = "ready"


# ───────────────────────── валидация пунктов ─────────────────────────

def is_trivial_cmd(cmd: str) -> bool:
    parts = [p.strip() for p in re.split(r"&&|;|\n", cmd) if p.strip()]
    return not parts or all(_TRIVIAL.fullmatch(p) for p in parts) or bool(_SWALLOW.search(cmd.strip()))


def _int(value, default, where, name, lo=None, hi=None) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise PlanError(f"{where}: {name} должен быть целым числом.")
    if (lo is not None and value < lo) or (hi is not None and value > hi):
        raise PlanError(f"{where}: {name}={value} вне диапазона {lo}..{hi}.")
    return value


def normalize_check(c, where: str) -> dict:
    if isinstance(c, str) and c.strip():
        c = {"type": "cmd", "cmd": c.strip()}
    if not isinstance(c, dict):
        raise PlanError(f"{where}: проверка — строка-команда или объект с type.")
    kind = c.get("type")
    if not kind or kind in ("command", "bash", "shell"):
        kind = ("node" if "cmd" in c and "server" in c else "cmd" if "cmd" in c else "file_exists" if "paths" in c
                else "file_contains" if "path" in c and "pattern" in c else "http" if "url" in c else kind)
    contains = c.get("contains")
    if contains is not None and (not isinstance(contains, str) or not contains):
        raise PlanError(f"{where}: contains должен быть непустой строкой.")
    extra = {"contains": contains} if contains else {}
    if kind == "cmd":
        cmd = c.get("cmd")
        if not isinstance(cmd, str) or not cmd.strip():
            raise PlanError(f"{where}: у cmd-проверки нужен непустой cmd.")
        if is_trivial_cmd(cmd):
            raise PlanError(f"{where}: команда {cmd!r} ничего не доказывает (true, echo, ls, exit 0, «|| true»). "
                            "Нужна реальная проверка: тесты, сборка, rg -q по результату, curl -fsS.")
        return {"type": "cmd", "cmd": cmd.strip(),
                "timeout": _int(c.get("timeout"), DEFAULT_TIMEOUT, where, "timeout", 1, MAX_TIMEOUT),
                "expect_exit": _int(c.get("expect_exit"), 0, where, "expect_exit"), **extra}
    if kind == "node":
        server, cmd = c.get("server"), c.get("cmd")
        if not isinstance(server, str) or not server.strip() or not isinstance(cmd, str) or not cmd.strip():
            raise PlanError(f"{where}: у node-проверки нужны server (узел devopsmcp) и cmd.")
        return {"type": "node", "server": server.strip(), "cmd": cmd.strip(),
                "timeout": _int(c.get("timeout"), DEFAULT_TIMEOUT, where, "timeout", 1, MAX_TIMEOUT),
                "expect_exit": _int(c.get("expect_exit"), 0, where, "expect_exit"), **extra}
    if kind == "file_exists":
        paths = c.get("paths") or ([c["path"]] if isinstance(c.get("path"), str) else None)
        if not isinstance(paths, list) or not paths or not all(isinstance(p, str) and p for p in paths):
            raise PlanError(f"{where}: у file_exists нужен непустой список paths.")
        return {"type": "file_exists", "paths": paths}
    if kind == "file_contains":
        path, pattern = c.get("path"), c.get("pattern")
        if not isinstance(path, str) or not path or not isinstance(pattern, str) or not pattern:
            raise PlanError(f"{where}: у file_contains нужны path и pattern (regex).")
        try:
            re.compile(pattern)
        except re.error as exc:
            raise PlanError(f"{where}: битый regex {pattern!r}: {exc}.") from None
        return {"type": "file_contains", "path": path, "pattern": pattern}
    if kind == "http":
        url = c.get("url")
        if not isinstance(url, str) or not re.match(r"https?://", url):
            raise PlanError(f"{where}: у http-проверки нужен url с http:// или https://.")
        return {"type": "http", "url": url,
                "expect_status": _int(c.get("expect_status"), 200, where, "expect_status", 100, 599), **extra}
    raise PlanError(f"{where}: неизвестный type {kind!r}. Допустимо: cmd, node, file_exists, file_contains, http.")


def normalize_tier(value, where: str) -> str:
    if value in (None, ""):
        return "code"
    tier = TIER_ALIASES.get(str(value).strip().lower(), str(value).strip().lower())
    if tier not in TIERS:
        raise PlanError(f"{where}: tier {value!r} не существует. Допустимо: {', '.join(TIERS)} "
                        "(main — делает оркестратор; code-mini — воркер Haiku для механики; code-low/code/code-high — воркер "
                        "Sonnet с effort low/medium/high). "
                        "Разведка — не пункт плана, а plan_scout до планирования.")
    return tier


def normalize_files(raw, root: str, where: str) -> list[str]:
    if raw in (None, ""):
        return []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list) or not all(isinstance(f, str) and f.strip() for f in raw):
        raise PlanError(f"{where}: files — список путей от корня проекта (новый файл — с префиксом new:).")
    out, missing = [], []
    for f in raw:
        f = f.strip()
        new = f.startswith("new:")
        rel = f[4:].strip() if new else f
        if os.path.isabs(rel) and (rel + os.sep).startswith(root + os.sep):
            rel = os.path.relpath(rel, root)
        if not new and not os.path.exists(os.path.join(root, rel)):
            missing.append(rel)
        out.append(("new:" if new and not os.path.exists(os.path.join(root, rel)) else "") + rel)
    if missing:
        raise PlanError(f"{where}: файлов нет на диске: {', '.join(missing)}. Проверь пути (rg --files | rg <имя>) "
                        "или пометь создаваемые префиксом new:.")
    return list(dict.fromkeys(out))


def file_paths(item: dict) -> list[str]:
    return [f[4:] if f.startswith("new:") else f for f in item.get("files") or []]


def build_item(raw, item_id: str, root: str) -> dict:
    where = f"Пункт {item_id}"
    if not isinstance(raw, dict):
        raise PlanError(f"{where}: пункт должен быть объектом.")
    title = raw.get("title")
    if not isinstance(title, str) or len(title.strip()) < 3:
        raise PlanError(f"{where}: нужен title (что сделать, одной строкой).")
    where = f"Пункт {item_id} «{title.strip()[:40]}»"
    light = str(raw.get("light") or "").strip().lower() or None
    if light:
        light = {v: k for k, v in LIGHT.items()}.get(light, light)
        if light not in LIGHT:
            raise PlanError(f"{where}: light — один из " + ", ".join(f"{k} ({v})" for k, v in LIGHT.items()) + ".")
    acceptance = raw.get("acceptance") or (title if light else None)
    if not isinstance(acceptance, str) or len(acceptance.strip()) < (3 if light else 10):
        raise PlanError(f"{where}: нужен acceptance — наблюдаемый результат (≥ 10 символов), а не «сделано».")
    verify = raw.get("verify")
    if isinstance(verify, (str, dict)):
        verify = [verify]
    if light and verify:
        raise PlanError(f"{where}: пункт light закрывается записью, verify ему не нужен. Есть проверка — убери light.")
    if not light and (not isinstance(verify, list) or not verify):
        raise PlanError(f"{where}: нужен verify — хотя бы одна детерминированная проверка. Без проверки бывают "
                        f"только пункты light ({', '.join(LIGHT)}): отладка, эксперимент, не код, мелкая правка "
                        "в одном файле, доделка.")
    checks = [normalize_check(c, f"{where}, verify[{n}]") for n, c in enumerate(verify or [], 1)]
    tier = normalize_tier(raw.get("tier", raw.get("assignee")) or ("main" if light else None), where)
    if light and tier != "main":
        raise PlanError(f"{where}: пункт light делает основная сессия (tier main). Воркеру нужна проверка.")
    files = normalize_files(raw.get("files"), root, where)
    if tier in WORKER_TIERS and not files:
        raise PlanError(f"{where}: воркеру {tier} нужен files — какие файлы он правит (нужно для блокировки "
                        "пересечений между параллельными воркерами).")
    touches_code = any(os.path.splitext(p)[1].lower() in CODE_EXT for p in
                       (f[4:] if f.startswith("new:") else f for f in files))
    if touches_code and not light and not any(c["type"] in ("cmd", "http", "node") for c in checks):
        raise PlanError(f"{where}: пункт правит код, а проверки только смотрят, что файл есть. Добавь cmd "
                        "(тест, сборка, запуск) или http.")
    deps = raw.get("depends_on") or []
    if isinstance(deps, (str, int)):
        deps = [deps]
    if not isinstance(deps, list):
        raise PlanError(f"{where}: depends_on — список id пунктов.")
    context = raw.get("context", raw.get("description")) or ""
    if not isinstance(context, str):
        raise PlanError(f"{where}: context — строка (ориентиры file:L1-L2, символы, ограничения).")
    return {"id": item_id, "title": title.strip(), "acceptance": acceptance.strip(), "verify": checks,
            "tier": tier, "files": files, "depends_on": [str(d).strip() for d in deps],
            "context": context.strip(), "status": "todo", "evidence": [], **({"light": light} if light else {})}


def compute_waves(items: list) -> dict[str, int]:
    """Волны DAG: пункт идёт в волну max(волна зависимостей)+1. Цикл или чужой id — ошибка."""
    ids = {i["id"] for i in items}
    for it in items:
        bad = [d for d in it["depends_on"] if d not in ids or d == it["id"]]
        if bad:
            raise PlanError(f"Пункт {it['id']}: depends_on ссылается на {', '.join(bad)} — таких пунктов нет "
                            "(или пункт зависит сам от себя).")
    waves: dict[str, int] = {}
    pending = {i["id"]: set(i["depends_on"]) for i in items}
    while pending:
        ready = [i for i, deps in pending.items() if deps <= waves.keys()]
        if not ready:
            raise PlanError(f"Цикл в depends_on между пунктами: {', '.join(sorted(pending))}.")
        for i in ready:
            waves[i] = 1 + max((waves[d] for d in pending[i]), default=0)
            del pending[i]
    return waves


def _append_items(plan: dict, raw_items, root: str) -> list:
    if not isinstance(raw_items, list) or not raw_items:
        raise PlanError("items — непустой список пунктов.")
    if len(plan["items"]) + len(raw_items) > MAX_ITEMS:
        raise PlanError(f"В плане не больше {MAX_ITEMS} пунктов. Укрупни: пункт = один воркер, 1–5 файлов.")
    taken = {i["id"] for i in plan["items"]}
    nxt = max((int(i) for i in taken if i.isdigit()), default=0)
    built = []
    for raw in raw_items:
        given = str(raw.get("id") or "").strip() if isinstance(raw, dict) else ""
        if given and given in taken:
            raise PlanError(f"id {given!r} уже занят.")
        if not given:
            nxt += 1
            while str(nxt) in taken:
                nxt += 1
            given = str(nxt)
        taken.add(given)
        built.append(build_item(raw, given, root))
    plan["items"].extend(built)
    compute_waves(plan["items"])
    return built


def advice(plan: dict) -> list[str]:
    """Нежёсткие подсказки: что в плане выглядит дорого или хрупко."""
    notes = []
    items = plan["items"]
    mains = [i for i in items if i["tier"] == "main"]
    if len(items) >= REVIEW_MIN and len(mains) * 2 > len(items):
        notes.append(f"{len(mains)} из {len(items)} пунктов на main: независимые пункты дешевле отдать "
                     "code-low/code (Sonnet), себе оставь общие файлы и рискованное.")
    weak = [i["id"] for i in items if i["verify"] and all(c["type"] == "file_exists" for c in i["verify"])]
    if weak:
        notes.append(f"Пункты {', '.join(weak)} проверяются только наличием файла — это слабое доказательство.")
    todo = [i for i in items if i["status"] in OPEN]
    waves = compute_waves(items) if items else {}
    crew = [i for i in todo if i["tier"] in WORKER_TIERS]
    if len(crew) >= 3 and len({waves[i["id"]] for i in crew}) == len(crew):
        notes.append(f"Все {len(crew)} пунктов воркеров идут строго по очереди. depends_on нужен только там, где "
                     "пункт использует результат другого: независимые пункты с разными файлами стартуют параллельно.")
    owners: dict[tuple, list] = {}
    for i in todo:
        for path in file_paths(i):
            owners.setdefault((waves[i["id"]], path), []).append(i["id"])
    for (_, path), ids in sorted(owners.items()):
        if len(ids) > 1:
            notes.append(f"Файл {path} правят пункты {', '.join(ids)} одной волны — пойдут по очереди в произвольном "
                         "порядке. Раздели файлы (тогда параллельно) или задай порядок через depends_on.")
    from . import graph  # связи между файлами разных пунктов видны только по графу кода
    owner = {path: i["id"] for i in crew for path in file_paths(i)}
    seen = set()
    for path, users in graph.links(plan.get("root") or "", list(owner)).items():
        for user in users:
            a, b = owner[path], owner.get(user)
            if b and b != a and waves[a] == waves[b] and (a, b) not in seen and (b, a) not in seen:
                seen.add((a, b))
                notes.append(f"Пункты {a} и {b} одной волны связаны по графу кода: {user} использует {path}. Меняется "
                             "интерфейс — задай порядок через depends_on, иначе воркеры разойдутся.")
    return notes


# ───────────────────────── операции над планом ─────────────────────────

def git_branch(root: str) -> str | None:
    """Текущая ветка проекта; None — не git или HEAD не на ветке."""
    try:
        out = subprocess.run(["git", "-C", root, "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True,
                             timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return out if out and out != "HEAD" else None


def git_head(root: str) -> str:
    try:
        return subprocess.run(["git", "-C", root, "rev-parse", "HEAD"], capture_output=True, text=True,
                              timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def init(root: str, title: str, goal: str, items, review="auto", track="auto", refine: bool = False,
         docs="auto") -> dict:
    if not isinstance(title, str) or len(title.strip()) < 3:
        raise PlanError("title обязателен.")
    if not isinstance(goal, str) or len(goal.strip()) < 10:
        raise PlanError("goal обязателен: какой результат получит пользователь (≥ 10 символов).")
    for name, val in (("review", review), ("track", track), ("docs", docs)):
        if val not in ("auto", True, False):
            raise PlanError(f"{name}: допустимо \"auto\", true или false.")
    with locked(root):
        old = load(root, required=False)
        if old and refine:
            raise PlanError("refine больше не нужен: добавляй пункты через plan_update(add=[...]).")
        if old and old.get("status") != "finished":
            opened = sum(i["status"] in OPEN for i in old["items"])
            raise PlanError(f"В проекте уже есть незакрытый план «{old['title']}» ({opened} открытых пунктов). "
                            "Дополни или перестрой его plan_update(add / update / remove / title / goal) либо закрой "
                            "plan_finish. План устарел — спроси пользователя и закрой как есть: "
                            "plan_finish(summary, user_approval=<цитата>, force=true).")
        if old:
            stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            atomic_write(pdir(root, "archive", f"{stamp}-{slugify(old['title'])}.json"),
                         json.dumps(old, ensure_ascii=False, indent=1))
        plan = {"version": 1, "root": root, "project": os.path.basename(root), "title": title.strip(),
                "goal": goal.strip(), "status": "active", "created_at": now(), "base_rev": git_head(root),
                "branch": git_branch(root),
                "review_mode": review, "track": track, "docs": {"mode": docs, "published": []},
                "items": [], "history": []}
        _append_items(plan, items, root)
        event(plan, "init")
        save(root, plan)
    return plan


def approval_quote(user_approval, what: str) -> str:
    if not isinstance(user_approval, str) or len(user_approval.strip()) < 2:
        raise PlanError(f"{what} — только с согласия пользователя: нужен user_approval, дословная цитата его "
                        "ответа. Согласия нет — объяви паузу plan_update(hold=\"что ждём\") и спроси.")
    return user_approval.strip()[:300]


def update(root: str, add=None, changes=None, hold=None, title=None, goal=None, remove=None,
           user_approval=None, branch=None) -> dict:
    if not add and not changes and not remove and hold is None and title is None and goal is None and branch is None:
        raise PlanError("Нечего менять: передай add (новые пункты), update (правки пунктов), remove (удалить "
                        "пункты), title, goal или hold.")
    if title is not None and (not isinstance(title, str) or len(title.strip()) < 3):
        raise PlanError("title: новое название плана (≥ 3 символов).")
    if goal is not None and (not isinstance(goal, str) or len(goal.strip()) < 10):
        raise PlanError("goal: какой результат получит пользователь (≥ 10 символов).")
    if isinstance(remove, (str, int)):
        remove = [remove]
    approval = approval_quote(user_approval, "Удалить пункты из плана") if remove else ""
    with locked(root):
        plan = load(root)
        if plan.get("status") == "finished":
            plan["status"] = "active"
            plan.pop("finished_at", None)
            plan.pop("closed_as_is", None)
            event(plan, "reopen")
        if branch is not None:
            cur = git_branch(root)
            if not cur or str(branch).strip() != cur:
                raise PlanError(f"branch: план переносится только в текущую ветку проекта, а сейчас {cur or 'HEAD не на ветке'}. "
                                "Переключись на нужную ветку и повтори.")
            event(plan, "branch", note=f"{plan.get('branch')} → {cur}")
            plan["branch"] = cur
        if title is not None:
            plan["title"] = title.strip()
            event(plan, "retitle", note=plan["title"])
        if goal is not None:
            plan["goal"] = goal.strip()
            event(plan, "regoal", note=plan["goal"])
        for iid in dict.fromkeys(str(i).strip() for i in remove or []):
            it = find(plan, iid)
            if it["status"] == "in_progress" and it.get("job"):
                raise PlanError(f"Пункт {it['id']} сейчас у воркера. Останови: plan_worker(action=kill).")
            plan["items"].remove(it)
            for other in plan["items"]:
                other["depends_on"] = [d for d in other["depends_on"] if d != it["id"]]
            event(plan, "remove", it["id"], f"{it['title'][:80]} | согласие: {approval}")
        for ch in changes or []:
            if not isinstance(ch, dict) or not ch.get("id"):
                raise PlanError("update: каждый элемент — объект с id пункта и изменяемыми полями.")
            it = find(plan, ch["id"])
            if it["status"] in CLOSED and not ch.get("reopen"):
                raise PlanError(f"Пункт {it['id']} закрыт. Чтобы переделать — добавь reopen: true.")
            if it["status"] == "in_progress" and it.get("job"):
                raise PlanError(f"Пункт {it['id']} сейчас у воркера. Останови: plan_worker(action=kill|steer).")
            merged = {**{k: it[k] for k in ("title", "acceptance", "verify", "tier", "files", "depends_on", "context")},
                      "light": it.get("light"), **{k: v for k, v in ch.items() if k not in ("id", "reopen")}}
            if ch.get("verify") and "light" not in ch:
                merged["light"] = None  # пункту дали проверку — он больше не light
            fresh = build_item(merged, it["id"], root)
            it.update({k: fresh[k] for k in ("title", "acceptance", "verify", "tier", "files", "depends_on", "context")})
            it.pop("light", None)
            if fresh.get("light"):
                it["light"] = fresh["light"]
            if ch.get("reopen"):
                it["status"] = "todo"
                for key in ("closed_at", "manual", "skip"):
                    it.pop(key, None)
            event(plan, "update", it["id"])
        if add:
            for it in _append_items(plan, add, root):
                event(plan, "add", it["id"])
        compute_waves(plan["items"])
        if hold is not None:
            plan["hold"] = str(hold).strip()[:300] or None
            event(plan, "hold", note=plan["hold"] or "снят")
        save(root, plan)
    return plan


def ready_items(plan: dict) -> list:
    state = {i["id"]: i["status"] for i in plan["items"]}
    return [i for i in plan["items"] if i["status"] == "todo" and all(state[d] in CLOSED for d in i["depends_on"])]


def start_gate(plan: dict, item: dict) -> str | None:
    """None — можно стартовать; иначе причина отказа."""
    want, cur = plan.get("branch"), git_branch(plan.get("root") or "") if plan.get("branch") else None
    if want and cur and cur != want:
        return (f"план ведётся в ветке {want}, а сейчас {cur}: вернись (git switch {want}) или перенеси план — "
                f"plan_update(branch=\"{cur}\")")
    if item["status"] in CLOSED:
        return f"пункт уже {item['status']}"
    if item["status"] == "in_progress":
        return "пункт уже в работе"
    state = {i["id"]: i["status"] for i in plan["items"]}
    blocked = [d for d in item["depends_on"] if state[d] not in CLOSED]
    if blocked:
        return f"ждёт пункты {', '.join(blocked)}"
    mine = set(file_paths(item))
    for other in plan["items"]:
        if other["id"] != item["id"] and other["status"] == "in_progress":
            clash = mine & set(file_paths(other))
            if clash:
                return f"файл {sorted(clash)[0]} сейчас правит пункт {other['id']}"
    return None


def set_status(root: str, item_id: str, status: str, note: str = "", job: str | None = None) -> None:
    with mutate(root) as plan:
        it = find(plan, item_id)
        it["status"] = status
        if job is not None:
            it["job"] = job or None
        if note:
            it["note"] = note[:400]
        elif status == "in_progress":
            it.pop("note", None)
        plan["hold"] = None if status == "in_progress" else plan.get("hold")
        event(plan, status, item_id, note)


def skip(root: str, item_ids, reason: str, user_approval: str, outcome: str = "skipped") -> dict:
    """Закрыть пункты решением пользователя: skipped — не делаем; done — результат принят без проверки
    (проверка устарела, сделано иначе). Такие done помечены manual и в повторные прогоны не попадают."""
    if not isinstance(reason, str) or len(reason.strip()) < 5:
        raise PlanError("reason обязателен: почему пункт закрываем без проверки.")
    approval = approval_quote(user_approval, "Закрыть пункт без проверки")
    if outcome not in ("skipped", "done"):
        raise PlanError("outcome: \"skipped\" (пункт не делаем) или \"done\" (пользователь принял результат "
                        "без проверки).")
    ids = [item_ids] if isinstance(item_ids, (str, int)) else list(item_ids or [])
    if not ids:
        raise PlanError("Нужен item_id или item_ids.")
    with mutate(root) as plan:
        for iid in dict.fromkeys(str(i).strip() for i in ids):
            it = find(plan, iid)
            if it["status"] in CLOSED:
                raise PlanError(f"Пункт {it['id']} уже {it['status']}.")
            stamp = {"reason": reason.strip(), "user_approval": approval, "ts": now()}
            it.update(status=outcome, closed_at=now(), job=None, **{"manual" if outcome == "done" else "skip": stamp})
            it.pop("note", None)
            event(plan, "skip" if outcome == "skipped" else "accept", it["id"], reason)
    return plan


def escalate(root: str, item_id: str) -> str | None:
    """Поднять упавший пункт на ярус выше. Возвращает новый ярус или None, если выше некуда."""
    with mutate(root) as plan:
        it = find(plan, item_id)
        up = ESCALATE.get(it["tier"])
        if not up or it["status"] != "failed":
            return None
        it.update(escalated_from=it["tier"], tier=up)
        event(plan, "escalate", it["id"], f"{it['escalated_from']} → {up}")
    return up


def recheckable(plan: dict) -> list[str]:
    """Пункты для повторного прогона проверок: done, кроме принятых пользователем и пунктов light."""
    return [i["id"] for i in plan["items"] if i["status"] == "done" and not i.get("manual") and not i.get("light")]


# ───────────────────────── проверки ─────────────────────────

def fetch_http(url: str, timeout: int = HTTP_TIMEOUT) -> tuple[int, str]:
    req = urllib.request.Request(url, headers={"User-Agent": "claude-planner"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(1_000_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(1_000_000).decode("utf-8", "replace")


def run_check(root: str, c: dict) -> tuple[bool, str, str]:
    """Возвращает (прошла, краткая причина, полный вывод)."""
    kind = c["type"]
    if kind == "cmd":
        proc = subprocess.Popen(["bash", "-lc", c["cmd"]], cwd=root, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            raw, _ = proc.communicate(timeout=c["timeout"])
            code = proc.returncode
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            raw, _ = proc.communicate()
            code = None
        out = raw.decode("utf-8", "replace")
        if code is None:
            return False, f"таймаут {c['timeout']}s", out + f"\n[TIMEOUT {c['timeout']}s]"
        if code != c["expect_exit"]:
            return False, f"exit {code} вместо {c['expect_exit']}", out
        if "contains" in c and c["contains"] not in out:
            return False, f"в выводе нет {c['contains']!r}", out
        return True, f"exit {code}", out
    if kind == "node":
        from . import node  # узел опрашивается по сети, модуль нужен только этой проверке
        try:
            code, out = node.run(c["server"], c["cmd"], c["timeout"])
        except node.NodeError as exc:
            return False, str(exc), str(exc)
        if code is None:
            return False, f"узел {c['server']} не выполнил команду (профиль только для чтения)", out
        if code != c["expect_exit"]:
            return False, f"exit {code} вместо {c['expect_exit']}", out
        if "contains" in c and c["contains"] not in out:
            return False, f"в выводе нет {c['contains']!r}", out
        return True, f"{c['server']}: exit {code}", out
    if kind == "file_exists":
        missing = [p for p in c["paths"] if not os.path.exists(os.path.join(root, p))]
        detail = f"нет файлов: {', '.join(missing)}" if missing else "файлы на месте"
        return not missing, detail, detail
    if kind == "file_contains":
        try:
            with open(os.path.join(root, c["path"]), encoding="utf-8", errors="replace") as fh:
                match = re.search(c["pattern"], fh.read(), re.M)
        except OSError as exc:
            return False, f"не читается {c['path']}: {exc}", str(exc)
        detail = f"найдено {match.group(0)[:120]!r}" if match else f"в {c['path']} нет {c['pattern']!r}"
        return bool(match), detail, detail
    try:
        status, body = fetch_http(c["url"])
    except Exception as exc:  # noqa: BLE001 — сеть падает как угодно, для нас это просто FAIL
        return False, f"запрос не удался: {exc}", str(exc)
    ok = status == c["expect_status"] and ("contains" not in c or c["contains"] in body)
    return ok, f"HTTP {status}" + ("" if ok else f", ждали {c['expect_status']}"
                                    + (f" и {c['contains']!r}" if "contains" in c else "")), body


def check_label(c: dict) -> str:
    if c.get("type") == "node":
        return f"{c['server']}: {c['cmd']}"[:160]
    return str(c.get("cmd") or c.get("url") or c.get("path") or ", ".join(c.get("paths", [])))[:160]


def _run_checks(root: str, checks: list, cache: dict | None = None) -> tuple[bool, list, str]:
    results, log = [], []
    for c in checks:
        key = json.dumps(c, sort_keys=True, ensure_ascii=False)
        if cache is not None and key in cache:
            ok, detail, out = cache[key]
        else:
            ok, detail, out = run_check(root, c)
            if cache is not None:
                cache[key] = (ok, detail, out)
        res = {"type": c["type"], "what": check_label(c), "passed": ok, "detail": detail}
        if not ok:
            res["tail"] = out[-TAIL:]
        results.append(res)
        log.append(f"=== {key}\n{out}\n=== {'PASS' if ok else 'FAIL'}: {detail}\n")
    return all(r["passed"] for r in results), results, "\n".join(log)


def verify_items(root: str, item_ids: list, by: str = "main", recheck: bool = False,
                 note: str | None = None) -> list[tuple[dict, dict]]:
    """Запускает проверки пунктов (параллельно между пунктами) и фиксирует evidence.

    Единственный путь в done. recheck=True — повторный прогон закрытых пунктов (регрессия).
    Пункты light проверок не имеют: закрываются записью note — что сделал и чем убедился.
    Возвращает [(пункт, запись evidence)].
    """
    with locked(root):
        plan = load(root)
        targets, light = [], []
        for iid in dict.fromkeys(str(i) for i in item_ids):
            it = find(plan, iid)
            if it.get("light") and recheck:
                continue
            if it["status"] == "skipped" or (it["status"] == "done" and not recheck):
                raise PlanError(f"Пункт {it['id']} уже {it['status']}; повторная проверка — plan_finish/plan_review.")
            if it.get("light"):
                light.append(it["id"])
            else:
                targets.append((it["id"], list(it["verify"]), len(it["evidence"]) + 1))
        if light and (not isinstance(note, str) or len(note.strip()) < 10):
            raise PlanError(f"Пункты без проверки ({', '.join(light)}) закрываются записью: plan_verify(item_ids=[...], "
                            "note=\"что сделал и чем убедился\") — не короче 10 символов.")
    cache: dict = {} if recheck else None  # одинаковые проверки разных пунктов гоняем один раз

    def one(target):
        iid, checks, n = target
        ok, results, log = _run_checks(root, checks, cache)
        rel = os.path.join(DIR, "evidence", f"{iid}-{n}.log")
        atomic_write(os.path.join(root, rel), log)
        return iid, {"ts": now(), "passed": ok, "by": by, "log": rel, "checks": results}

    workers = 1 if recheck else min(4, len(targets) or 1)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        done = list(pool.map(one, targets))
    out = []
    with mutate(root) as plan:
        for iid in light:
            it = find(plan, iid)
            ev = {"ts": now(), "passed": True, "by": by, "log": None, "checks": [], "note": note.strip()[:500]}
            it["evidence"].append(ev)
            it.update(status="done", closed_at=now(), job=None, done_note=ev["note"])
            it.pop("note", None)
            event(plan, "done_light", iid, ev["note"][:120])
            out.append((dict(it), ev))
        for iid, ev in done:
            it = find(plan, iid)
            it["evidence"].append(ev)
            del it["evidence"][:-6]
            was = it["status"]
            it["status"] = "done" if ev["passed"] else "failed"
            if ev["passed"]:
                if was != "done":
                    it["closed_at"] = now()
                it.pop("note", None)
                it.pop("manual", None)
            else:
                it.pop("closed_at", None)
            it["job"] = None if by == "main" else it.get("job")
            event(plan, "verify_ok" if ev["passed"] else "verify_fail", iid, "" if ev["passed"] else fail_summary(ev))
            out.append((dict(it), ev))
    return out


def fail_summary(ev: dict) -> str:
    return "; ".join(f"{c['what'][:70]} → {c['detail']}" for c in ev["checks"] if not c["passed"])


def fail_report(item: dict, ev: dict, tail: int = TAIL) -> str:
    lines = [f"Пункт {item['id']} «{item['title']}» — проверки не прошли:"]
    for c in ev["checks"]:
        if not c["passed"]:
            lines.append(f"- {c['type']}: {c['what']} → {c['detail']}")
            if c.get("tail", "").strip():
                lines.append("```\n" + c["tail"][-tail:].strip() + "\n```")
    return "\n".join(lines)


def finish_blockers(plan: dict) -> list[str]:
    out = []
    opened = [i for i in plan["items"] if i["status"] in OPEN]
    if not plan["items"]:
        out.append("в плане нет пунктов")
    if opened:
        out.append("открыты пункты: " + ", ".join(f"{i['id']} [{i['status']}]" for i in opened))
    elif needs_review(plan) and not review_approved(plan):
        out.append("не пройдена приёмка" + (" (лимит раундов исчерпан — нужно решение пользователя)"
                                               if review_stuck(plan) else ": вызови plan_review"))
    elif needs_docs(plan) and not docs_ok(plan):
        out.append("документация не опубликована после последних изменений: wiki_publish(title, notes) — страницу напишет воркер")
    return out


USER_WAY_OUT = ("Обойти можно только решением пользователя: спроси его (пауза plan_update(hold=...)) и передай "
                "дословный ответ — plan_finish(summary, user_approval=<цитата>) закрывает без приёмки и вики, "
                "с force=true — как есть: открытые пункты уйдут в skipped, проверки не гоняются.")


def finish(root: str, summary: str, user_approval: str | None = None, force: bool = False) -> dict:
    if not isinstance(summary, str) or len(summary.strip()) < 10:
        raise PlanError("summary обязателен: что сделано и чем подтверждено (2–5 строк).")
    plan = load(root)
    if plan.get("status") == "finished":
        raise PlanError("План уже закрыт.")
    if force:
        approval = approval_quote(user_approval, "Закрыть план как есть (force)")
        with mutate(root) as plan:
            for it in plan["items"]:
                if it["status"] in OPEN:
                    it.update(status="skipped", closed_at=now(), job=None,
                              skip={"reason": "план закрыт как есть", "user_approval": approval, "ts": now()})
                    it.pop("note", None)
                    event(plan, "skip", it["id"], "план закрыт как есть")
            plan.update(summary=summary.strip(), finished_at=now(), status="finished", hold=None,
                        closed_as_is=approval)
            event(plan, "finish_forced", note=approval)
        return plan
    approval = user_approval.strip()[:300] if isinstance(user_approval, str) else ""
    blockers = finish_blockers(plan)
    has_open = not plan["items"] or any(i["status"] in OPEN for i in plan["items"])
    override = bool(approval) and not has_open  # согласие снимает гейты приёмки и вики, но не открытые пункты
    if blockers and not override:
        raise PlanError("Закрыть нельзя: " + "; ".join(blockers) + ". " + USER_WAY_OUT)
    closed = recheckable(plan)
    regress = [(it, ev) for it, ev in verify_items(root, closed, by="finish", recheck=True) if not ev["passed"]] \
        if closed else []
    if regress:
        raise PlanError("Финальный прогон проверок нашёл регрессию, пункты вернулись в failed:\n"
                        + "\n".join(fail_report(it, ev, 600) for it, ev in regress)
                        + "\nПроверка устарела — поправь её plan_update(update=[{id, verify}]) → plan_verify. "
                        "Чинить не будем — решает пользователь: plan_skip(outcome=\"done\"|\"skipped\") или "
                        "plan_finish(force=true), оба с его цитатой в user_approval.")
    with mutate(root) as plan:
        if override and blockers:
            plan["user_override"] = {"gates": blockers, "user_approval": approval, "ts": now()}
            if needs_review(plan) and not review_approved(plan):
                plan.setdefault("review", {})["user_override"] = approval
                plan["review"]["approved_at"] = now()
        plan["summary"] = summary.strip()
        plan["finished_at"] = now()
        plan["status"] = "finished"
        plan["hold"] = None
        event(plan, "finish")
    return plan


# ───────────────────────── представление ─────────────────────────

def counts(plan: dict) -> str:
    c: dict[str, int] = {}
    for i in plan["items"]:
        c[i["status"]] = c.get(i["status"], 0) + 1
    return ", ".join(f"{k} {v}" for k, v in c.items()) or "пунктов нет"


def status_line(plan: dict) -> str:
    extra = f" | приёмка: {'пройдена' if review_approved(plan) else 'нужна'}" if needs_review(plan) else ""
    if needs_docs(plan):
        extra += f" | вики: {'опубликована' if docs_ok(plan) else 'нужна'}"
    if plan.get("branch"):
        extra += f" | ветка: {plan['branch']}"
    hold = f" | HOLD: {plan['hold']}" if plan.get("hold") else ""
    key = (plan.get("plane") or {}).get("issue_key")
    return f"«{plan['title']}» — {plan['status']} ({counts(plan)}){extra}{hold}" + (f" | {key}" if key else "")


def render(plan: dict) -> str:
    waves = compute_waves(plan["items"]) if plan["items"] else {}
    lines = [f"# {plan['title']}", "", f"Цель: {plan['goal']}", f"Статус: {status_line(plan)}", ""]
    for it in plan["items"]:
        deps = f" ← {','.join(it['depends_on'])}" if it["depends_on"] else ""
        tail = ""
        if it["status"] == "failed" and it["evidence"] and not it["evidence"][-1]["passed"]:
            tail = " — " + fail_summary(it["evidence"][-1])[:160]
        elif it.get("note"):
            tail = " — " + it["note"][:160]
        elif it.get("light") and it["status"] == "done":
            tail = " — " + str(it.get("done_note") or "")[:160]
        elif it.get("manual") or it.get("skip"):
            stamp = it.get("manual") or it["skip"]
            tail = f" — {'принят' if it.get('manual') else 'пропущен'} решением пользователя: {stamp['reason'][:120]}"
        kind = f", без проверки: {LIGHT[it['light']]}" if it.get("light") else ""
        lines.append(f"- {BOX[it['status']]} {it['id']} ({it['tier']}{kind}, волна {waves.get(it['id'], '?')}{deps}) "
                     f"{it['title']}{tail}")
    rounds = review_rounds(plan)
    if rounds:
        last = rounds[-1]
        lines += ["", f"Приёмка: раунд {last['n']} — {last['verdict']}"
                      + (f", блокеров {sum(1 for i in last.get('issues', []) if i.get('severity') == 'blocker')}"
                         if last["verdict"] == "rejected" else "")]
    for pub in docs_published(plan)[-3:]:
        lines += [f"Документация: {pub.get('title')} — {pub.get('url')}"]
    if plan.get("summary"):
        how = " (закрыт как есть решением пользователя)" if plan.get("closed_as_is") else ""
        lines += ["", f"Итог{how}: " + plan["summary"]]
    return "\n".join(lines) + "\n"


def item_detail(plan: dict, item_id) -> str:
    it = find(plan, item_id)
    lines = [f"{BOX[it['status']]} {it['id']} ({it['tier']}) {it['title']}", f"Приёмка: {it['acceptance']}",
             "Файлы: " + (", ".join(it["files"]) or "—"),
             (f"Без проверки: {LIGHT[it['light']]} — закрывается записью plan_verify(note=...)" if it.get("light") else
              "Проверки: " + "; ".join(f"{c['type']}: {check_label(c)}" for c in it["verify"]))]
    if it.get("context"):
        lines.append("Контекст: " + it["context"][:1200])
    if it.get("note"):
        lines.append("Заметка: " + it["note"])
    for ev in it["evidence"][-3:]:
        lines.append(f"- {ev['ts']} {'PASS' if ev['passed'] else 'FAIL'} ({ev['by']}) "
                     + (f"лог {ev['log']}" if ev.get("log") else "без проверки: " + str(ev.get("note") or ""))
                     + ("" if ev["passed"] else " — " + fail_summary(ev)))
    return "\n".join(lines)
