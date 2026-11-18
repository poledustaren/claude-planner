"""HTML-отчёт по плану: снимок git до работы, дифф по файлам после, самодостаточная вёрстка без JS.

Только stdlib: модуль не импортирует ничего из пакета planner.
"""
import datetime
import html
import os
import re
import subprocess

MAX_FILE_DIFF = 60_000
MAX_TOTAL_DIFF = 1_200_000
BINARY_SNIFF = 8000
DEFAULT_EXCLUDE = (".cplan", ".planner")


def _git(root, *args):
    """stdout git-команды; любой сбой — None."""
    try:
        r = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True,
                           timeout=30, errors="replace")
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return r.stdout if r.returncode == 0 else None


def _split_z(out):
    return [p for p in (out or "").split("\0") if p]


def _under(path, dirs):
    return any(path == d or path.startswith(d + "/") for d in dirs)


def snapshot(root):
    """Состояние репозитория до работы. Не git или git упал -> {}. Дерево и индекс не меняет."""
    if (_git(root, "rev-parse", "--is-inside-work-tree") or "").strip() != "true":
        return {}
    rev = (_git(root, "rev-parse", "--verify", "-q", "HEAD") or "").strip()
    tree = (_git(root, "stash", "create") or "").strip()
    others = _git(root, "ls-files", "-z", "--others", "--exclude-standard")
    if others is None:
        return {}
    return {"rev": rev, "tree": tree, "untracked": _split_z(others)}


def _is_binary_bytes(data):
    return b"\0" in data[:BINARY_SNIFF]


def _hunks_only(text):
    """Дифф с первой строки '@@', без заголовков diff --git/index/---/+++."""
    pos = text.find("@@")
    while pos > 0 and text[pos - 1] != "\n":
        pos = text.find("@@", pos + 2)
    return text[pos:].rstrip("\n") if pos >= 0 else ""


def _cut(text):
    """Обрезка по границе строки до MAX_FILE_DIFF; -> (текст, обрезан)."""
    if len(text) <= MAX_FILE_DIFF:
        return text, False
    head = text[:MAX_FILE_DIFF]
    nl = head.rfind("\n")
    return (head[:nl] if nl >= 0 else head), True


def _parse_name_status(out):
    toks = (out or "").split("\0")
    res, i = [], 0
    while i < len(toks) and toks[i]:
        code = toks[i][0]
        if code in "RC":
            if i + 2 >= len(toks):
                break
            res.append(("R" if code == "R" else "A", toks[i + 2], toks[i + 1]))
            i += 3
        else:
            if i + 1 >= len(toks):
                break
            res.append((code if code in "ADM" else "M", toks[i + 1], None))
            i += 2
    return res


def _parse_numstat(out):
    """new_path -> (added, removed) или None для двоичного."""
    toks = (out or "").split("\0")
    res, i = {}, 0
    while i < len(toks) and toks[i]:
        parts = toks[i].split("\t", 2)
        if len(parts) < 3:
            break
        a, r, path = parts
        if path == "":  # переименование: пути следующими двумя токенами
            if i + 2 >= len(toks):
                break
            path = toks[i + 2]
            i += 3
        else:
            i += 1
        res[path] = None if a == "-" or r == "-" else (int(a), int(r))
    return res


def _untracked_entry(root, path):
    full = os.path.join(root, path)
    if not os.path.isfile(full):
        return None
    try:
        with open(full, "rb") as f:
            data = f.read()
    except OSError:
        return None
    entry = {"path": path, "status": "A", "added": 0, "removed": 0, "diff": "",
             "binary": False, "truncated": False}
    if _is_binary_bytes(data):
        entry["binary"] = True
        return entry
    lines = data.decode("utf-8", "replace").splitlines()
    entry["added"] = len(lines)
    if lines:
        entry["diff"] = "\n".join([f"@@ -0,0 +1,{len(lines)} @@"] + ["+" + ln for ln in lines])
    return entry


def git_files(root, snap, exclude=DEFAULT_EXCLUDE):
    """Изменения рабочего дерева после snapshot: список {path,status,added,removed,diff,binary,truncated}."""
    snap = snap or {}
    exclude = tuple(d.strip("/") for d in exclude)
    before = set(snap.get("untracked") or [])
    common = ["-c", "core.quotepath=off", "diff", "--no-color", "--no-ext-diff", "-M", "--relative"]
    spec = ["--", "."] + [f":(exclude){d}" for d in exclude]

    entries = []
    # Снимок stash create ни на что не ссылается: git gc со временем его убирает — тогда сравниваем с коммитом.
    for base in (snap.get("tree"), snap.get("rev")):
        ns = _git(root, *common, "--name-status", "-z", base, *spec) if base else None
        if ns is not None:
            break
    else:
        return []
    stats = _parse_numstat(_git(root, *common, "--numstat", "-z", base, *spec))
    for status, path, old in _parse_name_status(ns):
        if status == "A" and path in before:
            continue
        counts = stats.get(path, (0, 0))
        binary = counts is None
        added, removed = counts or (0, 0)
        diff = ""
        if not binary:
            paths = [path] + ([old] if old else [])
            out = _git(root, *common, "--unified=3", base, "--", *paths)
            diff = _hunks_only(out or "")
            if "\0" in diff[:BINARY_SNIFF]:
                binary, diff = True, ""
        entry = {"path": path, "status": status, "added": added, "removed": removed,
                 "diff": diff, "binary": binary, "truncated": False}
        if old:
            entry["old_path"] = old
        entries.append(entry)

    others = _git(root, "ls-files", "-z", "--others", "--exclude-standard")
    seen = {e["path"] for e in entries}
    for path in _split_z(others):
        if path in before or path in seen or _under(path, exclude):
            continue
        entry = _untracked_entry(root, path)
        if entry:
            entries.append(entry)

    entries.sort(key=lambda e: e["path"])
    total = 0
    for e in entries:
        e["diff"], e["truncated"] = _cut(e["diff"])
        if total + len(e["diff"]) > MAX_TOTAL_DIFF:
            e["diff"], e["truncated"] = "", True
        total += len(e["diff"])
    return entries


def attribute(files, items):
    """(item_id -> [file], other): файл достаётся первому пункту, чей files его покрывает."""
    by_item, other = {}, []
    specs = []
    for it in items or []:
        pats = []
        for p in it.get("files") or []:
            p = str(p)
            if p.startswith("new:"):
                p = p[4:]
            if p:
                pats.append(p)
        specs.append((it.get("id"), pats))
    for f in files or []:
        path = f.get("path", "")
        for iid, pats in specs:
            if any(path.startswith(p) if p.endswith("/") else path == p for p in pats):
                by_item.setdefault(iid, []).append(f)
                break
        else:
            other.append(f)
    return by_item, other


# ---------------------------------------------------------------- HTML

CSS = """
:root{--bg:#f6f7f9;--card:#fff;--fg:#1f2430;--muted:#667085;--line:#e1e4ea;--accent:#2f5fd0;
--ok:#1a7f4b;--bad:#c0392b;--warn:#a86a00;--add:#e6f6ea;--del:#fdeaea;--hunk:#eef1f7;--code:#f0f2f6}
@media (prefers-color-scheme: dark){:root{--bg:#15181e;--card:#1d2129;--fg:#e4e7ee;--muted:#9aa3b2;
--line:#2c323d;--accent:#7ca2ff;--ok:#5cc58a;--bad:#ff7b6e;--warn:#e0a84a;--add:#16301f;--del:#3a1c1e;
--hunk:#242a36;--code:#262c37}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1100px;margin:0 auto;padding:20px 16px 48px}
a{color:var(--accent)}
h1{margin:0 0 4px;font-size:1.6rem;line-height:1.25;overflow-wrap:anywhere}
h2{font-size:1.15rem;margin:28px 0 10px}
h3{font-size:.8rem;margin:16px 0 4px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
.goal{margin:4px 0 10px;color:var(--muted);white-space:pre-wrap;overflow-wrap:anywhere}
.meta{display:flex;flex-wrap:wrap;gap:6px 14px;color:var(--muted);font-size:.9rem}
.meta span{overflow-wrap:anywhere}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin:12px 0}
.stats{display:flex;flex-wrap:wrap;gap:6px 20px;margin:14px 0;font-weight:600}
.pre{white-space:pre-wrap;overflow-wrap:anywhere;margin:0}
.rich{overflow-wrap:anywhere}.rich p{margin:0 0 8px}.rich ul{margin:0 0 8px;padding-left:22px}
.rich li{margin:2px 0}.rich>:last-child{margin-bottom:0}
.toc{list-style:none;margin:0;padding:0}
.toc li{padding:3px 0;overflow-wrap:anywhere}
.badge{display:inline-block;padding:1px 8px;border-radius:999px;font-size:.78rem;border:1px solid var(--line);
color:var(--muted);margin-left:6px;white-space:nowrap}
.st-done{color:var(--ok)}.st-failed{color:var(--bad)}.st-skipped,.st-other{color:var(--warn)}
.card h2{margin:0;display:flex;flex-wrap:wrap;align-items:baseline;gap:4px 8px}
.num{color:var(--muted)}
ul.checks{list-style:none;margin:0;padding:0}
ul.checks li{padding:2px 0;overflow-wrap:anywhere}
code{font:13px/1.45 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;background:var(--code);
padding:1px 5px;border-radius:4px}
.ok{color:var(--ok)}.bad{color:var(--bad)}
details.file{border:1px solid var(--line);border-radius:8px;margin:6px 0;background:var(--card)}
details.file>summary{cursor:pointer;padding:6px 10px;overflow-wrap:anywhere}
.add-n{color:var(--ok);font-weight:600}.del-n{color:var(--bad);font-weight:600}
.tag{color:var(--muted);font-size:.8rem;margin:0 6px}
.wrap{overflow-x:auto;border-top:1px solid var(--line)}
table.diff{border-collapse:collapse;width:100%;font:12.5px/1.45 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
tab-size:4;-moz-tab-size:4}
table.diff td{padding:0 8px;white-space:pre;vertical-align:top}
table.diff td.n{color:var(--muted);text-align:right;user-select:none;min-width:3.2em;border-right:1px solid var(--line)}
table.diff tr.add{background:var(--add)}table.diff tr.del{background:var(--del)}
table.diff tr.hunk{background:var(--hunk);color:var(--muted)}
.note{padding:6px 10px;color:var(--muted);font-style:italic}
@media (max-width:600px){main{padding:12px 10px 32px}h1{font-size:1.3rem}.card{padding:10px 12px}}
"""

STATUS = {"done": ("выполнен", "st-done", "✓"), "skipped": ("пропущен", "st-skipped", "↷"),
          "failed": ("не прошёл", "st-failed", "✗")}
FILE_TAG = {"A": "новый", "D": "удалён", "R": "переименован"}
HUNK_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def _e(v):
    return html.escape("" if v is None else str(v), quote=True)


def _link(url, text):
    url = str(url or "")
    if re.match(r"https?://", url, re.I):
        return f'<a href="{_e(url)}" target="_blank" rel="noopener noreferrer">{_e(text or url)}</a>'
    return _e(text or url)


LOCALTIME = "/etc/localtime"


def _zone():
    """Пояс машины по /etc/localtime, а не по TZ процесса: агенту TZ могут подменить, а отчёт читает человек."""
    try:
        import zoneinfo
        return zoneinfo.ZoneInfo(os.path.realpath(LOCALTIME).split("/zoneinfo/", 1)[1])
    except Exception:  # noqa: BLE001 — пояс не определился: astimezone(None) возьмёт пояс процесса
        return None


def _when(v):
    """Время плана (ISO, в плане оно в UTC) — по поясу машины; не разобралось — как записано."""
    if not v:
        return ""
    try:
        stamp = datetime.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return _e(str(v)[:16].replace("T", " "))
    return _e((stamp.astimezone(_zone()) if stamp.tzinfo else stamp).strftime("%Y-%m-%d %H:%M"))


INLINE_RE = re.compile(r"`([^`\n]+)`|\*\*(`[^`\n]+`|[^*`\n]+)\*\*")
BULLET_RE = re.compile(r"(?:[-*•]|\d{1,2}[.)])\s+(.*)")
STATUS_RE = re.compile(r"\A\s*STATUS:[ \t]*[A-Z_]+[ \t]*\n?")


def _inline(text):
    out, pos = [], 0
    for m in INLINE_RE.finditer(text):
        out.append(_e(text[pos:m.start()]))
        code, bold = m.group(1), m.group(2)
        if code is not None:
            out.append(f"<code>{_e(code)}</code>")
        elif bold.startswith("`"):
            out.append(f"<b><code>{_e(bold[1:-1])}</code></b>")
        else:
            out.append(f"<b>{_e(bold)}</b>")
        pos = m.end()
    out.append(_e(text[pos:]))
    return "".join(out)


def _rich(text):
    """Комментарий человеку: абзацы, списки, `код` и **выделение**; остальная разметка остаётся текстом."""
    blocks, para, bullets = [], [], []

    def flush():
        if para:
            blocks.append("<p>" + "<br>".join(para) + "</p>")
            para.clear()
        if bullets:
            blocks.append("<ul>" + "".join(f"<li>{b}</li>" for b in bullets) + "</ul>")
            bullets.clear()

    for line in STATUS_RE.sub("", str(text or "")).replace("\r\n", "\n").split("\n"):
        s = line.strip()
        m = BULLET_RE.match(s)
        if not s:
            flush()
        elif m:
            if para:
                flush()
            bullets.append(_inline(m.group(1)))
        elif bullets and line[:1] in " \t":
            bullets[-1] += " " + _inline(s)
        else:
            if bullets:
                flush()
            head = re.match(r"#{1,6}\s+(.*)", s)
            para.append(f"<b>{_inline(head.group(1))}</b>" if head else _inline(s))
    flush()
    return '<div class="rich">' + "".join(blocks) + "</div>"


def _diff_table(diff):
    rows, old, new = [], 0, 0
    for line in diff.split("\n"):
        m = HUNK_RE.match(line)
        if m:
            old, new = int(m.group(1)), int(m.group(2))
            rows.append(f'<tr class="hunk"><td class="n"></td><td class="n"></td><td>{_e(line)}</td></tr>')
        elif line.startswith("+"):
            rows.append(f'<tr class="add"><td class="n"></td><td class="n">{new}</td><td>{_e(line)}</td></tr>')
            new += 1
        elif line.startswith("-"):
            rows.append(f'<tr class="del"><td class="n">{old}</td><td class="n"></td><td>{_e(line)}</td></tr>')
            old += 1
        elif line.startswith("\\"):
            rows.append(f'<tr class="hunk"><td class="n"></td><td class="n"></td><td>{_e(line)}</td></tr>')
        else:
            rows.append(f'<tr><td class="n">{old}</td><td class="n">{new}</td><td>{_e(line)}</td></tr>')
            old += 1
            new += 1
    return '<div class="wrap"><table class="diff">' + "".join(rows) + "</table></div>"


def _file(f):
    status = f.get("status")
    tag = FILE_TAG.get(status, "")
    label = f'<span class="tag">{tag}</span>' if tag else ""
    if status == "R" and f.get("old_path"):
        label = f'<span class="tag">переименован из {_e(f["old_path"])}</span>'
    head = (f'<summary><code>{_e(f.get("path"))}</code>{label} '
            f'<span class="add-n">+{int(f.get("added") or 0)}</span> '
            f'<span class="del-n">−{int(f.get("removed") or 0)}</span></summary>')
    body = ""
    if f.get("binary"):
        body = '<div class="note">двоичный файл</div>'
    elif f.get("diff"):
        body = _diff_table(f["diff"])
    if f.get("truncated"):
        body += '<div class="note">дифф обрезан</div>'
    return f'<details class="file">{head}{body}</details>'


def _item(n, it):
    iid = _e(it.get("id", n))
    label, cls, _mark = STATUS.get(it.get("status"), (it.get("status") or "—", "st-other", "•"))
    badge = f'<span class="badge {cls}">{_e(label)}</span>'
    if it.get("light"):
        badge += f'<span class="badge" title="{_e(it["light"])}">без проверки</span>'
    out = [f'<section class="card" id="item-{iid}">',
           f'<h2><span class="num">{n}.</span> <span>{_e(it.get("title"))}</span>{badge}</h2>']
    if it.get("acceptance"):
        out.append(f'<h3>Что требовалось</h3><p class="pre">{_e(it["acceptance"])}</p>')
    if it.get("note"):
        out.append(f'<h3>Что сделано</h3>{_rich(it["note"])}')
    checks = [c for c in it.get("checks") or [] if isinstance(c, dict)]
    if checks:
        lis = "".join(f'<li><span class="{"ok" if c.get("ok") else "bad"}">{"✓" if c.get("ok") else "✗"}</span> '
                      f'<code>{_e(c.get("cmd"))}</code></li>' for c in checks)
        out.append(f'<h3>Проверки</h3><ul class="checks">{lis}</ul>')
    files = it.get("files") or []
    files = [f for f in files if isinstance(f, dict)]
    if files:
        out.append("<h3>Файлы</h3>" + "".join(_file(f) for f in files))
    out.append("</section>")
    return "".join(out)


def render(data):
    """Полный HTML-документ отчёта. Любое поле data может отсутствовать."""
    d = data or {}
    items = [it for it in d.get("items") or [] if isinstance(it, dict)]
    other = [f for f in d.get("other_files") or [] if isinstance(f, dict)]
    title = d.get("title") or "План"

    meta = []
    if d.get("project"):
        meta.append(f'<span>Проект: {_e(d["project"])}</span>')
    if d.get("branch"):
        meta.append(f'<span>Ветка: <code>{_e(d["branch"])}</code></span>')
    deploy = d.get("deploy") or {}
    if deploy.get("target") in ("master", "test"):
        text = "Деплой: в мастер" if deploy["target"] == "master" else "Деплой: для теста"
        meta.append(f'<span title="{_e(deploy.get("quote"))}">{text}</span>')
    plane = d.get("plane") or {}
    if plane.get("key") or plane.get("url"):
        meta.append(f'<span>Plane: {_link(plane.get("url"), plane.get("key"))}</span>')
    if d.get("finished"):
        meta.append(f'<span>Закрыт: {_when(d["finished"])}</span>')
    elif d.get("created"):
        meta.append(f'<span>Создан: {_when(d["created"])}</span>')

    head = f"<header><h1>{_e(title)}</h1>"
    if d.get("goal"):
        head += f'<p class="goal">{_e(d["goal"])}</p>'
    if meta:
        head += f'<div class="meta">{"".join(meta)}</div>'
    head += "</header>"

    parts = [head]
    if d.get("summary"):
        parts.append(f'<h2>Итог</h2><div class="card">{_rich(d["summary"])}</div>')

    allf = [f for it in items for f in it.get("files") or [] if isinstance(f, dict)] + other
    added = sum(int(f.get("added") or 0) for f in allf)
    removed = sum(int(f.get("removed") or 0) for f in allf)
    parts.append(f'<div class="stats"><span>Пунктов: {len(items)}</span><span>Файлов: {len(allf)}</span>'
                 f'<span><span class="add-n">+{added}</span> <span class="del-n">−{removed}</span></span></div>')

    if items:
        lis = []
        for n, it in enumerate(items, 1):
            label, cls, mark = STATUS.get(it.get("status"), (it.get("status") or "—", "st-other", "•"))
            lis.append(f'<li><span class="{cls}">{mark}</span> <a href="#item-{_e(it.get("id", n))}">'
                       f'{n}. {_e(it.get("title"))}</a><span class="badge {cls}">{_e(label)}</span></li>')
        parts.append(f'<nav><ul class="toc">{"".join(lis)}</ul></nav>')
        parts.extend(_item(n, it) for n, it in enumerate(items, 1))

    if other:
        parts.append("<h2>Прочие изменения</h2>" + "".join(_file(f) for f in other))

    review = d.get("review") or {}
    issues = [i for i in review.get("issues") or [] if i]
    if review.get("verdict") or review.get("summary") or issues:
        box = ""
        if review.get("verdict"):
            box += f'<p><b>{_e(review["verdict"])}</b></p>'
        if review.get("summary"):
            box += f'<p class="pre">{_e(review["summary"])}</p>'
        if issues:
            box += "<ul>" + "".join(f"<li>{_e(i)}</li>" for i in issues) + "</ul>"
        parts.append(f'<h2>Приёмка</h2><div class="card">{box}</div>')

    docs = [x for x in d.get("docs") or [] if isinstance(x, dict)]
    if docs:
        lis = "".join(f'<li>{_link(x.get("url"), x.get("title"))}</li>' for x in docs)
        parts.append(f"<h2>Документация</h2><ul>{lis}</ul>")

    return ('<!doctype html>\n<html lang="ru"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>{_e(title)}</title><style>{CSS}</style></head><body><main>"
            + "".join(parts) + "</main></body></html>\n")


# ---------------------------------------------------------------- запись

TRANSLIT = dict(zip("абвгдежзийклмнопрстуфхцчшщыэюя",
                    ["a", "b", "v", "g", "d", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p", "r",
                     "s", "t", "u", "f", "h", "c", "ch", "sh", "sch", "y", "e", "yu", "ya"]))


def _slug(title):
    s = "".join(TRANSLIT.get(c, c) for c in str(title or "").lower())
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:40].strip("-")
    return s or "plan"


def write(plan_dir, data, now=None):
    """Пишет <plan_dir>/reports/<YYYYMMDD-HHMMSS>-<slug>.html, возвращает абсолютный путь."""
    now = now or datetime.datetime.now()
    folder = os.path.join(os.path.abspath(plan_dir), "reports")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{now.strftime('%Y%m%d-%H%M%S')}-{_slug((data or {}).get('title'))}.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(render(data))
    return path
