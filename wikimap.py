#!/usr/bin/env python3
"""wikimap - zero-LLM incremental index + lazy semantic layer for markdown knowledge bases.

Design: eager structure, lazy semantics.
- update  : deterministic re-parse of changed files only (no LLM, sub-second, always)
- search  : substring-friendly ranked section search (CJK-safe, no tokenizer issues)
- links   : outlinks / backlinks / REQ-ID cross-references / inferred connections
- note    : semantic insights saved at answer-time, auto-invalidated by source sha
- suggest : heuristic candidates for unwritten connections between documents (no LLM)

Single file, stdlib only.
"""
import argparse
import difflib
import fnmatch
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
import zlib
from collections import Counter, deque
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

VERSION = "0.7.0"

IGNORE_DIRS = {
    ".obsidian", ".git", ".wikimap", "graphify-out", "node_modules",
    ".claude", ".github", "__pycache__", ".venv", "venv", ".trash",
}
IGNORE_FILES = {"MAP.md", ".wikimapignore"}
PLAIN_EXTS = {".txt", ".rst", ".org", ".adoc"}
HTML_EXTS = {".html", ".htm"}
IMG_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
INDEX_EXTS = {".md", ".pdf", ".svg"} | PLAIN_EXTS | HTML_EXTS | IMG_EXTS
MAP_DISABLED = "-"

SKILL_TEMPLATE = r"""---
name: wikimap
description: Zero-LLM incremental index + lazy semantic notes for a markdown knowledge base (wiki, Obsidian vault, spec folder; plain text, HTML, PDF, and images indexed too). Use when searching vault documents ("where is the X policy/spec?"), tracing links, backlinks, or requirement IDs across documents, and refreshing the index after creating or editing vault files.
---

# wikimap

Index tool for a markdown knowledge base. Principle: **eager structure, lazy semantics** — builds are deterministic parsing only (zero LLM calls, sub-second), semantic knowledge accumulates at answer time.

All commands: `python3 ~/.claude/skills/wikimap/wikimap.py [--root <vault>] <cmd>`
(`--root` optional when cwd is inside the vault — the `.wikimap/` directory is auto-detected upward)

| Command | Purpose |
|---------|---------|
| `update [--ignore <dir\|glob>] [--map-path <rel> \| --no-map]` | incremental re-index + regenerate the map (sha-diff, changed files only; prints coverage: indexed vs skipped; map ends with a Health section — orphans, broken links, stale semantics). Persistent excludes: `.wikimapignore` at vault root, one dir/glob per line. `--map-path`/`--no-map` persist — use when another tool also indexes the vault root |
| `search "query" [-n 8] [-C 3 \| --full]` | ranked section search (filename/title/heading boosted; FTS5-accelerated when available); shows matched lines (≤3); `-C N` adds N context lines, `--full` prints the whole section; fresh notes surface first; CJK substring-safe. Query syntax: `"exact phrase"`, `title:x` `path:x` `heading:x` `tag:x` field filters (frontmatter `tags: [a, b]` are indexed), `type:md\|html\|pdf\|image\|text` file-type filter. When no section matches every term, results relax to a majority-of-terms OR and are marked `partial k/n` (field filters stay hard) |
| `links <REQ-ID|filename|path>` | docs mentioning a requirement ID, or a doc's outlinks/backlinks/inferred connections — entries tagged `[linked|…]` (written by a human) vs `[inferred|…]` (confirmed guess) |
| `path <a> <b>` | shortest connection path between two docs (BFS over wiki/md links + fresh edges, both directions) |
| `note add --question "..." --insight "..." --sources a.md,b.md` | save an answer-time insight (source shas pinned) |
| `notes [--all] [--prune]` | list notes / prune stale ones |
| `suggest [--doc path] [-n 10] [--wikilink]` | heuristic candidates for unwritten doc connections (shared rare terms, requirement IDs, code refs — no LLM); `--wikilink` prints paste-ready `[[links]]` for the doc body |
| `edge add --src a.md --dst b.md --relation ... --rationale "..."` | confirm a connection (both shas pinned; goes stale if either file changes) |
| `edge repin --src a.md --dst b.md` | after reviewing both ends of a stale edge: refresh the sha pins, keep the rationale |
| `edges [--all] [--prune]` | list inferred connections |
| `import-graphify <graph.json>` | one-time import of INFERRED edges from an existing graphify graph |
| `install --hook` | git post-commit hook that runs `update` automatically (appends to an existing hook) |
| `mv <old> <new> [--apply]` | move/rename a doc AND rewrite every wikilink/md/img reference to it (dry run without `--apply`); semantics.jsonl paths updated too |
| `fix-links [--json]` | for every broken link the Health section counts: suggest close-match targets (suggestions only, never auto-applied) |

`search`/`links`/`path`/`suggest`/`notes`/`edges` accept `--json` for structured output — prefer it when a script consumes the result.

Notes and edges live in `.wikimap/semantics.jsonl` (append-only, git-committable — the source of truth); `.wikimap/index.db` is a disposable cache rebuilt from files + that jsonl.

## Rules for the agent

1. **On a vault question**: read `MAP.md` at the vault root first, then `search` for relevant sections, then Read only those file sections. Never sweep whole files. For fact/value questions ("what is the limit/period/owner?"), retry with `-C 3` or `--full` before falling back to Read.
2. **After answering**: if the answer synthesized multiple documents into a non-obvious conclusion, save it with `note add` (sources = the actual evidence files, vault-relative paths).
3. **After creating/editing/deleting vault files**: run `update` before the session ends (sub-second, zero tokens).
4. **`[NOTE fresh]` in search results**: sha-verified cache — trust and reuse it. Stale notes are hidden automatically.
5. **After creating or substantially editing a doc**: run `suggest --doc <path> -n 5 --wikilink`, read the candidates' relevant sections, and paste only the genuinely related `[[links]]` into the doc body (a "Related" line is fine) — explicit links are readable by every vault tool and survive re-indexing. Use `edge add` only when you can't edit the doc. Requirement IDs are per-document local numbers — a match across unrelated projects is a false signal; discard it.
6. **Trust tags in `links` output**: `[linked|…]` means a human wrote that connection in the source text; `[inferred|…]` means it was guessed and then confirmed (sha-verified). Weight answers accordingly.
7. **A stale edge whose connection still holds**: if it went stale only because an endpoint was edited, review both docs and run `edge repin --src a --dst b` — the rationale is kept, only the sha pins refresh. Re-add only when the relationship itself changed.
"""

HEADING = re.compile(r"^(#{1,6})\s+(.*)")
WIKILINK = re.compile(r"\[\[([^\]|#]+)")
MDLINK = re.compile(r"\]\(([^)#\s]+\.md)\)")
IMGLINK = re.compile(r"!\[([^\]]*)\]\(([^)#\s]+)\)")
CODEREF = re.compile(r"\b[\w/.-]*\w\.(?:kt|kts|swift|java|py|ts|tsx|gradle)\b")
REQID = re.compile(r"\bREQ-\d+\b")
SVG_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.DOTALL | re.IGNORECASE)
XML_TAG = re.compile(r"<[^>]*>")
PDF_STREAM = re.compile(rb"stream\r?\n(.*?)endstream", re.DOTALL)
PDF_TEXTBLOCK = re.compile(rb"\bBT\b(.*?)\bET\b", re.DOTALL)
PDF_SHOWTEXT = re.compile(rb"\(((?:\\.|[^\\()])*)\)\s*(?:Tj|'|\")")
PDF_ARRAY = re.compile(rb"\[((?:\\.|[^\\\]])*)\]\s*TJ", re.DOTALL)
PDF_LITERAL = re.compile(rb"\(((?:\\.|[^\\()])*)\)")
PDF_TITLE = re.compile(rb"/Title\s*\(((?:\\.|[^\\()])*)\)")
PDF_WORD = re.compile(r"[A-Za-z0-9]{3,}|[가-힣]{2,}|[぀-ヿ一-鿿]{2,}")


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def norm_rel(p):
    # index keys are POSIX-style — Windows args/paths would miss otherwise
    return str(p).replace("\\", "/")


def find_root(cli_root):
    if cli_root:
        return Path(cli_root).expanduser().resolve()
    p = Path.cwd()
    for cand in [p, *p.parents]:
        if (cand / ".wikimap").is_dir():
            return cand
    return p


def open_db(root: Path) -> sqlite3.Connection:
    d = root / ".wikimap"
    d.mkdir(exist_ok=True)
    db = sqlite3.connect(d / "index.db")
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS files(
            path TEXT PRIMARY KEY, sha TEXT, mtime REAL, title TEXT, words INT);
        CREATE TABLE IF NOT EXISTS sections(
            path TEXT, line INT, level INT, heading TEXT, content TEXT);
        CREATE TABLE IF NOT EXISTS links(src TEXT, dst TEXT, kind TEXT);
        CREATE TABLE IF NOT EXISTS notes(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            question TEXT, insight TEXT, created TEXT, sources TEXT);
        CREATE TABLE IF NOT EXISTS edges(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            src TEXT, dst TEXT, relation TEXT, rationale TEXT,
            origin TEXT, created TEXT, src_sha TEXT, dst_sha TEXT,
            UNIQUE(src, dst, relation));
        CREATE INDEX IF NOT EXISTS idx_sections_path ON sections(path);
        CREATE INDEX IF NOT EXISTS idx_links_src ON links(src);
        CREATE INDEX IF NOT EXISTS idx_links_dst ON links(dst);
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS tags(path TEXT, tag TEXT);
        CREATE INDEX IF NOT EXISTS idx_tags_path ON tags(path);
        CREATE TABLE IF NOT EXISTS img_alts(src TEXT, dst TEXT, alt TEXT);
        CREATE INDEX IF NOT EXISTS idx_img_alts_src ON img_alts(src);
        """
    )
    try:
        db.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS sections_fts USING fts5("
            "path UNINDEXED, line UNINDEXED, heading, content, tokenize='trigram')"
        )
    except sqlite3.OperationalError:
        pass  # sqlite < 3.34 has no trigram tokenizer — search falls back to linear scan
    return db


FTS_MIN_DOCS = 500  # below this a linear scan is already sub-100ms — skip FTS upkeep


def has_fts(db):
    return bool(
        db.execute("SELECT 1 FROM sqlite_master WHERE name='sections_fts'").fetchone()
    )


def fts_populated(db):
    return bool(db.execute("SELECT 1 FROM sections_fts LIMIT 1").fetchone())


def sync_fts(db, changed_rels, deleted_rels):
    if not has_fts(db):
        return
    total = db.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    if total < FTS_MIN_DOCS:
        if fts_populated(db):
            db.execute("DELETE FROM sections_fts")
        return
    if not fts_populated(db):
        db.execute(
            "INSERT INTO sections_fts(path, line, heading, content) "
            "SELECT path, line, heading, content FROM sections"
        )
        return
    stale = sorted(set(changed_rels) | set(deleted_rels))
    for i in range(0, len(stale), 500):
        chunk = stale[i : i + 500]
        db.execute(
            "DELETE FROM sections_fts WHERE path IN (%s)" % ",".join("?" * len(chunk)),
            chunk,
        )
    for rel in changed_rels:
        db.execute(
            "INSERT INTO sections_fts(path, line, heading, content) "
            "SELECT path, line, heading, content FROM sections WHERE path=?",
            (rel,),
        )


def parse_frontmatter(lines):
    meta = {}
    end = 0
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                end = i + 1
                break
            m = re.match(r"^(\w[\w-]*):\s*(.*)$", lines[i])
            if m:
                meta[m.group(1).lower()] = m.group(2).strip().strip("\"'")
    return meta, end


def parse_plain_sections(rel, lines):
    sections = []
    buf, start = [], 1
    for i, ln in enumerate(lines):
        if not ln.strip() and sum(1 for l in buf if l.strip()) >= 12:
            heading = next((l.strip()[:60] for l in buf if l.strip()), "(text)")
            sections.append((rel, start, 1, heading, "\n".join(buf).strip("\n")))
            buf, start = [], i + 2
        else:
            buf.append(ln)
    if any(l.strip() for l in buf):
        heading = next((l.strip()[:60] for l in buf if l.strip()), "(text)")
        sections.append((rel, start, 1, heading, "\n".join(buf).strip("\n")))
    return sections


class _HTMLDoc(HTMLParser):
    HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
    BREAKS = {"p", "div", "li", "tr", "br", "section", "article",
              "ul", "ol", "table", "blockquote", "pre", "hr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.hrefs = []
        self.imgs = []
        self.events = []
        self._skip = 0
        self._in_title = False
        self._heading = None

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag in self.HEADINGS:
            self._heading = (int(tag[1]), self.getpos()[0], [])
        elif tag == "a":
            href = dict(attrs).get("href") or ""
            if href:
                self.hrefs.append(href)
        elif tag == "img":
            a = dict(attrs)
            if a.get("src"):
                self.imgs.append((a["src"], a.get("alt") or ""))
        if tag in self.BREAKS:
            self.events.append(("text", "\n"))

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        elif tag == "title":
            self._in_title = False
        elif tag in self.HEADINGS and self._heading:
            level, line, buf = self._heading
            self.events.append(("heading", (level, line, " ".join("".join(buf).split()))))
            self._heading = None
        elif tag in self.BREAKS:
            self.events.append(("text", "\n"))

    def handle_data(self, data):
        if self._skip:
            return
        if self._in_title:
            self.title += data
        elif self._heading is not None:
            self._heading[2].append(data)
        else:
            self.events.append(("text", data))


def parse_html_doc(rel, text, stem):
    doc = _HTMLDoc()
    try:
        doc.feed(text)
        doc.close()
    except Exception:
        pass  # malformed HTML — keep whatever parsed before the error
    chunks = [["(intro)", 0, 1, []]]
    first_h1 = ""
    for kind, payload in doc.events:
        if kind == "text":
            chunks[-1][3].append(payload)
        else:
            level, line, heading = payload
            heading = heading or "(heading)"
            if not first_h1 and level == 1:
                first_h1 = heading
            chunks.append([heading, level, line, []])
    sections = []
    for heading, level, line, buf in chunks:
        content = "\n".join(l.strip() for l in "".join(buf).splitlines())
        content = re.sub(r"\n{3,}", "\n\n", content).strip("\n")
        if content or heading != "(intro)":
            sections.append((rel, line, level, heading, content))
    if len(chunks) == 1 and sections:
        sections = parse_plain_sections(rel, sections[0][4].splitlines())
    title = " ".join(doc.title.split()) or first_h1 or stem
    return title, sections, doc.hrefs, doc.imgs


def parse_frontmatter_tags(raw):
    return [t.strip().strip("\"'").lower() for t in raw.strip("[]").split(",") if t.strip()]


def _pdf_str(raw: bytes) -> str:
    out, i = bytearray(), 0
    esc = {ord("n"): 10, ord("r"): 13, ord("t"): 9,
           ord("("): 40, ord(")"): 41, ord("\\"): 92}
    while i < len(raw):
        c = raw[i]
        if c == 0x5C and i + 1 < len(raw):
            n = raw[i + 1]
            if n in esc:
                out.append(esc[n])
                i += 2
            elif 0x30 <= n <= 0x37:
                j = i + 1
                while j < len(raw) and j < i + 4 and 0x30 <= raw[j] <= 0x37:
                    j += 1
                out.append(int(raw[i + 1 : j], 8) & 0xFF)
                i = j
            else:
                out.append(n)
                i += 2
        else:
            out.append(c)
            i += 1
    b = bytes(out)
    if b[:2] == b"\xfe\xff":
        return b[2:].decode("utf-16-be", errors="replace")
    return b.decode("latin-1", errors="replace")


def _pdf_wordish(s: str) -> bool:
    s = s.strip()
    if len(s) < 2:
        return False
    good = sum(1 for ch in s if ch.isalnum() or ch.isspace() or ch in ".,;:!?%()·-–—'\"/&+@")
    return good / len(s) >= 0.8


def extract_pdf_text(data: bytes):
    """Deterministic literal-string extraction, BT..ET text blocks only — image/CID
    streams yield nothing wordish, so scanned or CJK-CID PDFs fall back to name-only."""
    parts = []

    def emit(raw):
        s = _pdf_str(raw)
        if _pdf_wordish(s):
            parts.append(s)

    def harvest(buf):
        if b"BT" not in buf:
            return
        for block in PDF_TEXTBLOCK.finditer(buf):
            b = block.group(1)
            for m in PDF_SHOWTEXT.finditer(b):
                emit(m.group(1))
                parts.append(" ")
            for arr in PDF_ARRAY.finditer(b):
                body = arr.group(1)
                if b"(" not in body:
                    continue
                for lit in PDF_LITERAL.finditer(body):
                    emit(lit.group(1))
                parts.append(" ")
            parts.append("\n")

    for m in PDF_STREAM.finditer(data):
        buf = m.group(1)
        try:
            buf = zlib.decompress(buf)
        except Exception:
            pass  # not FlateDecode — treat as an uncompressed content stream
        harvest(buf)

    text = "".join(ch for ch in "".join(parts) if ch.isprintable() or ch in "\n ")
    text = re.sub(r"[ \t]{2,}", " ", text)
    ok = len(PDF_WORD.findall(text)) >= 5
    title = ""
    tm = PDF_TITLE.search(data)
    if tm:
        title = " ".join(_pdf_str(tm.group(1)).split())
        if not _pdf_wordish(title):
            title = ""
    return text if ok else "", title, ok


def stem_words(stem: str) -> str:
    return " ".join(w for w in re.split(r"[-_.\s]+", stem) if w)


def parse_file(root: Path, path: Path):
    rel = path.relative_to(root).as_posix()
    suffix = path.suffix.lower()
    stat_mtime = path.stat().st_mtime

    def resolve_doc_link(dst):
        resolved = (path.parent / dst).resolve()
        try:
            return resolved.relative_to(root).as_posix()
        except ValueError:
            return norm_rel(dst)

    if suffix in IMG_EXTS:
        data = path.read_bytes()
        return {
            "path": rel, "sha": hashlib.sha256(data).hexdigest(), "mtime": stat_mtime,
            "title": path.stem, "words": 0,
            "sections": [(rel, 1, 1, "(image)", stem_words(path.stem))],
            "links": [], "tags": [], "img_alts": [],
        }

    if suffix == ".pdf":
        data = path.read_bytes()
        text, pdf_title, ok = extract_pdf_text(data)
        if ok:
            sections = parse_plain_sections(rel, text.splitlines())
        else:
            sections = [(rel, 1, 1, "(pdf)", stem_words(path.stem))]
        links = [(rel, m, "code") for m in set(CODEREF.findall(text))]
        links += [(rel, m, "req") for m in set(REQID.findall(text))]
        return {
            "path": rel, "sha": hashlib.sha256(data).hexdigest(), "mtime": stat_mtime,
            "title": pdf_title or path.stem, "words": len(text.split()),
            "sections": sections, "links": links, "tags": [], "img_alts": [],
            "pdf_name_only": not ok,
        }

    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    html_hrefs, html_imgs, tags = [], [], []

    if suffix == ".md":
        meta, body_start = parse_frontmatter(lines)
        title = meta.get("title") or ""
        tags = parse_frontmatter_tags(meta.get("tags", ""))
        sections = []
        cur_heading, cur_level, cur_line, buf = "(intro)", 0, body_start + 1, []
        for i in range(body_start, len(lines)):
            m = HEADING.match(lines[i])
            if m:
                if buf or cur_heading != "(intro)":
                    sections.append((rel, cur_line, cur_level, cur_heading, "\n".join(buf)))
                cur_level, cur_heading, cur_line, buf = len(m.group(1)), m.group(2).strip(), i + 1, []
                if not title and cur_level == 1:
                    title = cur_heading
            else:
                buf.append(lines[i])
        sections.append((rel, cur_line, cur_level, cur_heading, "\n".join(buf)))
        if not title:
            title = path.stem
        scan_text = text
    elif suffix in HTML_EXTS:
        title, sections, html_hrefs, html_imgs = parse_html_doc(rel, text, path.stem)
        scan_text = "\n".join(s[4] for s in sections)  # tag-stripped — raw HTML would false-match refs
    elif suffix == ".svg":
        stripped = XML_TAG.sub(" ", text)
        tm = SVG_TITLE.search(text)
        title = " ".join(tm.group(1).split()) if tm and tm.group(1).strip() else path.stem
        sections = parse_plain_sections(rel, stripped.splitlines()) or [
            (rel, 1, 1, "(svg)", stem_words(path.stem))
        ]
        scan_text = stripped
    else:
        sections = parse_plain_sections(rel, lines)
        title = next((l.strip()[:80] for l in lines if l.strip()), path.stem)
        scan_text = text

    links, img_alts = [], []
    for m in WIKILINK.finditer(scan_text):
        links.append((rel, m.group(1).strip(), "wiki"))
    for m in MDLINK.finditer(text):
        dst = m.group(1)
        if not dst.startswith("http"):
            links.append((rel, resolve_doc_link(dst), "md"))
    if suffix == ".md":
        for m in IMGLINK.finditer(text):
            alt, dst = m.group(1), m.group(2)
            if dst.startswith(("http://", "https://", "//")):
                continue
            if Path(dst).suffix.lower() not in IMG_EXTS | {".svg"}:
                continue
            resolved = resolve_doc_link(dst)
            links.append((rel, resolved, "img"))
            if alt.strip():
                img_alts.append((rel, resolved, alt.strip()))
    for src_attr, alt in html_imgs:
        src_attr = src_attr.split("#")[0].split("?")[0]
        if not src_attr or src_attr.startswith(("http://", "https://", "//", "data:")):
            continue
        if Path(src_attr).suffix.lower() not in IMG_EXTS | {".svg"}:
            continue
        resolved = resolve_doc_link(src_attr)
        links.append((rel, resolved, "img"))
        if alt.strip():
            img_alts.append((rel, resolved, alt.strip()))
    for href in html_hrefs:
        href = href.split("#")[0].split("?")[0]
        if not href or href.startswith(("http://", "https://", "mailto:", "//")):
            continue
        if Path(href).suffix.lower() not in ({".md"} | HTML_EXTS):
            continue
        links.append((rel, resolve_doc_link(href), "md"))
    for m in set(CODEREF.findall(scan_text)):
        links.append((rel, m, "code"))
    for m in set(REQID.findall(scan_text)):
        links.append((rel, m, "req"))

    return {
        "path": rel,
        "sha": hashlib.sha256(text.encode()).hexdigest(),
        "mtime": stat_mtime,
        "title": title,
        "words": len(text.split()),
        "sections": sections,
        "links": links,
        "tags": tags,
        "img_alts": img_alts,
    }


def load_ignore_patterns(root: Path, cli_ignores=None):
    pats = [p.strip().rstrip("/") for p in (cli_ignores or []) if p.strip()]
    f = root / ".wikimapignore"
    if f.is_file():
        for ln in f.read_text(encoding="utf-8", errors="replace").splitlines():
            ln = ln.strip().rstrip("/")
            if ln and not ln.startswith("#"):
                pats.append(ln)
    return pats


def is_ignored(rel_parts, rel_posix, patterns):
    for pat in patterns:
        if "/" in pat or any(ch in pat for ch in "*?["):
            if fnmatch.fnmatch(rel_posix, pat) or fnmatch.fnmatch(rel_posix, pat + "/*"):
                return True
        elif pat in rel_parts:
            return True
    return False


def scan_files(root: Path, skipped: Counter = None, ignore_patterns=None, skip_rels=frozenset()):
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        if any(part in IGNORE_DIRS for part in rel.parts):
            continue
        if p.name in IGNORE_FILES or rel.as_posix() in skip_rels:
            continue
        if ignore_patterns and is_ignored(rel.parts, rel.as_posix(), ignore_patterns):
            continue
        if p.suffix.lower() not in INDEX_EXTS:
            if skipped is not None:
                skipped[p.suffix.lower() or "(no ext)"] += 1
            continue
        yield p


def stem_map(db):
    return {Path(p).stem.lower(): p for (p,) in db.execute("SELECT path FROM files")}


def link_stem(dst):
    # Extensionless targets can contain dots ([[wikimap-0.6.0-plan]]); Path.stem would
    # truncate at the last dot, so only strip a suffix that is a real indexable ext.
    name = Path(dst).name
    suffix = Path(name).suffix.lower()
    return (name[: -len(suffix)] if suffix in INDEX_EXTS else name).lower()


def resolve_stem(stems, dst):
    # wikilink targets may be path-style ([[insights/foo]]) — match by final segment
    return stems.get(link_stem(dst))


def sources_fresh(db, sources):
    for s in sources:
        row = db.execute("SELECT sha FROM files WHERE path=?", (s.get("path"),)).fetchone()
        if not row or row[0] != s.get("sha"):
            return False
    return True


def note_is_fresh(db, sources_json):
    return sources_fresh(db, json.loads(sources_json))


def edge_is_fresh(db, src, dst, src_sha, dst_sha):
    for path, sha in ((src, src_sha), (dst, dst_sha)):
        row = db.execute("SELECT sha FROM files WHERE path=?", (path,)).fetchone()
        if not row or row[0] != sha:
            return False
    return True


def fresh_edges(db):
    rows = db.execute(
        "SELECT src, dst, relation, rationale, origin, src_sha, dst_sha FROM edges"
    ).fetchall()
    result = {"fresh": [], "stale": []}
    for src, dst, rel, rat, origin, ss, ds in rows:
        key = "fresh" if edge_is_fresh(db, src, dst, ss, ds) else "stale"
        result[key].append((src, dst, rel, rat, origin))
    return result


def semantics_path(root: Path) -> Path:
    return root / ".wikimap" / "semantics.jsonl"


def load_semantics(root: Path):
    p = semantics_path(root)
    if not p.is_file():
        return []
    recs = []
    for ln in p.read_text(encoding="utf-8", errors="replace").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            r = json.loads(ln)
        except ValueError:
            continue  # a hand-edited bad line must not take the whole layer down
        if isinstance(r, dict) and r.get("type") in ("note", "edge"):
            recs.append(r)
    return recs


def compact_semantics(recs):
    # append-only log: the last line wins per edge key (repin appends, never rewrites)
    out, seen = [], set()
    for r in reversed(recs):
        if r["type"] == "edge":
            key = (r.get("src"), r.get("dst"), r.get("relation"))
            if key in seen:
                continue
            seen.add(key)
        out.append(r)
    out.reverse()
    return out


def write_semantics(root: Path, recs):
    p = semantics_path(root)
    p.parent.mkdir(exist_ok=True)
    tmp = p.parent / (p.name + ".tmp")
    tmp.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs), encoding="utf-8"
    )
    tmp.replace(p)


def append_semantics(root: Path, rec):
    p = semantics_path(root)
    p.parent.mkdir(exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def sync_semantics(root: Path, db):
    """notes/edges live in semantics.jsonl (the SSOT); DB tables are a derived cache."""
    p = semantics_path(root)
    if not p.is_file():
        recs = []
        for q, ins, created, src in db.execute(
            "SELECT question, insight, created, sources FROM notes ORDER BY id"
        ):
            recs.append({"type": "note", "question": q, "insight": ins,
                         "created": created, "sources": json.loads(src)})
        for s, d, rel, rat, origin, created, ss, ds in db.execute(
            "SELECT src, dst, relation, rationale, origin, created, src_sha, dst_sha "
            "FROM edges ORDER BY id"
        ):
            recs.append({"type": "edge", "src": s, "dst": d, "relation": rel,
                         "rationale": rat, "origin": origin, "created": created,
                         "src_sha": ss, "dst_sha": ds})
        if not recs:
            return
        write_semantics(root, recs)
        print(f"migrated {len(recs)} semantic records to {norm_rel(p.relative_to(root))} "
              "(file is now the source of truth; the DB is a rebuildable cache)",
              file=sys.stderr)
    sha = hashlib.sha256(p.read_bytes()).hexdigest()
    row = db.execute("SELECT value FROM meta WHERE key='semantics_sha'").fetchone()
    if row and row[0] == sha:
        return
    db.execute("DELETE FROM notes")
    db.execute("DELETE FROM edges")
    for r in compact_semantics(load_semantics(root)):
        if r["type"] == "note":
            db.execute(
                "INSERT INTO notes(question, insight, created, sources) VALUES(?,?,?,?)",
                (r.get("question", ""), r.get("insight", ""), r.get("created", ""),
                 json.dumps(r.get("sources", []), ensure_ascii=False)),
            )
        else:
            db.execute(
                "INSERT OR REPLACE INTO edges"
                "(src, dst, relation, rationale, origin, created, src_sha, dst_sha)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (r.get("src"), r.get("dst"), r.get("relation"), r.get("rationale", ""),
                 r.get("origin", ""), r.get("created", ""), r.get("src_sha"), r.get("dst_sha")),
            )
    db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('semantics_sha', ?)", (sha,))
    db.commit()


def cmd_import_graphify(root, db, args):
    data = json.loads(Path(args.graph).expanduser().read_text())
    nodes = {n["id"]: n for n in data.get("nodes", [])}
    known = {p for (p,) in db.execute("SELECT path FROM files")}

    def resolve(p):
        if not p:
            return None
        if p in known:
            return p
        if "wiki/" + p in known:
            return "wiki/" + p
        return None

    pairs, skipped = {}, 0
    for l in data.get("links", []):
        if l.get("confidence") != "INFERRED":
            continue
        s, t = nodes.get(l.get("source")), nodes.get(l.get("target"))
        sp = resolve(s.get("source_file")) if s else None
        tp = resolve(t.get("source_file")) if t else None
        if not sp or not tp or sp == tp:
            skipped += 1
            continue
        key = tuple(sorted([sp, tp]))
        info = pairs.setdefault(key, {"relations": [], "rationales": []})
        info["relations"].append(l.get("relation", "conceptually_related_to"))
        if len(info["rationales"]) < 3:
            info["rationales"].append(f'{s.get("label")} --{l.get("relation","")}→ {t.get("label")}')

    shas = {p: sha for p, sha in db.execute("SELECT path, sha FROM files")}
    existing = {(s, d, r) for s, d, r in db.execute("SELECT src, dst, relation FROM edges")}
    now = datetime.now(timezone.utc).isoformat()
    added = 0
    for (a, b), info in pairs.items():
        rel = max(set(info["relations"]), key=info["relations"].count)
        if (a, b, rel) in existing:
            continue
        append_semantics(root, {
            "type": "edge", "src": a, "dst": b, "relation": rel,
            "rationale": " | ".join(info["rationales"]), "origin": "graphify-import",
            "created": now, "src_sha": shas[a], "dst_sha": shas[b],
        })
        added += 1
    sync_semantics(root, db)
    write_map(root, db)
    print(
        f"imported {added} doc-pair edges (from {len(pairs)} pairs; "
        f"{skipped} entity-edges skipped: same-doc or unresolved path)"
    )


TOKEN = re.compile(r"[가-힣]{2,}|[A-Za-z][A-Za-z0-9_]{2,}")


def cmd_suggest(root, db, args):
    docs = {p: t for p, t in db.execute("SELECT path, title FROM files")}
    doc_terms = {p: {} for p in docs}
    for path, heading, content in db.execute("SELECT path, heading, content FROM sections"):
        tw = doc_terms.setdefault(path, {})
        for tok in TOKEN.findall(heading):
            tw[tok.lower()] = 2
        cnt = {}
        for tok in TOKEN.findall(content):
            tok = tok.lower()
            cnt[tok] = cnt.get(tok, 0) + 1
        for tok, c in cnt.items():
            if c >= 2:
                tw.setdefault(tok, 1)
    for p, t in docs.items():
        for tok in TOKEN.findall(t or ""):
            doc_terms[p][tok.lower()] = 2

    df = {}
    for tw in doc_terms.values():
        for tok in tw:
            df[tok] = df.get(tok, 0) + 1

    stems = stem_map(db)
    linked = set()
    for src, dst, kind in db.execute("SELECT src,dst,kind FROM links WHERE kind IN ('wiki','md')"):
        t = resolve_stem(stems, dst) if kind == "wiki" else dst
        if t:
            linked.add(tuple(sorted([src, t])))
    for a, b in db.execute("SELECT src, dst FROM edges"):
        linked.add(tuple(sorted([a, b])))

    scores, why = {}, {}

    doc_filter = norm_rel(args.doc) if args.doc else None

    def bump(pa, pb, amount, signal):
        key = tuple(sorted([pa, pb]))
        if key in linked:
            return
        if doc_filter and doc_filter not in key:
            return
        scores[key] = scores.get(key, 0) + amount
        w = why.setdefault(key, [])
        if len(w) < 8:
            w.append(signal)

    for tok, d in df.items():
        if not (2 <= d <= args.max_df):
            continue
        docs_with = [(p, tw[tok]) for p, tw in doc_terms.items() if tok in tw]
        for i in range(len(docs_with)):
            for j in range(i + 1, len(docs_with)):
                (pa, wa), (pb, wb) = docs_with[i], docs_with[j]
                bump(pa, pb, wa * wb / d, tok)

    ref_docs = {}
    for src, dst, kind in db.execute("SELECT src,dst,kind FROM links WHERE kind IN ('req','code')"):
        ref_docs.setdefault((kind, dst), set()).add(src)
    for (kind, ref), ds in ref_docs.items():
        if len(ds) < 2 or len(ds) > 6:
            continue
        ds = sorted(ds)
        for i in range(len(ds)):
            for j in range(i + 1, len(ds)):
                bump(ds[i], ds[j], 3 if kind == "req" else 2, ref)

    for key in scores:
        if Path(key[0]).parts[:2] != Path(key[1]).parts[:2]:
            scores[key] *= 1.3

    top = sorted(scores.items(), key=lambda x: -x[1])[: args.n]
    if args.json:
        print(json.dumps({"doc": doc_filter, "candidates": [
            {"a": a, "b": b, "score": round(s, 2), "signals": why[(a, b)][:6]}
            for (a, b), s in top
        ]}, ensure_ascii=False, indent=2))
        return
    if not top:
        print("no candidates")
        return
    if args.wikilink:
        for (a, b), s in top:
            if doc_filter and doc_filter in (a, b):
                other = b if a == doc_filter else a
                print(f"[[{Path(other).stem}]]  # {other} — {', '.join(why[(a, b)][:4])} ({s:.1f})")
            else:
                print(f"[[{Path(a).stem}]] ↔ [[{Path(b).stem}]] — {', '.join(why[(a, b)][:4])} ({s:.1f})")
        print(
            "\nPaste the genuine ones into the doc body — explicit [[links]] are readable "
            "by every vault tool; use edge add only when you can't edit the doc"
        )
        return
    for (a, b), s in top:
        print(f"({s:.1f}) {a}")
        print(f"      ↔ {b}")
        print(f"      shared signals: {', '.join(why[(a, b)][:6])}")
    print(
        "\nTo confirm: wikimap edge add --src <a> --dst <b> "
        "--relation conceptually_related_to --rationale '...'"
    )


def endpoint_shas(db, src, dst):
    shas = {}
    for p in (src, dst):
        row = db.execute("SELECT sha FROM files WHERE path=?", (p,)).fetchone()
        if not row:
            sys.exit(f"not in index (run update first?): {p}")
        shas[p] = row[0]
    return shas


def cmd_edge_add(root, db, args):
    if not args.rationale:
        sys.exit("edge add requires --rationale")
    src, dst = norm_rel(args.src), norm_rel(args.dst)
    shas = endpoint_shas(db, src, dst)
    a, b = sorted([src, dst])
    relation = args.relation or "conceptually_related_to"
    append_semantics(root, {
        "type": "edge", "src": a, "dst": b, "relation": relation,
        "rationale": args.rationale, "origin": "claude",
        "created": datetime.now(timezone.utc).isoformat(),
        "src_sha": shas[a], "dst_sha": shas[b],
    })
    sync_semantics(root, db)
    write_map(root, db)
    print(f"edge saved: {a} ↔ {b} ({relation})")


def cmd_edge_repin(root, db, args):
    src, dst = norm_rel(args.src), norm_rel(args.dst)
    a, b = sorted([src, dst])
    rows = db.execute(
        "SELECT src, dst, relation, rationale, origin, created FROM edges "
        "WHERE src=? AND dst=?" + (" AND relation=?" if args.relation else ""),
        (a, b, args.relation) if args.relation else (a, b),
    ).fetchall()
    if not rows:
        sys.exit(f"no edge between {a} and {b} (use edge add to create one)")
    shas = endpoint_shas(db, a, b)
    now = datetime.now(timezone.utc).isoformat()
    for s, d, rel, rat, origin, created in rows:
        append_semantics(root, {
            "type": "edge", "src": s, "dst": d, "relation": rel,
            "rationale": rat, "origin": origin, "created": created,
            "src_sha": shas[s], "dst_sha": shas[d], "repinned": now,
        })
    sync_semantics(root, db)
    write_map(root, db)
    print(f"repinned {len(rows)} edge(s): {a} ↔ {b} (rationale kept, shas refreshed)")


def prune_semantics(root, db, kind):
    kept, removed = [], 0
    for r in compact_semantics(load_semantics(root)):
        if r["type"] == kind == "note" and not sources_fresh(db, r.get("sources", [])):
            removed += 1
            continue
        if r["type"] == kind == "edge" and not edge_is_fresh(
            db, r.get("src"), r.get("dst"), r.get("src_sha"), r.get("dst_sha")
        ):
            removed += 1
            continue
        kept.append(r)
    write_semantics(root, kept)
    sync_semantics(root, db)
    write_map(root, db)
    return removed


def cmd_edges(root, db, args):
    r = fresh_edges(db)
    pruned = prune_semantics(root, db, "edge") if args.prune and r["stale"] else 0
    if args.json:
        recs = [
            {"src": s, "dst": d, "relation": rel, "rationale": rat, "origin": o, "fresh": True}
            for s, d, rel, rat, o in r["fresh"]
        ]
        if args.all:
            recs += [
                {"src": s, "dst": d, "relation": rel, "rationale": rat, "origin": o, "fresh": False}
                for s, d, rel, rat, o in r["stale"] if not pruned
            ]
        print(json.dumps({"edges": recs, "pruned": pruned}, ensure_ascii=False, indent=2))
        return
    for src, dst, rel, rat, origin in r["fresh"]:
        print(f"[fresh|{origin}] {src} --{rel}→ {dst}\n   {rat[:140]}")
    if args.all and not pruned:
        for src, dst, rel, rat, origin in r["stale"]:
            print(f"[STALE|{origin}] {src} --{rel}→ {dst}")
    if pruned:
        print(f"pruned {pruned} stale edges")
    elif r["stale"] and not args.all:
        print(f"({len(r['stale'])} stale edges hidden — use --all to show, --prune to delete)")


def map_setting(db):
    row = db.execute("SELECT value FROM meta WHERE key='map_path'").fetchone()
    return row[0] if row else "MAP.md"


def apply_map_flags(root, db, args):
    if getattr(args, "no_map", False):
        new = MAP_DISABLED
    elif getattr(args, "map_path", None):
        new = norm_rel(args.map_path)
    else:
        return
    prev = map_setting(db)
    if new == prev:
        return
    db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('map_path', ?)", (new,))
    old = root / prev
    if prev != MAP_DISABLED and old.is_file():
        try:
            if old.read_text(encoding="utf-8", errors="replace").startswith("# Wiki Map"):
                old.unlink()  # generated artifact only — never delete a user file
        except OSError:
            pass


def rebuild_image_sections(db):
    """Image search text = filename + every alt text that references it — alts live in
    other docs, so this runs vault-wide each update instead of per-file (images are few)."""
    alts = {}
    for dst, alt in db.execute("SELECT dst, alt FROM img_alts ORDER BY dst, alt"):
        alts.setdefault(dst, []).append(alt)
    changed = []
    for (p,) in db.execute("SELECT path FROM files").fetchall():
        if Path(p).suffix.lower() not in IMG_EXTS:
            continue
        content = " ".join(
            [Path(p).stem, stem_words(Path(p).stem)] + sorted(set(alts.get(p, [])))
        )
        row = db.execute("SELECT content FROM sections WHERE path=?", (p,)).fetchone()
        if row and row[0] == content:
            continue
        db.execute("DELETE FROM sections WHERE path=?", (p,))
        db.execute("INSERT INTO sections VALUES(?,?,?,?,?)", (p, 1, 1, "(image)", content))
        changed.append(p)
    return changed


def cmd_update(root, db, args):
    t0 = time.time()
    apply_map_flags(root, db, args)
    patterns = load_ignore_patterns(root, getattr(args, "ignore", None))
    map_rel = map_setting(db)
    skip_rels = frozenset({map_rel} if map_rel != MAP_DISABLED else ())
    skipped = Counter()
    seen, changed_rels = set(), []
    row = db.execute("SELECT value FROM meta WHERE key='pdf_name_only'").fetchone()
    pdf_no = set(json.loads(row[0])) if row else set()
    known = {p: (sha, mt) for p, sha, mt in db.execute("SELECT path, sha, mtime FROM files")}
    for p in scan_files(root, skipped, patterns, skip_rels):
        rel = p.relative_to(root).as_posix()
        seen.add(rel)
        prev = known.get(rel)
        if prev and abs(prev[1] - p.stat().st_mtime) < 1e-6:
            continue
        parsed = parse_file(root, p)
        if prev and prev[0] == parsed["sha"]:
            db.execute("UPDATE files SET mtime=? WHERE path=?", (parsed["mtime"], rel))
            continue
        db.execute("DELETE FROM sections WHERE path=?", (rel,))
        db.execute("DELETE FROM links WHERE src=?", (rel,))
        db.execute("DELETE FROM tags WHERE path=?", (rel,))
        db.execute("DELETE FROM img_alts WHERE src=?", (rel,))
        db.execute(
            "INSERT OR REPLACE INTO files VALUES(?,?,?,?,?)",
            (rel, parsed["sha"], parsed["mtime"], parsed["title"], parsed["words"]),
        )
        db.executemany("INSERT INTO sections VALUES(?,?,?,?,?)", parsed["sections"])
        db.executemany("INSERT INTO links VALUES(?,?,?)", parsed["links"])
        db.executemany("INSERT INTO tags VALUES(?,?)",
                       [(rel, t) for t in parsed.get("tags", [])])
        db.executemany("INSERT INTO img_alts VALUES(?,?,?)", parsed.get("img_alts", []))
        if rel.lower().endswith(".pdf"):
            pdf_no.add(rel) if parsed.get("pdf_name_only") else pdf_no.discard(rel)
        changed_rels.append(rel)

    deleted = set(known) - seen
    for rel in deleted:
        db.execute("DELETE FROM files WHERE path=?", (rel,))
        db.execute("DELETE FROM sections WHERE path=?", (rel,))
        db.execute("DELETE FROM links WHERE src=?", (rel,))
        db.execute("DELETE FROM tags WHERE path=?", (rel,))
        db.execute("DELETE FROM img_alts WHERE src=?", (rel,))
        pdf_no.discard(rel)
    db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('pdf_name_only', ?)",
               (json.dumps(sorted(pdf_no)),))
    img_changed = rebuild_image_sections(db)
    sync_fts(db, changed_rels + [p for p in img_changed if p not in deleted], deleted)
    db.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES('skipped', ?)",
        (json.dumps(dict(skipped.most_common())),),
    )
    db.commit()

    fresh = stale = 0
    for (src,) in db.execute("SELECT sources FROM notes"):
        if note_is_fresh(db, src):
            fresh += 1
        else:
            stale += 1

    write_map(root, db)
    e = fresh_edges(db)
    total = db.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    ms = int((time.time() - t0) * 1000)
    n_skipped = sum(skipped.values())
    top = ", ".join(f"{ext} {n}" for ext, n in skipped.most_common(3))
    map_note = "map disabled" if map_rel == MAP_DISABLED else f"{map_rel} updated"
    print(
        f"wikimap: {total} files indexed ({len(changed_rels)} changed, {len(deleted)} deleted) "
        f"in {ms}ms | skipped {n_skipped} non-indexed files"
        + (f" ({top})" if top else "")
        + (f" | pdf text-extraction failed: {len(pdf_no)} (indexed name+path only)" if pdf_no else "")
        + f" | notes: {fresh} fresh, {stale} stale | "
        f"edges: {len(e['fresh'])} fresh, {len(e['stale'])} stale | {map_note}"
    )


def backlink_counts(db):
    stems = stem_map(db)
    counts = {}
    for src, dst, kind in db.execute("SELECT src, dst, kind FROM links WHERE kind IN ('wiki','md')"):
        target = resolve_stem(stems, dst) if kind == "wiki" else dst
        if target and target != src:
            counts[target] = counts.get(target, 0) + 1
    return counts


def vault_health(db):
    stems = stem_map(db)
    known = {p for (p,) in db.execute("SELECT path FROM files")}
    connected, broken, broken_seen = set(), [], set()
    for src, dst, kind in db.execute(
        "SELECT src, dst, kind FROM links WHERE kind IN ('wiki','md','img')"
    ):
        target = resolve_stem(stems, dst) if kind == "wiki" else (dst if dst in known else None)
        if target and target != src:
            connected.add(src)
            connected.add(target)
        elif not target:
            label = f"[[{dst}]]" if kind == "wiki" else dst
            if (label, src) not in broken_seen:
                broken_seen.add((label, src))
                broken.append((label, src))
    edges = fresh_edges(db)
    for src, dst, _, _, _ in edges["fresh"]:
        connected.add(src)
        connected.add(dst)
    stale_notes = sum(
        1 for (s,) in db.execute("SELECT sources FROM notes") if not note_is_fresh(db, s)
    )
    return {
        "orphans": sorted(known - connected),
        "broken": broken,
        "stale_notes": stale_notes,
        "stale_edges": len(edges["stale"]),
    }


def write_map(root, db):
    map_rel = map_setting(db)
    if map_rel == MAP_DISABLED:
        return
    now = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
    total, words = db.execute("SELECT COUNT(*), COALESCE(SUM(words),0) FROM files").fetchone()
    out = [
        "# Wiki Map",
        "",
        f"> auto-generated by wikimap ({now}) — do not edit. Refresh: `wikimap update`",
        f"> {total} files · ~{words:,} words",
    ]
    row = db.execute("SELECT value FROM meta WHERE key='skipped'").fetchone()
    if row:
        skipped = json.loads(row[0])
        n = sum(skipped.values())
        top = " · ".join(f"{ext} {c}" for ext, c in list(skipped.items())[:4])
        out.append(
            f"> coverage: every file accounted for — {total} indexed, {n} skipped"
            + (f" ({top})" if top else "")
        )
    out += [
        "",
        "## Directories",
        "",
    ]
    dirs = {}
    for path, title in db.execute("SELECT path, title FROM files ORDER BY path"):
        d = str(Path(path).parent)
        dirs.setdefault(d, []).append(title)
    for d in sorted(dirs):
        titles = dirs[d]
        sample = " · ".join(titles[:4]) + (" …" if len(titles) > 4 else "")
        out.append(f"- `{d}/` ({len(titles)}): {sample}")

    counts = backlink_counts(db)
    hubs = sorted(counts.items(), key=lambda x: -x[1])[:10]
    if hubs:
        out += ["", "## Hubs (most backlinks)", ""]
        for path, n in hubs:
            row = db.execute("SELECT title FROM files WHERE path=?", (path,)).fetchone()
            title = row[0] if row else path
            out.append(f"- [{title}]({path}) ← {n} links")

    recent = db.execute("SELECT path, title FROM files ORDER BY mtime DESC LIMIT 10").fetchall()
    out += ["", "## Recently changed", ""]
    out += [f"- [{t}]({p})" for p, t in recent]

    tag_rows = db.execute(
        "SELECT tag, COUNT(*) c FROM tags GROUP BY tag ORDER BY c DESC, tag LIMIT 15"
    ).fetchall()
    if tag_rows:
        out += ["", "## Tags", ""]
        out += [f"- `{t}` ({c}) — `wikimap search \"tag:{t}\"`" for t, c in tag_rows]

    req_rows = db.execute(
        "SELECT dst, COUNT(DISTINCT src) c FROM links WHERE kind='req' "
        "GROUP BY dst HAVING c > 1 ORDER BY c DESC LIMIT 15"
    ).fetchall()
    if req_rows:
        out += ["", "## Cross-document requirement IDs", ""]
        out += [f"- {r} ({c} docs) — `wikimap links {r}`" for r, c in req_rows]

    edges = fresh_edges(db)
    if edges["fresh"] or edges["stale"]:
        out += ["", "## Inferred connections " + f"({len(edges['fresh'])} fresh / {len(edges['stale'])} stale)", ""]
        for src, dst, rel, _, origin in edges["fresh"][:12]:
            out.append(f"- [{Path(src).stem}]({src}) ↔ [{Path(dst).stem}]({dst}) — {rel} ({origin})")
        if len(edges["fresh"]) > 12:
            out.append(f"- … and {len(edges['fresh']) - 12} more: `wikimap edges`")

    notes = db.execute("SELECT question, sources FROM notes ORDER BY id DESC").fetchall()
    if notes:
        fresh = [q for q, s in notes if note_is_fresh(db, s)]
        out += ["", "## Semantic notes " + f"({len(fresh)} fresh / {len(notes) - len(fresh)} stale)", ""]
        out += [f"- {q}" for q in fresh[:10]]

    h = vault_health(db)
    out += ["", "## Health", ""]
    if h["orphans"]:
        sample = " · ".join(f"`{p}`" for p in h["orphans"][:5])
        more = f" · … +{len(h['orphans']) - 5}" if len(h["orphans"]) > 5 else ""
        out.append(f"- orphan docs (no links in or out): {len(h['orphans'])} — {sample}{more}")
    else:
        out.append("- orphan docs: 0")
    if h["broken"]:
        sample = " · ".join(f"`{lbl}` in {src}" for lbl, src in h["broken"][:5])
        more = f" · … +{len(h['broken']) - 5}" if len(h["broken"]) > 5 else ""
        out.append(f"- broken links (target missing): {len(h['broken'])} — {sample}{more}")
    else:
        out.append("- broken links: 0")
    out.append(
        f"- stale semantics: {h['stale_notes']} notes, {h['stale_edges']} edges"
        + (" — `wikimap notes --prune` / `wikimap edges --prune`"
           if h["stale_notes"] or h["stale_edges"] else "")
    )

    target = root / map_rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(out) + "\n", encoding="utf-8")


def candidate_paths(db, terms, titles):
    """FTS5 pre-filter: docs that can possibly satisfy every term.

    Returns None to request a full linear scan (no FTS, or a term shorter
    than the trigram minimum — pitfall: trigram cannot match <3 chars).
    """
    if not has_fts(db) or not fts_populated(db):
        return None
    paths = None
    for t in terms:
        if len(t) < 3:
            return None
        cur = {p for p in titles if t in p.lower() or t in (titles[p] or "").lower()}
        match = '"%s"' % t.replace('"', '""')
        try:
            cur |= {
                r[0]
                for r in db.execute(
                    "SELECT DISTINCT path FROM sections_fts WHERE sections_fts MATCH ?",
                    (match,),
                )
            }
        except sqlite3.OperationalError:
            return None
        paths = cur if paths is None else (paths & cur)
        if not paths:
            return paths
    return paths


QUERY_TOKEN = re.compile(r'(?:(title|path|heading|tag|type):)?(?:"([^"]+)"|([^\s"]+))')
FIELD_WEIGHT = {"title": 8, "path": 6, "heading": 5, "tag": 7, "type": 3}
TYPE_EXTS = {
    "md": {".md"},
    "html": HTML_EXTS,
    "pdf": {".pdf"},
    "image": IMG_EXTS | {".svg"},
    "text": PLAIN_EXTS,
}


def parse_query(q):
    terms = []
    for m in QUERY_TOKEN.finditer(q):
        field, phrase, word = m.group(1), m.group(2), m.group(3)
        term = (phrase if phrase is not None else word or "").lower().strip()
        if term:
            terms.append((field, term))
    return terms


def cmd_search(root, db, args):
    terms = parse_query(args.query)
    if not terms:
        sys.exit("empty query")
    for f, t in terms:
        if f == "type" and t not in TYPE_EXTS:
            sys.exit(f"unknown type: {t} (known: {', '.join(sorted(TYPE_EXTS))})")
    plain = [t for f, t in terms if f is None]

    matched_notes = []
    for q, ins, created, src in db.execute(
        "SELECT question, insight, created, sources FROM notes ORDER BY id DESC"
    ):
        hay = (q + " " + ins).lower()
        if plain and all(t in hay for t in plain) and note_is_fresh(db, src):
            matched_notes.append(
                {"question": q, "insight": ins, "created": created,
                 "sources": [s["path"] for s in json.loads(src)]}
            )
            if len(matched_notes) >= 3:
                break
    if not args.json:
        for n in matched_notes:
            print(f"[NOTE fresh {n['created'][:10]}] Q: {n['question']}\n"
                  f"  {n['insight']}\n  sources: {', '.join(n['sources'])}\n")

    titles = {p: t for p, t in db.execute("SELECT path, title FROM files")}
    doc_tags = {}
    for p, t in db.execute("SELECT path, tag FROM tags"):
        doc_tags.setdefault(p, set()).add(t)
    fts_terms = [t for f, t in terms if f in (None, "heading")]
    paths = candidate_paths(db, fts_terms, titles) if fts_terms else None
    if paths is None:
        rows = db.execute("SELECT path, line, heading, content FROM sections")
    elif not paths:
        rows = []
    else:
        rows = []
        plist = sorted(paths)
        for i in range(0, len(plist), 500):
            chunk = plist[i : i + 500]
            rows += db.execute(
                "SELECT path, line, heading, content FROM sections WHERE path IN (%s)"
                % ",".join("?" * len(chunk)),
                chunk,
            ).fetchall()

    def collect(section_rows, require_all):
        out = []
        for path, line, heading, content in section_rows:
            title_l, heading_l, content_l = titles.get(path, "").lower(), heading.lower(), content.lower()
            path_l = path.lower()
            score, ok, matched = 0, True, 0
            for f, t in terms:
                if f == "title":
                    hit = t in title_l
                elif f == "path":
                    hit = t in path_l
                elif f == "heading":
                    hit = t in heading_l
                elif f == "tag":
                    hit = any(t in tag for tag in doc_tags.get(path, ()))
                elif f == "type":
                    hit = Path(path).suffix.lower() in TYPE_EXTS[t]
                else:
                    if t in title_l or t in path_l or t in heading_l or t in content_l:
                        matched += 1
                        score += (
                            8 * (t in title_l) + 6 * (t in path_l) + 5 * (t in heading_l)
                            + min(content_l.count(t), 5)
                        )
                    elif require_all:
                        ok = False
                        break
                    continue
                # field filters are explicit constraints — hard even in partial mode
                if not hit:
                    ok = False
                    break
                score += FIELD_WEIGHT[f]
            if not ok or (plain and not matched) or (require_all and matched < len(plain)):
                continue
            # partial mode still demands a majority of terms — a single stray hit
            # (e.g. "scan" inside a path) must not surface as a result
            if not require_all and matched * 2 < len(plain):
                continue
            out.append((matched, score, path, line, heading, content))
        return out

    results = collect(rows, require_all=True)
    partial = False
    if not results and len(plain) >= 2:
        # every-term AND came up empty — relax plain terms to OR, rank by how many
        # matched (full scan: the FTS pre-filter above is AND-semantics too)
        results = collect(db.execute("SELECT path, line, heading, content FROM sections"),
                          require_all=False)
        partial = True

    results.sort(key=lambda r: (-r[0], -r[1]))
    if args.json:
        out = []
        for matched, score, path, line, heading, content in results[: args.n]:
            lines = content.splitlines()
            hits = [ln.strip() for ln in lines if any(t in ln.lower() for t in plain)]
            rec = {"path": path, "line": line, "heading": heading,
                   "score": score, "matched": hits[:3]}
            if partial:
                rec["partial"] = f"{matched}/{len(plain)}"
            if args.full:
                rec["content"] = content
            out.append(rec)
        print(json.dumps({"query": args.query, "notes": matched_notes,
                          "partial": partial, "results": out},
                         ensure_ascii=False, indent=2))
        return
    if not results and not matched_notes:
        print("no results")
        return
    for matched, score, path, line, heading, content in results[: args.n]:
        tag = f"partial {matched}/{len(plain)}, score {score}" if partial else f"score {score}"
        print(f"{path}:{line}  [{heading}]  ({tag})")
        lines = content.splitlines()
        if args.full:
            for ln in lines:
                print(f"  {ln.rstrip()}")
            continue
        hits = [i for i, ln in enumerate(lines) if any(t in ln.lower() for t in plain)]
        if args.context:
            shown = set()
            for i in hits:
                shown.update(range(max(0, i - args.context), min(len(lines), i + args.context + 1)))
            prev = None
            for j in sorted(shown):
                if prev is not None and j > prev + 1:
                    print("  ⋯")
                print(f"  {lines[j].rstrip()[:200]}")
                prev = j
        else:
            for i in hits[:3]:
                print(f"  {lines[i].strip()[:160]}")


def cmd_links(root, db, args):
    target = norm_rel(args.target)
    if REQID.fullmatch(target):
        rows = db.execute("SELECT src FROM links WHERE kind='req' AND dst=?", (target,)).fetchall()
        if args.json:
            print(json.dumps({"target": target, "kind": "req",
                              "docs": [src for (src,) in rows]}, ensure_ascii=False, indent=2))
            return
        print(f"{target} appears in {len(rows)} docs:")
        for (src,) in rows:
            print(f"  {src}")
        return

    stems = stem_map(db)
    path = (target if db.execute("SELECT 1 FROM files WHERE path=?", (target,)).fetchone()
            else resolve_stem(stems, target))
    if not path:
        sys.exit(f"not found: {target}")

    outlinks = []
    for dst, kind in db.execute("SELECT dst, kind FROM links WHERE src=? ORDER BY kind", (path,)):
        resolved = (resolve_stem(stems, dst) or dst) if kind == "wiki" else dst
        outlinks.append({"target": resolved, "kind": kind})
    backlinks, seen_back = [], set()
    for src, dst, kind in db.execute(
        "SELECT src, dst, kind FROM links WHERE kind IN ('wiki','md','img')"
    ):
        resolved = resolve_stem(stems, dst) if kind == "wiki" else dst
        if resolved == path and src not in seen_back:
            seen_back.add(src)
            backlinks.append({"source": src, "kind": kind})
    inferred = [
        {"other": (dst if src == path else src), "relation": rel,
         "origin": origin, "rationale": rat}
        for src, dst, rel, rat, origin in fresh_edges(db)["fresh"] if path in (src, dst)
    ]
    if args.json:
        print(json.dumps({"target": path, "outlinks": outlinks, "backlinks": backlinks,
                          "inferred": inferred}, ensure_ascii=False, indent=2))
        return
    print(f"== {path}")
    print("outlinks:")
    for l in outlinks:
        tag = f"linked|{l['kind']}" if l["kind"] in ("wiki", "md", "img") else l["kind"]
        print(f"  [{tag}] {l['target']}")
    print("backlinks:")
    for l in backlinks:
        print(f"  [linked|{l['kind']}] {l['source']}")
    if inferred:
        print("inferred:")
        for e in inferred:
            print(f"  [inferred|{e['relation']}|{e['origin']}] {e['other']}")
            print(f"    ∵ {e['rationale'][:120]}")


def cmd_path(root, db, args):
    stems = stem_map(db)
    known = {p for (p,) in db.execute("SELECT path FROM files")}

    def resolve(t):
        t = norm_rel(t)
        if t in known:
            return t
        return resolve_stem(stems, t)

    src, dst = resolve(args.src), resolve(args.dst)
    if not src:
        sys.exit(f"not found: {args.src}")
    if not dst:
        sys.exit(f"not found: {args.dst}")
    if src == dst:
        if args.json:
            print(json.dumps({"src": src, "dst": dst, "found": True, "hops": 0,
                              "chain": [{"path": src, "via": None}]}, ensure_ascii=False, indent=2))
        else:
            print(src)
        return

    adj = {}

    def add(a, b, label):
        adj.setdefault(a, {}).setdefault(b, label)

    for s, d, kind in db.execute(
        "SELECT src, dst, kind FROM links WHERE kind IN ('wiki','md','img')"
    ):
        t = resolve_stem(stems, d) if kind == "wiki" else d
        if t and t != s and t in known:
            add(s, t, f"—[{kind}]→")
            add(t, s, f"←[{kind}]—")
    for s, d, rel, _, origin in fresh_edges(db)["fresh"]:
        add(s, d, f"↔[{rel}|{origin}]")
        add(d, s, f"↔[{rel}|{origin}]")

    prev = {src: None}
    q = deque([src])
    while q:
        cur = q.popleft()
        if cur == dst:
            break
        for nxt in adj.get(cur, {}):
            if nxt not in prev:
                prev[nxt] = cur
                q.append(nxt)
    if dst not in prev:
        if args.json:
            print(json.dumps({"src": src, "dst": dst, "found": False, "hops": None,
                              "chain": []}, ensure_ascii=False, indent=2))
        else:
            print(f"no path: {src} ↮ {dst}")
        return
    chain = []
    cur = dst
    while cur is not None:
        chain.append(cur)
        cur = prev[cur]
    chain.reverse()
    if args.json:
        steps = [{"path": chain[0], "via": None}]
        steps += [{"path": b, "via": adj[a][b]} for a, b in zip(chain, chain[1:])]
        print(json.dumps({"src": src, "dst": dst, "found": True,
                          "hops": len(chain) - 1, "chain": steps}, ensure_ascii=False, indent=2))
        return
    print(chain[0])
    for a, b in zip(chain, chain[1:]):
        print(f"  {adj[a][b]} {b}")
    print(f"({len(chain) - 1} hops)")


def cmd_note_add(root, db, args):
    sources = []
    for p in args.sources.split(","):
        p = norm_rel(p.strip())
        row = db.execute("SELECT sha FROM files WHERE path=?", (p,)).fetchone()
        if not row:
            sys.exit(f"source not in index (run update first?): {p}")
        sources.append({"path": p, "sha": row[0]})
    append_semantics(root, {
        "type": "note", "question": args.question, "insight": args.insight,
        "created": datetime.now(timezone.utc).isoformat(), "sources": sources,
    })
    sync_semantics(root, db)
    write_map(root, db)
    print(f"note saved ({len(sources)} sources pinned)")


def cmd_notes(root, db, args):
    rows = db.execute("SELECT id, question, insight, created, sources FROM notes ORDER BY id DESC").fetchall()
    recs = [
        {"id": nid, "question": q, "insight": ins, "created": created,
         "sources": [s["path"] for s in json.loads(src)], "fresh": note_is_fresh(db, src)}
        for nid, q, ins, created, src in rows
    ]
    n_stale = sum(1 for r in recs if not r["fresh"])
    pruned = prune_semantics(root, db, "note") if args.prune and n_stale else 0
    if pruned:
        recs = [r for r in recs if r["fresh"]]
    if args.json:
        shown = recs if args.all else [r for r in recs if r["fresh"]]
        print(json.dumps({"notes": shown, "pruned": pruned}, ensure_ascii=False, indent=2))
        return
    for r in recs:
        if r["fresh"] or args.all:
            mark = "fresh" if r["fresh"] else "STALE"
            print(f"#{r['id']} [{mark}] {r['created'][:10]} Q: {r['question']}\n   {r['insight']}")
    if pruned:
        print(f"pruned {pruned} stale notes")
    elif n_stale and not args.all:
        print(f"({n_stale} stale notes hidden — use --all to show, --prune to delete)")


MDURL = re.compile(r"\]\(([^)#\s]+)\)")


def cmd_mv(root, db, args):
    old, new = norm_rel(args.old), norm_rel(args.new)
    old_abs, new_abs = root / old, root / new
    if not old_abs.is_file():
        sys.exit(f"not found: {old}")
    if new_abs.exists():
        sys.exit(f"destination exists: {new}")
    known = {p for (p,) in db.execute("SELECT path FROM files")}
    if old not in known:
        sys.exit(f"not in index (run update first?): {old}")
    stems = stem_map(db)
    old_stem, new_stem = Path(old).stem, Path(new).stem
    new_no_ext = new[: -len(Path(new).suffix)] if Path(new).suffix else new

    def resolve_from(src_rel, url):
        resolved = ((root / src_rel).parent / url).resolve()
        try:
            return resolved.relative_to(root).as_posix()
        except ValueError:
            return norm_rel(url)

    ref_srcs = set()
    for src, dst, kind in db.execute(
        "SELECT src, dst, kind FROM links WHERE kind IN ('wiki','md','img')"
    ):
        target = resolve_stem(stems, dst) if kind == "wiki" else dst
        if target == old and src != old:
            ref_srcs.add(src)

    edits = []
    for src in sorted(ref_srcs):
        p = root / src
        if not p.is_file() or p.suffix.lower() not in {".md"} | PLAIN_EXTS:
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        n = [0]

        def wiki_sub(m):
            target = m.group(1).strip()
            if resolve_stem(stems, target) == old:
                repl = new_no_ext if "/" in target else new_stem
                if repl != target:
                    n[0] += 1
                    return "[[" + repl
            return m.group(0)

        def url_sub(m):
            url = m.group(1)
            if url.startswith(("http://", "https://", "mailto:", "//")):
                return m.group(0)
            if resolve_from(src, url) == old:
                n[0] += 1
                return "](" + norm_rel(os.path.relpath(str(root / new), str(p.parent))) + ")"
            return m.group(0)

        new_text = MDURL.sub(url_sub, WIKILINK.sub(wiki_sub, text))
        if n[0]:
            edits.append((src, new_text, n[0]))

    moved_text = None
    own_n = [0]
    if old_abs.suffix.lower() in {".md"} | PLAIN_EXTS:
        text = old_abs.read_text(encoding="utf-8", errors="replace")

        def own_sub(m):
            url = m.group(1)
            if url.startswith(("http://", "https://", "mailto:", "//")):
                return m.group(0)
            target = resolve_from(old, url)
            if (root / target).exists():
                new_url = norm_rel(os.path.relpath(str(root / target), str(new_abs.parent)))
                if new_url != url:
                    own_n[0] += 1
                    return "](" + new_url + ")"
            return m.group(0)

        rewritten = MDURL.sub(own_sub, text)
        if own_n[0]:
            moved_text = rewritten

    sem = load_semantics(root)
    sem_changed = 0
    for r in sem:
        if r["type"] == "note":
            for s in r.get("sources", []):
                if s.get("path") == old:
                    s["path"] = new
                    sem_changed += 1
        elif old in (r.get("src"), r.get("dst")):
            if r["src"] == old:
                r["src"] = new
            if r["dst"] == old:
                r["dst"] = new
            if r["src"] > r["dst"]:  # edge pairs are stored sorted — keep the invariant
                r["src"], r["dst"] = r["dst"], r["src"]
                r["src_sha"], r["dst_sha"] = r.get("dst_sha"), r.get("src_sha")
            sem_changed += 1

    print(f"mv {old} → {new}")
    for src, _, n in edits:
        print(f"  rewrite {n} reference(s) in {src}")
    if own_n[0]:
        print(f"  rewrite {own_n[0]} relative link(s) inside the moved file")
    if sem_changed:
        print(f"  update {sem_changed} semantic record(s) in semantics.jsonl (shas stay valid)")
    if not args.apply:
        print("dry run — nothing written. Re-run with --apply to execute.")
        return
    for src, new_text, _ in edits:
        (root / src).write_text(new_text, encoding="utf-8")
    new_abs.parent.mkdir(parents=True, exist_ok=True)
    old_abs.rename(new_abs)
    if moved_text is not None:
        new_abs.write_text(moved_text, encoding="utf-8")
    if sem_changed:
        write_semantics(root, sem)
    sync_semantics(root, db)
    cmd_update(root, db, argparse.Namespace(ignore=[], map_path=None, no_map=False))


def cmd_fix_links(root, db, args):
    stems = stem_map(db)
    items = []
    for label, src in vault_health(db)["broken"]:
        raw = label[2:-2] if label.startswith("[[") else label
        cands = difflib.get_close_matches(
            link_stem(raw), sorted(stems.keys()), n=3, cutoff=0.6
        )
        items.append({"link": label, "in": src, "candidates": [stems[c] for c in cands]})
    if args.json:
        print(json.dumps({"broken": items}, ensure_ascii=False, indent=2))
        return
    if not items:
        print("no broken links")
        return
    for it in items:
        print(f"{it['link']}  (in {it['in']})")
        for c in it["candidates"]:
            print(f"   → {c}")
        if not it["candidates"]:
            print("   → no candidate")
    print(f"\n{len(items)} broken link(s). Suggestions only — fix by editing the doc, or use "
          "`wikimap mv` next time you relocate a file.")


def install_hook(root: Path):
    git_dir = root / ".git"
    if not git_dir.is_dir():
        sys.exit(f"not a git repository: {root} — run from inside your vault repo to install the hook")
    hooks = git_dir / "hooks"
    hooks.mkdir(exist_ok=True)
    hook = hooks / "post-commit"
    script = Path(__file__).resolve()
    line = f'python3 "{script}" --root "{root}" update || true'
    if hook.exists():
        text = hook.read_text(encoding="utf-8", errors="replace")
        if "wikimap" in text:
            print(f"hook already installed: {hook}")
            return
        hook.write_text(  # append — an existing hook is someone's workflow, never replace it
            text.rstrip("\n") + "\n\n# wikimap: keep the index fresh after every commit\n" + line + "\n",
            encoding="utf-8",
        )
        print(f"appended wikimap update to existing {hook}")
    else:
        hook.write_text(
            "#!/bin/sh\n# wikimap: keep the index fresh after every commit\n" + line + "\n",
            encoding="utf-8",
        )
        print(f"wrote {hook}")
    try:
        hook.chmod(hook.stat().st_mode | 0o755)
    except OSError:
        pass


def cmd_install(args):
    if args.hook:
        install_hook(find_root(args.root))
        return
    base = (Path.cwd() if args.project else Path.home()) / ".claude"
    dest = base / "skills" / "wikimap"
    dest.mkdir(parents=True, exist_ok=True)
    src = Path(__file__).resolve()
    target = dest / "wikimap.py"
    if src != target:
        target.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    skill = dest / "SKILL.md"
    if skill.exists():
        print(f"kept existing {skill} (customizations preserved)")
    else:
        skill.write_text(SKILL_TEMPLATE, encoding="utf-8")
        print(f"wrote {skill}")
    print(f"installed wikimap {VERSION} to {dest}")
    print(f"next: cd <your-vault> && python3 {target} update")


def main():
    # Windows consoles default to cp949/cp1252 — arrows (↔, →) would crash print()
    for stream in (sys.stdout, sys.stderr):
        if (stream.encoding or "").lower() not in ("utf-8", "utf8"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    ap = argparse.ArgumentParser(prog="wikimap")
    ap.add_argument("--root", help="vault root (default: walk up to find .wikimap, else cwd)")
    ap.add_argument("--version", action="version", version=f"wikimap {VERSION}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    ins = sub.add_parser("install", help="install as a Claude Code skill (~/.claude/skills/wikimap)")
    ins.add_argument("--project", action="store_true", help="install to ./.claude instead of ~/.claude")
    ins.add_argument("--hook", action="store_true",
                     help="install a git post-commit hook in the vault repo that runs "
                          "wikimap update (appends to an existing hook, never replaces)")

    up = sub.add_parser("update", help="incremental re-index + regenerate the map file")
    up.add_argument("--ignore", action="append", default=[], metavar="DIR_OR_GLOB",
                    help="extra exclude for this run (repeatable); persistent version: "
                         "a .wikimapignore file at the vault root, one dir/glob per line")
    up.add_argument("--map-path", dest="map_path", metavar="REL_PATH",
                    help="write the map to this vault-relative path instead of MAP.md "
                         "(persisted in the index; the old generated map is removed)")
    up.add_argument("--no-map", dest="no_map", action="store_true",
                    help="stop generating a map file (persisted; re-enable with --map-path MAP.md)")
    sub.add_parser("map", help="regenerate the map file only (honors the persisted --map-path)")

    sp = sub.add_parser("search", help="ranked section search")
    sp.add_argument("query")
    sp.add_argument("-n", type=int, default=8)
    sp.add_argument("-C", type=int, default=0, dest="context",
                    help="show N context lines around each matched line")
    sp.add_argument("--full", action="store_true", help="print the whole matched section")
    sp.add_argument("--json", action="store_true", help="structured output for agents/scripts")

    lp = sub.add_parser("links", help="outlinks/backlinks of a doc, or docs for a REQ-ID")
    lp.add_argument("target")
    lp.add_argument("--json", action="store_true", help="structured output for agents/scripts")

    mv = sub.add_parser("mv", help="move/rename a doc and rewrite every reference to it "
                                   "(dry run by default)")
    mv.add_argument("old", help="current vault-relative path")
    mv.add_argument("new", help="new vault-relative path")
    mv.add_argument("--apply", action="store_true", help="actually write (default: dry run)")

    fl = sub.add_parser("fix-links", help="suggest targets for broken links (suggestions only)")
    fl.add_argument("--json", action="store_true", help="structured output for agents/scripts")

    pp = sub.add_parser("path", help="shortest link path between two docs (BFS over links + fresh edges)")
    pp.add_argument("src")
    pp.add_argument("dst")
    pp.add_argument("--json", action="store_true", help="structured output for agents/scripts")

    np_ = sub.add_parser("note", help="save an answer-time semantic insight")
    np_.add_argument("add", choices=["add"])
    np_.add_argument("--question", required=True)
    np_.add_argument("--insight", required=True)
    np_.add_argument("--sources", required=True, help="comma-separated vault-relative paths")

    lsp = sub.add_parser("notes", help="list semantic notes")
    lsp.add_argument("--all", action="store_true")
    lsp.add_argument("--prune", action="store_true")
    lsp.add_argument("--json", action="store_true", help="structured output for agents/scripts")

    ig = sub.add_parser("import-graphify", help="import INFERRED edges from a graphify graph.json")
    ig.add_argument("graph", help="path to graph.json")

    sg = sub.add_parser("suggest", help="heuristic candidates for inferred doc connections (no LLM)")
    sg.add_argument("--doc", help="limit to pairs involving this vault-relative path")
    sg.add_argument("-n", type=int, default=10)
    sg.add_argument("--max-df", type=int, default=4, dest="max_df")
    sg.add_argument("--wikilink", action="store_true",
                    help="print candidates as paste-ready [[wikilinks]] for the doc body")
    sg.add_argument("--json", action="store_true", help="structured output for agents/scripts")

    ea = sub.add_parser("edge", help="confirm or repin an inferred connection (sha-pinned both ends)")
    ea.add_argument("action", choices=["add", "repin"],
                    help="add: save a new connection; repin: refresh the shas of an existing "
                         "one after reviewing both ends (rationale kept)")
    ea.add_argument("--src", required=True)
    ea.add_argument("--dst", required=True)
    ea.add_argument("--relation", default=None,
                    help="add: relation name (default conceptually_related_to); "
                         "repin: limit to this relation (default: all edges of the pair)")
    ea.add_argument("--rationale", default=None, help="required for add")

    le = sub.add_parser("edges", help="list inferred connections")
    le.add_argument("--all", action="store_true")
    le.add_argument("--prune", action="store_true")
    le.add_argument("--json", action="store_true", help="structured output for agents/scripts")

    args = ap.parse_args()
    if args.cmd == "install":
        cmd_install(args)
        return
    root = find_root(args.root)
    db = open_db(root)
    try:
        sync_semantics(root, db)
        if args.cmd == "update":
            cmd_update(root, db, args)
        elif args.cmd == "map":
            write_map(root, db)
            m = map_setting(db)
            print("map disabled" if m == MAP_DISABLED else f"{m} updated")
        elif args.cmd == "search":
            cmd_search(root, db, args)
        elif args.cmd == "links":
            cmd_links(root, db, args)
        elif args.cmd == "path":
            cmd_path(root, db, args)
        elif args.cmd == "mv":
            cmd_mv(root, db, args)
        elif args.cmd == "fix-links":
            cmd_fix_links(root, db, args)
        elif args.cmd == "note":
            cmd_note_add(root, db, args)
        elif args.cmd == "notes":
            cmd_notes(root, db, args)
        elif args.cmd == "import-graphify":
            cmd_import_graphify(root, db, args)
        elif args.cmd == "suggest":
            cmd_suggest(root, db, args)
        elif args.cmd == "edge":
            if args.action == "repin":
                cmd_edge_repin(root, db, args)
            else:
                cmd_edge_add(root, db, args)
        elif args.cmd == "edges":
            cmd_edges(root, db, args)
    finally:
        db.close()


if __name__ == "__main__":
    main()
