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
_LINK = re.compile(r"(?:(?<=\s)[<xo])?[-=.~]{2,}[>xo]?|&")
_LINK_TEXT = re.compile(r"(?:--|==|-\.)\s+[^-=.>|]+?\s+(?=-->|==>|\.->|---|===)")
_SHAPE = re.compile(r"\[[^\[\]]*\]|\{[^{}]*\}|\([^()]*\)")
_CHART_WORDS = ("flowchart", "graph", "subgraph", "end", "classdef", "class", "style", "linkstyle", "click", "direction")


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


def heads(text: str) -> tuple[str, ...]:
    """Заголовки второго уровня: разделы страницы или шаблона."""
    body = _COMMENT.sub("", _FENCE.sub("", front(text)[1]))
    return tuple(h.strip() for h in re.findall(r"^##\s+(.+)$", body, re.M))


def required(root: str | None, kind: str, who: str) -> tuple[str, ...] | None:
    """Обязательные разделы страницы. Для страниц поставки — встроенный перечень; для своего типа — заголовки ##
    шаблона проекта (docs/_tpl/*.md) с такой же шапкой. None — тип неизвестен: проверять страницу не по чему."""
    if (kind, who) in SECTIONS:
        return SECTIONS[(kind, who)]
    folder = os.path.join(root, "docs", "_tpl") if root else ""
    try:
        names = sorted(n for n in os.listdir(folder) if n.endswith(".md")) if folder else []
    except OSError:
        names = []
    for name in names:
        try:
            with open(os.path.join(folder, name), encoding="utf-8") as fh:
                text = fh.read()
        except (OSError, UnicodeDecodeError):
            continue
        meta = front(text)[0]
        if meta.get("type") == kind and meta.get("audience") == who:
            return heads(text)
    return None


def _prose(body: str) -> list[str]:
    """Предложения обычного текста: без кода, таблиц, заголовков и разметки.

    Строки одного абзаца или пункта списка склеиваются: предложение, перенесённое на несколько строк, считается целиком."""
    out, para = [], []

    def flush():
        if para:
            out.extend(s for s in re.split(r"(?<=[.!?…])\s+", " ".join(para)) if s.strip())
            para.clear()

    for line in _COMMENT.sub("", _FENCE.sub("", body)).splitlines():
        line = re.sub(r"^(?:>\s*)+", "", line.strip())
        if not line or line.startswith(("#", "|", "```")):
            flush()
            continue
        mark = re.match(r"(?:[-*+]|\d+[.)])\s+", line)
        if mark:  # новый пункт списка — новый абзац
            flush()
            line = line[mark.end():]
        line = re.sub(r"`[^`]*`", " код ", line)
        para.append(re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", line).replace("**", ""))
    flush()
    return out


def nodes(chart: str) -> set[str]:
    """Узлы схемы. В блок-схеме это идентификаторы по обе стороны связей — с подписью в скобках или без неё."""
    lines = [ln.split("%%")[0].strip().rstrip(";") for ln in chart.splitlines()]
    lines = [ln for ln in lines if ln]
    found = set(_NODE.findall(chart)) - {"subgraph", "flowchart", "graph", "end"}
    if not lines or lines[0].split()[0].lower() not in ("flowchart", "graph"):
        return found
    for line in lines:
        if line.split()[0].lower() in _CHART_WORDS:
            continue
        line = re.sub(r"\|[^|]*\|", " ", re.sub(r'"[^"]*"', "", line))  # подписи узлов в кавычках и связей |текст|
        for _ in range(3):  # формы узлов, в том числе вложенные: [(...)], ((...)), {{...}}
            line = _SHAPE.sub("", line)
        for part in _LINK.split(_LINK_TEXT.sub("", line)):  # «A -- да --> B»: подпись внутри связи — не узел
            m = re.match(r"[^\W\d][\w-]*", part.split(":::")[0].strip())
            if m:
                found.add(m.group(0))
    return found


def lint(text: str, audience: str | None = None, max_words: int | None = None, root: str | None = None,
         template: str | None = None) -> list[str]:
    """Замечания к странице. template — текст шаблона, по которому её писали: тогда шапка страницы должна совпасть
    с шапкой шаблона, а у своего типа обязательны его разделы. root — проект, где искать свои шаблоны."""
    meta, body = front(text)
    problems = []
    want = front(template)[0] if template else {}
    kind, who = meta.get("type") or "", audience or want.get("audience") or meta.get("audience") or ""
    if not meta:
        problems.append("нет шапки страницы (--- type, audience, checks ---): возьми её из шаблона")
    else:
        if not kind:
            problems.append("в шапке нет type")
        if who not in MAX_WORDS:
            problems.append(f"в шапке audience должен быть одним из: {', '.join(MAX_WORDS)}")
        for key in ("type", "audience"):  # страница для разработчика не проходит как страница для поддержки
            if want.get(key) and meta.get(key) != want[key]:
                problems.append(f"в шапке {key}: {meta.get(key) or '—'}, а выбранный шаблон задаёт {want[key]}")
        filled = [(q, a) for q, a in meta["checks"] if a.strip() and not _PLACEHOLDER.search(a)]
        if len(filled) < 3:
            problems.append(f"в шапке нужно три контрольных вопроса с ответами (checks), заполнено {len(filled)}")
    left = sorted(set(_PLACEHOLDER.findall(_COMMENT.sub("", body))))  # в блоках кода тоже: «export X=<имя>» — не команда
    if left:
        problems.append("остались незаполненные места шаблона: " + ", ".join(left[:5]))
    kind = want.get("type") or kind
    need = required(root, kind, who) if kind and who in MAX_WORDS else ()
    if need is None and template:
        need = heads(template)
    if need is None:
        known = ", ".join(f"{k}/{w}" for k, w in SECTIONS)
        problems.append(f"type «{kind}» для audience «{who}» неизвестен (есть {known}): свой тип страницы — свой шаблон "
                        "docs/_tpl/<имя>.md с такой же шапкой, обязательными станут его разделы ##")
    have = [h.lower() for h in heads(text)]
    lack = [s for s in need or () if not any(h.startswith(s.lower()) for h in have)]
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
        count = len(nodes(chart))
        if count > MAX_NODES:
            problems.append(f"в схеме {count} узлов, нужно не больше {MAX_NODES}")
    return problems


def check(root: str, c: dict) -> tuple[bool, str, str]:
    base, full = os.path.realpath(root), os.path.realpath(os.path.join(root, c["path"]))
    if os.path.commonpath([base, full]) != base:  # и по ссылке тоже: текст замечаний цитирует страницу
        why = f"{c['path']} ведёт за пределы проекта — страница документации лежит в нём"
        return False, why, why
    try:
        with open(full, encoding="utf-8") as fh:
            text = fh.read()
    except (OSError, UnicodeDecodeError) as exc:
        return False, f"не читается {c['path']}: {exc}", str(exc)
    problems = lint(text, max_words=c.get("max_words"), root=root)
    if problems:
        return False, f"замечаний к странице: {len(problems)} — {problems[0][:120]}", "\n".join(f"- {p}" for p in problems)
    return True, "страница по правилам шаблона", "страница по правилам шаблона"
