"""Детерминированная проверка страницы документации: шапка, разделы, длина предложений, жаргон, схема.

Ловит то, что видно без модели: автор держался шаблона или нет. Понятность по существу это не доказывает —
для неё в шапке страницы лежат контрольные вопросы (checks).
"""
from __future__ import annotations

import os
import re

MAX_WORDS = {"dev": 25, "support": 15}
MAX_NODES = 10
SECTIONS = {
    ("howto", "dev"): ("Подготовка", "Шаги", "Проверка", "Как откатить"),
    ("howto", "support"): ("Как узнать эту ситуацию", "Шаги", "Если не помогло"),
    ("done", "dev"): ("Зачем", "Что изменилось", "Как пользоваться"),
}
# Слова разработки, которых не должно быть в инструкции для поддержки. «Под» как предлог не трогаем.
JARGON = re.compile(r"(?<![\w-])(депло\w*|эндпоинт\w*|бэкенд\w*|фронтенд\w*|роллб[еэ]к\w*|откат\w*|лог(?:и|ов|ах|ам)|"
                    r"под(?:ы|ов|ам|ах)|кластер\w*|миграци\w*)(?![\w-])", re.I)
_FENCE = re.compile(r"```.*?```", re.S)
_COMMENT = re.compile(r"<!--.*?-->", re.S)
_FRONT = re.compile(r"\A---\n(.*?)\n---\n", re.S)
_PLACEHOLDER = re.compile(r"<[^>\n]*[А-Яа-яЁё][^>\n]*>")
_WORD = re.compile(r"[A-Za-zА-Яа-яЁё0-9][\w-]*")
_NODE = re.compile(r"(?<![\w-])([A-Za-z]\w*)\s*(?:\[|\{|\(\()")


def front(text: str) -> tuple[dict, str]:
    """Шапка страницы (простые пары ключ: значение и список checks) и остальной текст."""
    m = _FRONT.match(text)
    if not m:
        return {}, text
    head = m.group(1)
    meta = {k: v.strip() for k, v in re.findall(r"^(\w+):[ \t]*(.*)$", head, re.M)}
    meta["checks"] = list(zip(re.findall(r"^\s*-\s*q:\s*(.+)$", head, re.M), re.findall(r"^\s*a:\s*(.+)$", head, re.M)))
    return meta, text[m.end():]


def publishable(text: str) -> str:
    """Страница для вики: без шапки и без служебного комментария с правилами автора."""
    return _COMMENT.sub("", front(text)[1]).strip() + "\n"


def _prose(body: str) -> list[str]:
    """Предложения обычного текста: без кода, таблиц, заголовков и разметки."""
    out = []
    for line in _COMMENT.sub("", _FENCE.sub("", body)).splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "|", "```")):
            continue
        line = re.sub(r"^(?:>\s*)+|^(?:[-*+]|\d+[.)])\s+", "", line)
        line = re.sub(r"`[^`]*`", " код ", line)
        line = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", line)
        out += [s for s in re.split(r"(?<=[.!?…])\s+", line.replace("**", "")) if s.strip()]
    return out


def lint(text: str, audience: str | None = None, max_words: int | None = None) -> list[str]:
    meta, body = front(text)
    problems = []
    kind, who = meta.get("type") or "", audience or meta.get("audience") or ""
    if not meta:
        problems.append("нет шапки страницы (--- type, audience, checks ---): возьми её из шаблона")
    else:
        if not kind:
            problems.append("в шапке нет type")
        if who not in MAX_WORDS:
            problems.append(f"в шапке audience должен быть одним из: {', '.join(MAX_WORDS)}")
        filled = [(q, a) for q, a in meta["checks"] if a.strip() and not _PLACEHOLDER.search(a)]
        if len(filled) < 3:
            problems.append(f"в шапке нужно три контрольных вопроса с ответами (checks), заполнено {len(filled)}")
    clean = _COMMENT.sub("", _FENCE.sub("", body))
    left = sorted(set(_PLACEHOLDER.findall(clean)))
    if left:
        problems.append("остались незаполненные места шаблона: " + ", ".join(left[:5]))
    heads = [h.strip().lower() for h in re.findall(r"^##\s+(.+)$", clean, re.M)]
    lack = [s for s in SECTIONS.get((kind, who), ()) if not any(h.startswith(s.lower()) for h in heads)]
    if lack:
        problems.append("нет разделов: " + ", ".join(lack))
    limit = max_words or MAX_WORDS.get(who, max(MAX_WORDS.values()))
    long = [s for s in _prose(body) if len(_WORD.findall(s)) > limit]
    if long:
        problems.append(f"предложений длиннее {limit} слов — {len(long)}: " + " | ".join(s[:90] for s in long[:3]))
    if who == "support":
        slang = sorted({m.group(1).lower() for s in _prose(body) for m in JARGON.finditer(s)})
        if slang:
            problems.append("жаргон разработки в инструкции для поддержки: " + ", ".join(slang[:8]))
    charts = re.findall(r"```mermaid\n(.*?)```", body, re.S)
    if len(charts) > 1:
        problems.append(f"схем на странице {len(charts)}, нужна одна")
    for chart in charts[:1]:
        nodes = set(_NODE.findall(chart)) - {"subgraph", "flowchart", "graph", "end"}
        if len(nodes) > MAX_NODES:
            problems.append(f"в схеме {len(nodes)} узлов, нужно не больше {MAX_NODES}")
    return problems


def check(root: str, c: dict) -> tuple[bool, str, str]:
    try:
        with open(os.path.join(root, c["path"]), encoding="utf-8") as fh:
            text = fh.read()
    except (OSError, UnicodeDecodeError) as exc:
        return False, f"не читается {c['path']}: {exc}", str(exc)
    problems = lint(text, max_words=c.get("max_words"))
    if problems:
        return False, f"замечаний к странице: {len(problems)} — {problems[0][:120]}", "\n".join(f"- {p}" for p in problems)
    return True, "страница по правилам шаблона", "страница по правилам шаблона"
