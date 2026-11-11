"""Синхронизация с трекером Plane: задача на каждый план и паспорт проекта в Pages.

Схема та же, что у agy-planner: проект ищется или создаётся, в нём — страница «Паспорт и
документация проекта» (назначение, стек, пути, правила деплоя, ссылки на Outline) и задача
плана с чек-листом. Паспорт agy-planner (.planner/project_meta.json) подхватывается, чтобы
оба планера вели одну страницу и один проект.
Любой сбой Plane не ломает работу планера: возвращается предупреждение строкой.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import subprocess
import urllib.error
import urllib.request

from . import core

CONFIG_FILES = ("~/.config/claude-planner/plane.env", "~/.config/agy-planner/plane.env")
KEYS = ("PLANE_BASE_URL", "PLANE_API_KEY", "PLANE_WORKSPACE_SLUG")
PAGE_TITLE_PREFIX = "Паспорт и документация проекта"
REVIEW_STATE_NAMES = ("в проверке", "in review", "review", "на проверке")
BOXES = {"done": "☑️", "todo": "⬜", "in_progress": "🔄", "failed": "❌", "skipped": "⏭️"}
PASSPORT_KEYS = ("stack", "features", "deploy_rules", "links")
FACTORY = None  # тесты подменяют на фабрику клиента с фейковым транспортом


class PlaneError(Exception):
    pass


def load_config() -> dict:
    cfg = {k: os.environ.get(k, "") for k in KEYS}
    for path in CONFIG_FILES:
        try:
            with open(os.path.expanduser(path), encoding="utf-8") as fh:
                for line in fh:
                    key, sep, val = line.strip().removeprefix("export ").partition("=")
                    if sep and key.strip() in KEYS and not cfg[key.strip()]:
                        cfg[key.strip()] = val.strip().strip("'\"")
        except OSError:
            continue
    return cfg


class Client:
    def __init__(self, base_url: str, api_key: str, workspace: str, transport=None, timeout: int = 12):
        self.base_url, self.api_key, self.workspace, self.timeout = base_url.rstrip("/"), api_key, workspace, timeout
        self.transport = transport or self._http
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _http(self, method: str, path: str, payload: dict | None = None):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"X-API-Key": self.api_key, "Accept": "application/json", "User-Agent": "claude-planner/1.0"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base_url + path, data=data, method=method, headers=headers)
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
                return json.loads(raw) if raw.strip() else None
        except urllib.error.HTTPError as exc:
            raise PlaneError(f"Plane {method} {path}: HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:200]}") from None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise PlaneError(f"Plane {method} {path}: {exc}") from None

    def call(self, method: str, path: str, payload: dict | None = None):
        return self.transport(method, path, payload)

    def ws(self, tail: str) -> str:
        return f"/api/v1/workspaces/{self.workspace}/{tail}"

    @staticmethod
    def results(resp) -> list:
        return resp if isinstance(resp, list) else (resp or {}).get("results") or []


def client() -> Client | None:
    if FACTORY is not None:
        return FACTORY()
    cfg = load_config()
    if not all(cfg[k] for k in KEYS):
        return None
    return Client(cfg["PLANE_BASE_URL"], cfg["PLANE_API_KEY"], cfg["PLANE_WORKSPACE_SLUG"])


def tracked(plan: dict) -> bool:
    return plan.get("track", "auto") is not False


# ───────────────────────── паспорт проекта ─────────────────────────

def _meta_path(root: str) -> str:
    return core.pdir(root, "meta.json")


def load_meta(root: str) -> dict:
    for path in (_meta_path(root), os.path.join(root, ".planner", "project_meta.json")):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                data.pop("plane_page_html_sha1", None) if path != _meta_path(root) else None
                return data
        except (OSError, ValueError):
            continue
    return {}


def save_meta(root: str, meta: dict) -> None:
    core.atomic_write(_meta_path(root), json.dumps(meta, ensure_ascii=False, indent=1))


def detect_stack(root: str) -> tuple[str, str, str]:
    """(стек, правила деплоя, git remote) по файлам в корне проекта."""
    def has(*names):
        return any(os.path.exists(os.path.join(root, n)) for n in names)
    stack, deploy = [], []
    if has("pyproject.toml", "requirements.txt", "setup.py"):
        stack.append("Python")
    if has("package.json"):
        stack.append("Node.js / TypeScript")
    if has("Cargo.toml"):
        stack.append("Rust")
    if has("go.mod"):
        stack.append("Go")
    try:
        entries = os.listdir(root)
    except OSError:
        entries = []
    if any(e.endswith((".csproj", ".sln")) for e in entries):
        stack.append(".NET / C#")
    if has("docker-compose.yml", "docker-compose.yaml", "compose.yaml"):
        stack.append("Docker Compose")
        deploy.append("Docker Compose (`docker compose up -d`)")
    elif has("Dockerfile"):
        stack.append("Docker")
    if has(".gitlab-ci.yml"):
        deploy.append("GitLab CI (`.gitlab-ci.yml`)")
    if has("setup.sh", "install.sh", "install.py"):
        deploy.append("Установщик в корне проекта")
    try:
        remote = subprocess.run(["git", "-C", root, "remote", "get-url", "origin"], capture_output=True, text=True,
                                timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        remote = ""
    return (", ".join(stack) or "Локальный проект / скрипты",
            "; ".join(deploy) or "Проверка детерминированными verify-командами планера", remote)


def enrich_meta(root: str, plan: dict | None = None, extra: dict | None = None) -> dict:
    """Собирает паспорт: явные поля из extra перекрывают сохранённые, те — автоопределение."""
    meta = load_meta(root)
    extra = {k: str(v).strip() for k, v in (extra or {}).items() if k in PASSPORT_KEYS and v}
    meta.update(project=os.path.basename(root), root=root)
    if not (meta.get("stack") and meta.get("deploy_rules") and meta.get("git_remote_checked")):
        stack, deploy, remote = detect_stack(root)
        meta.setdefault("stack", stack)
        meta.setdefault("deploy_rules", deploy)
        if remote:
            meta["git_remote"] = remote
        meta["git_remote_checked"] = True
    meta.update(extra)
    meta["features"] = meta.get("features") or (plan or {}).get("goal") or "—"
    save_meta(root, meta)
    return meta


def all_docs(plan: dict | None, meta: dict) -> list:
    docs, seen = [], set()
    for d in [*core.docs_published(plan or {"docs": {}}), *(meta.get("outline_docs") or [])]:
        if d.get("url") and d["url"] not in seen:
            seen.add(d["url"])
            docs.append({k: d.get(k) for k in ("title", "url", "mode")})
    return docs


def page_html(root: str, plan: dict | None, meta: dict) -> str:
    esc = html.escape
    name = os.path.basename(root)
    parts = [f"<h2>Паспорт проекта «{esc(name)}»</h2>", "<ul>",
             f"<li><strong>Особенности и назначение:</strong> {esc(str(meta.get('features') or '—'))}</li>",
             f"<li><strong>Стек технологий:</strong> {esc(str(meta.get('stack') or '—'))}</li>",
             f"<li><strong>Локальный путь:</strong> <code>{esc(root)}</code></li>"]
    if meta.get("git_remote"):
        parts.append(f"<li><strong>Git-репозиторий:</strong> <code>{esc(meta['git_remote'])}</code></li>")
    if meta.get("links"):
        parts.append(f"<li><strong>Ссылки:</strong> {esc(str(meta['links']))}</li>")
    parts += [f"<li><strong>Правила деплоя и проверки:</strong> {esc(str(meta.get('deploy_rules') or '—'))}</li>", "</ul>",
              "<h3>Документация в Outline Wiki</h3>"]
    docs = all_docs(plan, meta)
    if docs:
        parts.append("<ul>" + "".join(
            f'<li><a href="{esc(d["url"])}" target="_blank" rel="noopener noreferrer">{esc(d.get("title") or d["url"])}</a></li>'
            for d in docs) + "</ul>")
    else:
        parts.append(f"<p>Раздел в Outline Wiki: <strong>Проект · {esc(name)}</strong> — публикуется через "
                     "<code>wiki_publish</code>.</p>")
    if plan:
        parts.append(f"<h3>Текущий / последний план: {esc(plan['title'])}</h3>"
                     f"<p><strong>Цель:</strong> {esc(plan['goal'])} · <strong>Статус:</strong> "
                     f"<code>{esc(plan['status'])}</code></p>")
    return "\n".join(parts)


def sync_page(cl: Client, project_id: str, root: str, plan: dict | None, meta: dict) -> str:
    """Создаёт или обновляет страницу-паспорт. Возвращает id страницы."""
    title = f"{PAGE_TITLE_PREFIX} · {os.path.basename(root)}"
    body = page_html(root, plan, meta)
    digest = hashlib.sha1(f"{title}\n{body}".encode("utf-8")).hexdigest()
    saved = meta.get("plane_page_id")
    if saved and meta.get("plane_page_html_sha1") == digest:
        return str(saved)
    payload, page = {"name": title, "description_html": body}, None
    base = cl.ws(f"projects/{project_id}/pages/")
    if saved:
        try:
            page = cl.call("PATCH", f"{base}{saved}/", payload)
        except PlaneError:
            page = None
    if not (page or {}).get("id"):
        pages = cl.results(cl.call("GET", base))
        target = next((p for p in pages if p.get("name") == title), None) or next(
            (p for p in pages if (p.get("name") or "").strip().startswith(PAGE_TITLE_PREFIX)), None)
        page = (cl.call("PATCH", f"{base}{target['id']}/", payload) if target
                else cl.call("POST", base, {**payload, "access": 0})) or {}
    if not page.get("id"):
        raise PlaneError("Plane не вернул id страницы-паспорта (включён ли Pages API?)")
    meta["plane_page_id"], meta["plane_page_html_sha1"] = str(page["id"]), digest
    return meta["plane_page_id"]


# ───────────────────────── проект, статусы, задача ─────────────────────────

def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9а-яё]", "", (text or "").casefold())


def ensure_project(cl: Client, root: str, meta: dict) -> tuple[str, str]:
    if meta.get("plane_project_id"):
        return str(meta["plane_project_id"]), str(meta.get("plane_project_identifier") or "PROJ")
    name = os.path.basename(root)
    projects = cl.results(cl.call("GET", cl.ws("projects/")))
    found = next((p for p in projects if _norm(name) in (_norm(p.get("name")), _norm(p.get("identifier")))), None)
    if found is None:
        taken = {str(p.get("identifier") or "").upper() for p in projects}
        base = (re.sub(r"[^A-Za-z0-9]", "", name).upper() or "PROJ")[:9]
        ident, n = base if len(base) >= 3 else "PROJ" + base, 1
        while ident in taken:
            ident, n = base[:8] + str(n), n + 1
        clean = re.sub(r"\s+", " ", re.sub(r"[&+,:;$^}{*=?@#|'<>.()%!-]+", " ", name)).strip() or "project"
        desc = f"{meta.get('features') or name} | Стек: {meta.get('stack')} | Путь: {root}"[:250]
        found = cl.call("POST", cl.ws("projects/"), {"name": clean[:80], "identifier": ident, "network": 2,
                                                     "description": desc, "page_view": True, "module_view": True,
                                                     "cycle_view": True, "issue_views_view": True}) or {}
        if not found.get("id"):
            raise PlaneError("Plane не вернул id созданного проекта")
    meta["plane_project_id"] = str(found["id"])
    meta["plane_project_identifier"] = str(found.get("identifier") or "PROJ")
    return meta["plane_project_id"], meta["plane_project_identifier"]


def ensure_states(cl: Client, project_id: str, meta: dict) -> dict:
    cached = meta.get("plane_state_ids")
    if isinstance(cached, dict) and cached.get("in_progress") and cached.get("in_review") \
            and str(meta.get("plane_state_project_id") or project_id) == str(project_id):
        return cached
    roles: dict[str, str] = {}
    for st in cl.results(cl.call("GET", cl.ws(f"projects/{project_id}/states/"))):
        name, group = (st.get("name") or "").strip().casefold(), (st.get("group") or "").casefold()
        if name in REVIEW_STATE_NAMES:
            roles["in_review"] = st["id"]
        elif group == "started":
            roles.setdefault("in_progress", st["id"])
        elif group == "unstarted":
            roles.setdefault("todo", st["id"])
    if "in_review" not in roles:
        made = cl.call("POST", cl.ws(f"projects/{project_id}/states/"),
                       {"name": "В проверке", "color": "#F59E0B", "group": "started", "sequence": 40000.0,
                        "description": "Задача выполнена и проверена планером, ожидает приёмки"}) or {}
        roles["in_review"] = made.get("id") or roles.get("in_progress", "")
    meta["plane_state_ids"], meta["plane_state_project_id"] = roles, str(project_id)
    return roles


def issue_html(plan: dict, meta: dict, page_url: str = "") -> str:
    esc = html.escape
    parts = [f"<h3>Цель задачи</h3><p>{esc(plan['goal'])}</p>", "<h3>Контекст проекта</h3><ul>",
             f"<li><strong>Папка проекта:</strong> <code>{esc(plan['root'])}</code></li>",
             f"<li><strong>Стек:</strong> {esc(str(meta.get('stack') or '—'))}</li>",
             f"<li><strong>Деплой / проверка:</strong> {esc(str(meta.get('deploy_rules') or '—'))}</li>"]
    if page_url:
        parts.append(f'<li><strong>Page проекта (паспорт и доки):</strong> <a href="{esc(page_url)}">'
                     f'{esc(PAGE_TITLE_PREFIX)}</a></li>')
    parts += ["</ul>", "<h3>Чеклист выполнения</h3>", "<ul>"]
    for it in plan["items"]:
        last = it["evidence"][-1] if it.get("evidence") else None
        ev = "" if not last else (" — <code>PASS</code>" if last["passed"]
                                  else f" — <code>FAIL</code> ({esc(core.fail_summary(last)[:200])})")
        parts.append(f"<li>{BOXES[it['status']]} <strong>{esc(it['id'])}. {esc(it['title'])}</strong> "
                     f"[<code>{esc(it['status'])}</code>, {esc(it['tier'])}]{ev}<br/>"
                     f"<small>Критерий приёмки: {esc(it['acceptance'])}</small></li>")
    parts.append("</ul>")
    rounds = core.review_rounds(plan)
    if rounds:
        parts.append("<h3>Приёмка</h3><ul>" + "".join(
            f"<li>Раунд #{r['n']}: <strong>{esc(r['verdict'])}</strong></li>" for r in rounds) + "</ul>")
    docs = all_docs(plan, {})
    if docs:
        parts.append("<h3>Опубликованная документация (Outline Wiki)</h3><ul>" + "".join(
            f'<li><a href="{esc(d["url"])}" target="_blank" rel="noopener noreferrer">{esc(d.get("title") or d["url"])}</a></li>'
            for d in docs) + "</ul>")
    if plan.get("summary"):
        parts.append(f"<h3>Итог</h3><p>{esc(plan['summary'])}</p>")
    return "\n".join(parts)


def sync(root: str, extra: dict | None = None) -> str:
    """Проект → паспорт в Pages → задача плана. Возвращает строку для отчёта ('' — нечего сообщать)."""
    try:
        plan = core.load(root)
        if not tracked(plan):
            return ""
        cl = client()
        if cl is None:
            return "Plane не настроен (~/.config/claude-planner/plane.env) — задача и паспорт не заведены."
        meta = enrich_meta(root, plan, extra)
        project_id, ident = ensure_project(cl, root, meta)
        states = ensure_states(cl, project_id, meta)
        save_meta(root, meta)
        docs = all_docs(plan, meta)
        if docs:
            meta["outline_docs"] = docs
        page_note = page_url = ""
        try:
            page_id = sync_page(cl, project_id, root, plan, meta)
            page_url = f"{cl.base_url}/{cl.workspace}/projects/{project_id}/pages/{page_id}"
        except PlaneError as exc:
            page_note = f" Паспорт не обновлён: {exc}."
        save_meta(root, meta)
        info = dict(plan.get("plane") or {})
        finished = plan["status"] == "finished"
        payload = {"name": plan["title"], "description_html": issue_html(plan, meta, page_url)}
        state = states.get("in_review") if finished else states.get("in_progress") or states.get("todo")
        if state:
            payload["state"] = str(state)
        if info.get("issue_id"):
            issue = cl.call("PATCH", cl.ws(f"projects/{project_id}/work-items/{info['issue_id']}/"), payload) or {}
        else:
            if not meta.get("plane_user_id"):
                meta["plane_user_id"] = str((cl.call("GET", "/api/v1/users/me/") or {}).get("id") or "")
                save_meta(root, meta)
            if meta["plane_user_id"]:
                payload["assignees"] = [meta["plane_user_id"]]
            issue = cl.call("POST", cl.ws(f"projects/{project_id}/work-items/"), payload) or {}
            if not issue.get("id"):
                raise PlaneError("Plane не вернул id созданной задачи")
            info["issue_id"] = str(issue["id"])
        seq = issue.get("sequence_id") or info.get("sequence_id")
        info.update(project_id=project_id, sequence_id=seq, issue_key=f"{ident}-{seq}" if seq else ident,
                    issue_url=f"{cl.base_url}/{cl.workspace}/projects/{project_id}/issues/{info['issue_id']}",
                    page_url=page_url, state="В проверке" if finished else "In Progress")
        with core.locked(root):
            fresh = core.load(root)
            fresh["plane"] = info
            core.atomic_write(core.pdir(root, "plan.json"), json.dumps(fresh, ensure_ascii=False, indent=1))
        return f"Plane: {info['issue_key']} ({info['state']}) {info['issue_url']}" \
               + (f" | паспорт: {page_url}" if page_url else "") + page_note
    except (PlaneError, core.PlanError, OSError, KeyError) as exc:
        return f"Plane: синхронизация не удалась ({exc}); план продолжает работать локально."
