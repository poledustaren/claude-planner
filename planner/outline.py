"""Клиент Outline API и размещение страниц проекта в вики. Перенесён из agy-planner без изменения
дерева страниц («Проект · <имя>»), чтобы оба планера писали в одни и те же документы."""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request

CONFIG_FILES = ("~/.config/claude-planner/outline.env", "~/.config/agy-planner/outline.env")
FACTORY = None  # тесты подменяют на фабрику (клиент, коллекция)
KEYS = ("OUTLINE_URL", "OUTLINE_API_TOKEN", "OUTLINE_COLLECTION")
DEFAULT_COLLECTION = "hermes"
ROOT_PREFIX = "Проект · "
CONTAINER, PROJECTS, JOURNAL = "agy", "Проекты (agy)", "Журнал agy"
SEC_MARKER = "<!-- sec:"
TEXT_LIMIT = 20_000
SECRET_PATTERNS = [re.compile(p) for p in (
    r"ol_api_\w+", r"plane_api_\w+", r"sk-[A-Za-z0-9_-]{16,}", r"ghp_\w+", r"glpat-[\w-]+", r"xox[abp]-[\w-]+",
    r"(?i)(password|passwd|token|secret|api[_-]?key)\s*[:=]\s*\S+")]


class OutlineError(Exception):
    """Ошибка Outline API (HTTP >= 400, ok=false, нет связи)."""


def mask_secrets(text: str) -> str:
    for pattern in SECRET_PATTERNS:
        text = pattern.sub("***", text)
    return text


def _parse_env_file(path: str) -> dict:
    result = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.removeprefix("export ").split("=", 1)
                result[key.strip()] = value.strip().strip("'\"")
    except OSError:
        pass
    return result


def load_config(env=None, path: str | None = None) -> dict:
    env = os.environ if env is None else env
    cfg = {k: (env.get(k) or "").strip() for k in KEYS}
    for candidate in ([path] if path else [os.path.expanduser(p) for p in CONFIG_FILES]):
        if all(cfg.values()):
            break
        stored = _parse_env_file(candidate)
        cfg = {k: cfg[k] or stored.get(k, "") for k in KEYS}
    cfg["OUTLINE_URL"] = cfg["OUTLINE_URL"].rstrip("/")
    cfg["OUTLINE_COLLECTION"] = cfg["OUTLINE_COLLECTION"] or DEFAULT_COLLECTION
    return cfg


def connect():
    """(клиент, коллекция) либо None, если Outline не настроен."""
    if FACTORY is not None:
        return FACTORY()
    cfg = load_config()
    if not cfg["OUTLINE_URL"] or not cfg["OUTLINE_API_TOKEN"]:
        return None
    return Outline(cfg["OUTLINE_URL"], cfg["OUTLINE_API_TOKEN"]), cfg["OUTLINE_COLLECTION"]


class Outline:
    def __init__(self, url: str, token: str, post=None, timeout: int = 30):
        self.url, self.token, self.timeout = url.rstrip("/"), token, timeout
        self.post = post or self._http_post

    def _http_post(self, method: str, payload: dict) -> dict:
        req = urllib.request.Request(
            f"{self.url}/api/{method}", data=json.dumps(payload).encode(), method="POST",
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json",
                     "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise OutlineError(f"Outline {method}: HTTP {exc.code}: "
                               f"{exc.read().decode('utf-8', 'replace')[:500]}") from None
        except urllib.error.URLError as exc:
            raise OutlineError(f"Outline {method}: нет связи с {self.url}: {exc.reason}") from None

    def call(self, method: str, payload: dict) -> dict:
        resp = self.post(method, payload)
        if not isinstance(resp, dict) or resp.get("ok") is False:
            msg = resp.get("message") or resp.get("error") if isinstance(resp, dict) else resp
            raise OutlineError(f"Outline {method}: {msg}")
        return resp

    def collection_id(self, name: str) -> str:
        offset, limit = 0, 100
        while True:  # сервер может отдавать страницы меньше limit — идём до пустой
            data = self.call("collections.list", {"offset": offset, "limit": limit}).get("data") or []
            for col in data:
                if _norm(col.get("name")) == _norm(name):
                    return col["id"]
            if not data:
                break
            offset += len(data)
        return self.call("collections.create", {"name": name})["data"]["id"]

    def tree(self, collection_id: str) -> list:
        return self.call("collections.documents", {"id": collection_id}).get("data") or []

    def info(self, doc_id: str) -> dict:
        return self.call("documents.info", {"id": doc_id})["data"]

    def create(self, collection_id: str, title: str, text: str, parent_id: str | None = None) -> dict:
        payload = {"title": str(title)[:100], "text": text, "collectionId": collection_id, "publish": True}
        if parent_id:
            payload["parentDocumentId"] = parent_id
        return self.call("documents.create", payload)["data"]

    def update(self, doc_id: str, text: str, append: bool = False) -> dict:
        payload = {"id": doc_id, "text": text}
        if append:
            payload["append"] = True
        return self.call("documents.update", payload)["data"]

    def doc_url(self, data: dict) -> str:
        return self.url + (data.get("url") or "")


# ───────────────────────── дерево коллекции ─────────────────────────

def _norm(text) -> str:
    return " ".join(str(text or "").split()).casefold()


def find_node(nodes: list, title: str, deep: bool = True) -> dict | None:
    for node in nodes:
        if _norm(node.get("title")) == _norm(title):
            return node
        if deep:
            found = find_node(node.get("children") or [], title)
            if found:
                return found
    return None


def _ensure_child(client: Outline, cid: str, parent: dict, title: str, text: str) -> dict:
    """Прямой ребёнок parent с таким title; нет — создаётся (parent["id"] None = верх коллекции)."""
    children = parent.setdefault("children", [])
    node = find_node(children, title, deep=False)
    if node is None:
        data = client.create(cid, title, text, parent["id"])
        node = {"id": data["id"], "title": data.get("title", title), "url": data.get("url", ""), "children": []}
        children.append(node)
    return node


def root_title(project: str) -> str:
    return ROOT_PREFIX + project


def publish(client: Outline, collection: str, project: str, title: str, markdown: str,
            mode: str = "upsert", parent_title: str | None = None, passport: str = "") -> dict:
    """Публикует страницу проекта; возвращает {doc_id, url, title, mode: create|update|append}."""
    if mode not in ("upsert", "append"):
        raise ValueError("mode должен быть upsert или append")
    title = str(title).strip()[:100]
    if parent_title:
        parent_title = str(parent_title).strip()[:100]
    text, main = mask_secrets(markdown), root_title(project)[:100]
    cid = client.collection_id(collection)
    top = {"id": None, "children": client.tree(cid)}
    root = find_node(top["children"], main)
    is_root = _norm(title) == _norm(main)
    if root is None:
        agy = _ensure_child(client, cid, top, CONTAINER, "Страницы, которые ведёт agy-planner.")
        projects = _ensure_child(client, cid, agy, PROJECTS, "Проекты, которые ведёт agy-planner.")
        if is_root:
            data = client.create(cid, main, text, projects["id"])
            return {"doc_id": data["id"], "url": client.doc_url(data), "title": main, "mode": "create"}
        root = _ensure_child(client, cid, projects, main, mask_secrets(passport) or f"# {main}")
        protected = False
    else:
        protected = SEC_MARKER in (client.info(root["id"]).get("text") or "")
    if is_root and not protected:
        data = client.update(root["id"], text, append=mode == "append")
        return {"doc_id": root["id"], "url": client.doc_url(data), "title": main,
                "mode": "append" if mode == "append" else "update"}
    if is_root:
        parent, title = root, JOURNAL
    elif parent_title and _norm(parent_title) != _norm(main):
        parent = _ensure_child(client, cid, root, parent_title, f"Раздел проекта {project}.")
    else:
        parent = root
    page = find_node(parent.get("children") or [], title, deep=False)
    if page is None:
        data, done = client.create(cid, title, text, parent["id"]), "create"
    else:
        data = client.update(page["id"], text, append=mode == "append")
        done = "append" if mode == "append" else "update"
    return {"doc_id": data.get("id") or page["id"], "url": client.doc_url(data), "title": title, "mode": done}


def list_pages(client: Outline, collection: str, project: str, include_text: bool = False,
               limit: int = 50) -> dict | None:
    """Корневой документ проекта и все его потомки; None — если корня ещё нет."""
    cid = client.collection_id(collection)
    root = find_node(client.tree(cid), root_title(project))
    if root is None:
        return None
    flat = []

    def rec(node, depth):
        flat.append((node, depth))
        for child in node.get("children") or []:
            rec(child, depth + 1)
    rec(root, 0)
    pages = []
    for node, depth in flat[:limit]:
        data = client.info(node["id"])
        page = {"id": node["id"], "title": data.get("title", node.get("title")), "depth": depth,
                "url": client.doc_url(data if data.get("url") else node), "updatedAt": data.get("updatedAt")}
        if depth == 0:
            page["protected"] = SEC_MARKER in (data.get("text") or "")
        if include_text:
            body = data.get("text") or ""
            page["text"] = body[:TEXT_LIMIT] + ("\n…[обрезано]" if len(body) > TEXT_LIMIT else "")
        pages.append(page)
    return {"root": root_title(project), "pages": pages, "truncated": len(flat) > limit}
