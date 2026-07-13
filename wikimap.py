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
import base64
import difflib
import fnmatch
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import sys
import time
import zlib
from collections import Counter, deque
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

VERSION = "1.0.0"

# bump when parse_file output changes shape/semantics — forces a full reparse of
# cached index.db files that would otherwise silently miss the new fields
PARSER_VERSION = "2"

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
description: Zero-LLM incremental index + lazy semantic notes for a markdown knowledge base (wiki, Obsidian vault, spec folder; plain text, HTML, PDF, and images indexed too). Use when searching vault documents ("where is the X policy/spec?"), tracing links, backlinks, or requirement IDs across documents, suggesting or inserting links between related docs, moving/renaming vault files with reference rewriting, and refreshing the index after creating or editing vault files. Do not use for code symbol search in source repositories.
---

# wikimap

Index tool for a markdown knowledge base. Principle: **eager structure, lazy semantics** — builds are deterministic parsing only (zero LLM calls, sub-second), semantic knowledge accumulates at answer time.

All commands: `__WIKIMAP__ [--root <vault>] <cmd>` (or just `wikimap <cmd>` when installed via pip)
(`--root` optional when cwd is inside the vault — the `.wikimap/` directory is auto-detected upward)

| Command | Purpose |
|---------|---------|
| `update [--ignore <dir\|glob>] [--map-path <rel> \| --no-map]` | incremental re-index + regenerate the map (sha-diff, changed files only; prints coverage: indexed vs skipped; map ends with a Health section — orphans, broken links, stale semantics). Persistent excludes: `.wikimapignore` at vault root, one dir/glob per line. `--map-path`/`--no-map` persist — use when another tool also indexes the vault root |
| `search "query" ["variant" ...] [-n 8] [-C 3 \| --full]` | ranked section search (filename/title/heading boosted; FTS5-accelerated when available); shows matched lines (≤3); `-C N` adds N context lines, `--full` prints the whole section; fresh notes surface first; CJK substring-safe. Query syntax: `"exact phrase"`, `title:x` `path:x` `heading:x` `tag:x` field filters (frontmatter `tags: [a, b]` are indexed), `type:md\|html\|pdf\|image\|text` file-type filter. Frontmatter `aliases:` match at title weight — give a doc a same-language alias to make it findable across languages. When no section matches every term, results relax to a majority-of-terms OR and are marked `partial k/n` (field filters stay hard). **Several phrasings of one question in one call are rank-fused (RRF)** into a single document ranking — a doc multiple phrasings agree on wins |
| `links <REQ-ID|filename|path>` | docs mentioning a requirement ID, or a doc's outlinks/backlinks/inferred connections — entries tagged `[linked|…]` (written by a human) vs `[inferred|…]` (confirmed guess) |
| `path <a> <b>` | shortest connection path between two docs (BFS over wiki/md links + fresh edges, both directions) |
| `note add --question "..." --insight "..." --sources a.md,b.md` | save an answer-time insight (source shas pinned) |
| `notes [--all] [--prune]` | list notes / prune stale ones |
| `suggest [--doc path] [-n 10] [--wikilink]` | heuristic candidates for unwritten doc connections (shared rare terms, requirement IDs, code refs, directory proximity, filename-token overlap — no LLM); same-directory and sibling-directory pairs are always candidates even with no shared content; JSON rows carry `dir: same\|sibling\|far`; `-n 0` = no cap (bootstrap sweeps); `--wikilink` prints paste-ready `[[links]]` for the doc body |
| `link add <doc> <target>... [--section H] [--apply]` | insert `- [[target]]` items into the doc's link-list section (reuses an existing Related/See also section, or one named with `--section` in any language, else creates `## Related`). Idempotent — an already-linked target is a no-op. Targets may be stems, aliases, or paths. Dry run without `--apply` |
| `embed set <doc> --vector <json>\|--stdin` / `embed status` | store an agent-generated embedding vector for a doc (pinned to its content sha — auto-stale when the file changes), or report coverage + which docs need (re)embedding. wikimap stores and searches vectors; the *agent* generates them — no build-time LLM, no bundled model |
| `semsearch --vector <json>\|--stdin [-n 10]` | cosine-rank docs by an agent-supplied query embedding (language-agnostic semantic search; only fresh embeddings whose sha still matches are ranked) |
| `search "<q>" --hybrid <json>\|-` | one-call hybrid: keyword ranking blended with an agent-supplied query embedding (docs found by both signals float up; semantic-only docs are spliced in). `-` reads the vector from stdin |
| `edge add --src a.md --dst b.md --relation ... --rationale "..."` | confirm a connection (both shas pinned; goes stale if either file changes) |
| `edge repin --src a.md --dst b.md` | after reviewing both ends of a stale edge: refresh the sha pins, keep the rationale |
| `edges [--all] [--prune]` | list inferred connections |
| `migrate [--apply] [--no-import]` | move a graphify vault to wikimap in one command — imports inferred edges, removes graphify artifacts, reindexes. Dry run unless `--apply` |
| `import-graphify <graph.json>` | one-time import of INFERRED edges from an existing graphify graph (`migrate` does this for you) |
| `install --hook` | git post-commit hook that runs `update` automatically (appends to an existing hook) |
| `mv <old> <new> [--apply]` | move/rename a doc AND rewrite every wikilink/md/img reference to it (dry run without `--apply`); semantics.jsonl paths updated too |
| `fix-links [--json]` | for every broken link the Health section counts: suggest close-match targets (suggestions only, never auto-applied) |

`search`/`links`/`path`/`suggest`/`notes`/`edges`/`semsearch` accept `--json` for structured output — prefer it when a script consumes the result. `search --json` carries `weak: true` when the result set is empty, fell back to a partial match, or has a low top score — the signal to try the semantic path (see rule 9). It also carries `terms: [{term, df}]` — the document frequency of each query token in this corpus. A `df: 0` term is dead vocabulary (nothing in the vault contains it): when reformulating, replace exactly those terms and keep the ones that hit.

Notes and edges live in `.wikimap/semantics.jsonl` (append-only, git-committable — the source of truth); `.wikimap/index.db` is a disposable cache rebuilt from files + that jsonl.

## Rules for the agent

1. **On a vault question**: read `MAP.md` at the vault root first, then `search` for relevant sections, then Read only those file sections. Never sweep whole files. For fact/value questions ("what is the limit/period/owner?"), retry with `-C 3` or `--full` before falling back to Read.
   **Ask in full sentences, fanned out**: a whole conversational question ("탭 토글하면 N뱃지 다시 뜨던 버그 어떻게 막았어?") searches better than hand-picked keywords — long queries are matched by information content (rare terms carry the weight; function words are ignored) and rolled up per document. For a natural-language question, pass the raw question **plus 1–2 rewrites in the vault's own vocabulary** in one call — `search "<raw question>" "<rewrite 1>" "<rewrite 2>" --json` — and the rankings are rank-fused (RRF). Never *replace* the raw question with a rewrite: the raw phrasing is a free vote and your rewrite can miss the corpus vocabulary; fusion keeps both. Rewrites that help: the document-title phrasing of the concept, the other language (Korean↔English), the technical term behind a colloquial description. Reserve `field:` filters for when you truly want to constrain.
   **Re-query before giving up**: on 0 results, a `partial` marker, or `weak: true`, look at `terms` in the JSON — replace exactly the `df: 0` (dead) tokens with synonyms/the concept behind them/the other language, keep the tokens that hit, and search once more. The index is deterministic; the reformulation is your job. If reformulation still comes up weak and an embedding index exists (`embed status`), fall to the semantic path (rule 9) — or pass your query embedding straight into `search --hybrid` to get both halves in one call.
2. **After answering**: if the answer synthesized multiple documents into a non-obvious conclusion, save it with `note add` (sources = the actual evidence files, vault-relative paths).
3. **After creating/editing/deleting vault files**: run `update` before the session ends (sub-second, zero tokens).
4. **`[NOTE fresh]` in search results**: sha-verified cache — trust and reuse it. Stale notes are hidden automatically.
5. **After creating or substantially editing a doc**: run `suggest --doc <path> -n 5 --wikilink`, read the candidates' relevant sections, and paste only the genuinely related `[[links]]` into the doc body (a "Related" line is fine) — explicit links are readable by every vault tool and survive re-indexing. Use `edge add` only when you can't edit the doc. Requirement IDs are per-document local numbers — a match across unrelated projects is a false signal; discard it.
6. **Trust tags in `links` output**: `[linked|…]` means a human wrote that connection in the source text; `[inferred|…]` means it was guessed and then confirmed (sha-verified). Weight answers accordingly.
7. **A stale edge whose connection still holds**: if it went stale only because an endpoint was edited, review both docs and run `edge repin --src a --dst b` — the rationale is kept, only the sha pins refresh. Re-add only when the relationship itself changed.
8. **Bootstrapping a link-less corpus** (docs with no links between them): ① `suggest -n 0 --json` for the full candidate list. ② Judge each pair from its titles, headings, and the shared signals only — do not read whole files; the cost cap is the point. Work through candidates by `dir` stratum: `same` first, then `sibling`, and take `far` pairs only when their score is high — measured precision drops an order of magnitude from same-directory to far pairs. ③ Apply the genuine ones with `link add <doc> <target> --apply` (batch several targets per doc). Judge honestly: shared rare terms across unrelated projects (or matching REQ-IDs from different specs) are false signals — reject them.
9. **Semantic search for natural-language questions** (the language-agnostic path, when keyword search comes up `weak` and reformulation doesn't help): keyword matching is substring-based, so a conversational question that shares no exact terms with the doc ("how is the impression flag reset?" vs a doc titled "노출 트래킹") won't match by keyword in any language. If an embedding index exists, generate a query embedding **yourself** (any embedding model — wikimap is model-agnostic) and either run `semsearch --vector <json>` for a pure semantic ranking, or `search "<q>" --hybrid <json>` to blend it with keyword hits in a single call (docs found by both signals rank highest). To make docs searchable this way, embed them once: for each doc `embed set <path> --vector <json>` with a vector you generate; `embed status` shows what's missing or stale. wikimap only stores and cosine-ranks — the vectors, and the cost, are yours and scale with what you embed, not with build time.
"""

MIGRATE_SKILL = r"""---
name: graphify-to-wikimap
description: Migrate a knowledge vault from graphify (build-time LLM knowledge graph) to wikimap (zero-LLM incremental index). Use when a vault has a graphify-out/ directory, a graphify graph.json, or graphify-specific config/rules, and the user wants to switch to wikimap. Drives `wikimap migrate` (which imports graphify's inferred edges, removes its artifacts, and reindexes — dry run by default), then switches the vault's operating rules and git config over. Do not use for the initial wikimap setup of a vault that never used graphify — that is just `wikimap update`.
---

# Migrate graphify → wikimap

graphify extracts a knowledge graph with an LLM **at build time**; wikimap indexes the same vault deterministically with **no build-time LLM**. The vault's source documents are identical for both tools — migration removes graphify's *artifacts and config*, not your content, then builds a wikimap index over the same files.

**Ground rule: never touch source content.** graphify-out/ and .graphifyignore are artifacts (delete them). A `foo-graphify-benchmark.md` the user wrote, or a `graphify-graph.png`, is content (keep it — a filename containing "graphify" does not make it an artifact).

## The migration itself is one command

```bash
wikimap migrate            # dry run: prints exactly what it will remove and import
wikimap migrate --apply    # execute
```

`migrate` imports graphify's INFERRED edges **before** deleting `graph.json` (reverse that order by hand and the connections are lost for good), removes `graphify-out/` and `.graphifyignore`, then reindexes and writes `MAP.md`. Source documents are never modified. Imported edges are pinned to both endpoints' content hashes, so each goes stale automatically when either file changes — a freshness guarantee graphify's own graph lacks.

**Show the user the dry run first.** It lists the delete set; confirm before `--apply`. If the user wants a clean break with no imported edges, use `--apply --no-import` (`suggest` can regenerate candidates later, deterministically and free).

## What the command cannot do for you

1. **Switch the operating rules.** graphify vaults usually have rules pointing agents at graphify — in a `CLAUDE.md`, `AGENTS.md`, an editor hook, or a skill file. Find and update them:
   - navigation order: read `MAP.md` first, then `wikimap search "<q>" --json`, then open the doc — replacing "read graphify-out/GRAPH_REPORT.md first".
   - after editing vault files, run `wikimap update` (not a graphify rebuild).
   - if a rule triggered on `graphify-out/graph.json` existing, retrigger it on `MAP.md` existing instead.
   `wikimap install --agents-md` drops a maintained usage block into `AGENTS.md` idempotently.

2. **Clean up git.** If the artifacts were tracked, `git rm -r --cached graphify-out` and drop any `graphify-out/cache/` lines from `.gitignore`.

3. **Set the storage model** so nothing important gets gitignored by accident: `.wikimap/index.db` is a disposable cache (safe to gitignore — `update` rebuilds it), while `.wikimap/semantics.jsonl` is the source of truth for notes and edges (commit it).

## Reversibility

Source documents are never modified. To undo the wikimap index entirely, delete `.wikimap/` and `MAP.md` — no trace remains. To undo just the edge import, remove the `origin: graphify-import` lines from `.wikimap/semantics.jsonl` and run `wikimap update`.
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
# any-script letter runs (no script whitelist) — binary noise decoded as latin-1
# yields valid "letters" (é ñ ÿ), so the wordish ratio gates stay the real defense
PDF_WORD = re.compile(r"[^\W\d_]{2,}")


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
        CREATE TABLE IF NOT EXISTS aliases(path TEXT, alias TEXT);
        CREATE INDEX IF NOT EXISTS idx_aliases_path ON aliases(path);
        CREATE TABLE IF NOT EXISTS img_alts(src TEXT, dst TEXT, alt TEXT);
        CREATE INDEX IF NOT EXISTS idx_img_alts_src ON img_alts(src);
        CREATE TABLE IF NOT EXISTS embeds(path TEXT PRIMARY KEY, sha TEXT, vec TEXT);
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
        pending = None
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                end = i + 1
                break
            m = re.match(r"^(\w[\w-]*):\s*(.*)$", lines[i])
            if m:
                key, val = m.group(1).lower(), m.group(2).strip().strip("\"'")
                meta[key] = val
                pending = key if not val else None
                continue
            lm = re.match(r"^\s*-\s+(.*)$", lines[i]) if pending else None
            if lm:
                # YAML block list ("aliases:\n  - x") — join into the same
                # comma form as the inline "[a, b]" syntax
                item = lm.group(1).strip()
                meta[pending] = (meta[pending] + ", " if meta[pending] else "") + item
            else:
                pending = None
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


def _pdf_unescape(raw: bytes) -> bytes:
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
    return bytes(out)


def _pdf_str(raw: bytes) -> str:
    b = _pdf_unescape(raw)
    if b[:2] == b"\xfe\xff":
        return b[2:].decode("utf-16-be", errors="replace")
    return b.decode("latin-1", errors="replace")


def _pdf_wordish(s: str) -> bool:
    s = s.strip()
    if len(s) < 2:
        return False
    good = sum(1 for ch in s if ch.isalnum() or ch.isspace() or ch in ".,;:!?%()·-–—'\"/&+@")
    return good / len(s) >= 0.8


PDF_OBJ = re.compile(rb"(\d+)\s+\d+\s+obj\b(.*?)endobj", re.DOTALL)
PDF_HEXSTR = re.compile(rb"<([0-9A-Fa-f]+)>")


def _hexbytes(h: bytes) -> bytes:
    h = bytes(h).translate(None, b" \t\r\n\v\f")
    if len(h) % 2:
        h += b"0"
    return bytes.fromhex(h.decode("ascii"))


def _balanced(buf, i, open_b, close_b):
    depth, j, L = 0, i, len(buf)
    lo, lc = len(open_b), len(close_b)
    while j < L:
        if buf[j : j + lo] == open_b:
            depth += 1
            j += lo
        elif buf[j : j + lc] == close_b:
            depth -= 1
            j += lc
            if depth == 0:
                return buf[i + lo : j - lc], j
        else:
            j += 1
    return buf[i + lo :], L


def _pdf_value(buf, key):
    """Raw value after /Key in a dict body: ('ref', n) | ('dict' | 'array' | 'name', bytes) | None."""
    m = re.search(rb"/" + re.escape(key) + rb"(?![A-Za-z0-9.#_-])\s*", buf)
    if not m:
        return None
    j = m.end()
    if buf[j : j + 2] == b"<<":
        return "dict", _balanced(buf, j, b"<<", b">>")[0]
    if buf[j : j + 1] == b"[":
        return "array", _balanced(buf, j, b"[", b"]")[0]
    mr = re.match(rb"(\d+)\s+\d+\s+R\b", buf[j:])
    if mr:
        return "ref", int(mr.group(1))
    mn = re.match(rb"/([^\s/<>\[\]()]+)", buf[j:])
    if mn:
        return "name", mn.group(1)
    return None


def _pdf_dict(objs, val):
    if val is None:
        return None
    kind, v = val
    if kind == "dict":
        return v
    if kind == "ref":
        body = objs.get(v)
        if body is None:
            return None
        m = re.search(rb"<<", body)
        return _balanced(body, m.start(), b"<<", b">>")[0] if m else None
    return None


def _pdf_stream_data(objbody):
    m = PDF_STREAM.search(objbody)
    if not m:
        return None
    data = m.group(1)
    fv = _pdf_value(objbody, b"Filter")
    names = []
    if fv:
        kind, v = fv
        names = [v] if kind == "name" else re.findall(rb"/([^\s/<>\[\]()]+)", v) if kind == "array" else []
    if not names:
        try:
            return zlib.decompress(data)
        except Exception:
            return data
    for name in names:
        try:
            if name == b"FlateDecode":
                data = zlib.decompress(data)
            elif name == b"ASCII85Decode":
                s = bytes(data).translate(None, b" \t\r\n\v\f")
                if s.startswith(b"<~"):
                    s = s[2:]
                if s.endswith(b"~>"):
                    s = s[:-2]
                data = base64.a85decode(s)
            else:
                return None  # DCTDecode and friends carry no text
        except Exception:
            return None
    return data


def _pdf_objects(data):
    objs = {int(m.group(1)): m.group(2) for m in PDF_OBJ.finditer(data)}
    # object streams hold dict-only objects (page/font dicts in modern writers)
    for body in [b for b in objs.values() if re.search(rb"/Type\s*/ObjStm\b", b)]:
        payload = _pdf_stream_data(body)
        if payload is None:
            continue
        fm = re.search(rb"/First\s+(\d+)", body)
        nm = re.search(rb"/N\s+(\d+)", body)
        if not fm or not nm:
            continue
        first, n = int(fm.group(1)), int(nm.group(1))
        header = payload[:first].split()
        if len(header) < 2 * n:
            continue
        try:
            pairs = [(int(header[2 * i]), int(header[2 * i + 1])) for i in range(n)]
        except ValueError:
            continue
        for i, (onum, off) in enumerate(pairs):
            end = pairs[i + 1][1] if i + 1 < len(pairs) else len(payload) - first
            objs.setdefault(onum, payload[first + off : first + end])
    return objs


def _parse_tounicode(data):
    """ToUnicode CMap stream → ({code bytes: text}, code lengths ascending), or None."""
    cmap, lens = {}, set()
    for m in re.finditer(rb"begincodespacerange(.*?)endcodespacerange", data, re.DOTALL):
        for h in PDF_HEXSTR.finditer(m.group(1)):
            lens.add(len(_hexbytes(h.group(1))))
    for m in re.finditer(rb"beginbfchar(.*?)endbfchar", data, re.DOTALL):
        toks = PDF_HEXSTR.findall(m.group(1))
        for i in range(0, len(toks) - 1, 2):
            src = _hexbytes(toks[i])
            cmap[src] = _hexbytes(toks[i + 1]).decode("utf-16-be", errors="ignore")
            lens.add(len(src))
    for m in re.finditer(rb"beginbfrange(.*?)endbfrange", data, re.DOTALL):
        toks = list(re.finditer(rb"<([0-9A-Fa-f]+)>|(\[)|(\])", m.group(1)))
        k = 0
        while k + 2 < len(toks):  # an entry needs lo, hi, dst
            if not (toks[k].group(1) and toks[k + 1].group(1)):
                k += 1
                continue
            lo_b = _hexbytes(toks[k].group(1))
            lo = int.from_bytes(lo_b, "big")
            hi = int.from_bytes(_hexbytes(toks[k + 1].group(1)), "big")
            lens.add(len(lo_b))
            k += 2
            if hi - lo > 0xFFFF:
                k += 1
                continue
            if toks[k].group(2):  # [ <dst> <dst> ... ] — one destination per code
                k += 1
                code = lo
                while k < len(toks) and not toks[k].group(3):
                    if toks[k].group(1):
                        cmap[code.to_bytes(len(lo_b), "big")] = _hexbytes(
                            toks[k].group(1)).decode("utf-16-be", errors="ignore")
                    code += 1
                    k += 1
                k += 1
            elif toks[k].group(1):
                dst = _hexbytes(toks[k].group(1))
                base = int.from_bytes(dst, "big")
                for code in range(lo, hi + 1):
                    cmap[code.to_bytes(len(lo_b), "big")] = (
                        base + code - lo).to_bytes(len(dst), "big").decode("utf-16-be", errors="ignore")
                k += 1
    return (cmap, sorted(lens)) if cmap else None


def _cmap_decode(bs, cmap, lens):
    out, i, L = [], 0, len(bs)
    while i < L:
        for l in lens:
            piece = bs[i : i + l]
            if piece in cmap:
                out.append(cmap[piece])
                i += l
                break
        else:
            i += lens[0]  # unmapped code (subset font gap) — skip, stay aligned
    return "".join(out)


def _font_cmaps(objs, res_body, cache):
    """Resources dict → {font resource name: parsed ToUnicode CMap or None}."""
    fonts = {}
    fdict = _pdf_dict(objs, _pdf_value(res_body, b"Font")) if res_body is not None else None
    if fdict is None:
        return fonts
    for m in re.finditer(rb"/([^\s/<>\[\]()]+)\s+(\d+)\s+\d+\s+R", fdict):
        name, fnum = m.group(1), int(m.group(2))
        if fnum not in cache:
            cache[fnum] = None
            fobj = objs.get(fnum)
            tu = _pdf_value(fobj, b"ToUnicode") if fobj is not None else None
            if tu and tu[0] == "ref" and objs.get(tu[1]) is not None:
                stream = _pdf_stream_data(objs[tu[1]])
                if stream:
                    cache[fnum] = _parse_tounicode(stream)
        fonts[name] = cache[fnum]
    return fonts


def _pdf_text_jobs(objs):
    """(content stream, font map) per page and per reachable Form XObject."""
    jobs, cache, seen = [], {}, set()

    def add_xobjects(res_body, inherited, depth):
        if res_body is None or depth > 3:
            return
        xdict = _pdf_dict(objs, _pdf_value(res_body, b"XObject"))
        if xdict is None:
            return
        for m in re.finditer(rb"/[^\s/<>\[\]()]+\s+(\d+)\s+\d+\s+R", xdict):
            xnum = int(m.group(1))
            if xnum in seen:
                continue
            seen.add(xnum)
            xobj = objs.get(xnum)
            if xobj is None or not re.search(rb"/Subtype\s*/Form\b", xobj):
                continue
            xres = _pdf_dict(objs, _pdf_value(xobj, b"Resources"))
            fonts = _font_cmaps(objs, xres, cache) or inherited
            data = _pdf_stream_data(xobj)
            if data:
                jobs.append((data, fonts))
            add_xobjects(xres, fonts, depth + 1)

    for _, body in sorted(objs.items()):
        if not re.search(rb"/Type\s*/Page\b", body):
            continue
        res = _pdf_dict(objs, _pdf_value(body, b"Resources"))
        fonts = _font_cmaps(objs, res, cache)
        cv = _pdf_value(body, b"Contents")
        crefs = ([cv[1]] if cv and cv[0] == "ref"
                 else [int(g) for g in re.findall(rb"(\d+)\s+\d+\s+R", cv[1])] if cv and cv[0] == "array"
                 else [])
        for cn in crefs:
            data = _pdf_stream_data(objs[cn]) if objs.get(cn) is not None else None
            if data:
                jobs.append((data, fonts))
        add_xobjects(res, fonts, 1)
    return jobs


PDF_OPS = re.compile(
    rb"/([^\s/<>\[\]()]+)\s+[-\d.]+\s+Tf"
    rb"|\(((?:\\.|[^\\()])*)\)\s*(?:Tj|'|\")"
    rb"|<([0-9A-Fa-f\s]*)>\s*(?:Tj|'|\")"
    rb"|\[((?:\\.|[^\\\]])*)\]\s*TJ"
    rb"|(ET)",
    re.DOTALL,
)
PDF_TJ_ITEM = re.compile(rb"\(((?:\\.|[^\\()])*)\)|<([0-9A-Fa-f\s]*)>", re.DOTALL)


def _decode_content(buf, fonts, emit_plain, parts):
    """Walk one content stream in operator order, decoding shown text through the
    font selected by the last Tf. Fonts without a ToUnicode CMap fall back to the
    wordish-gated literal path (0.7.0 behavior)."""
    cur = None

    def show(lit, hexs):
        if cur:
            cmap, lens = cur
            bs = _pdf_unescape(lit) if lit is not None else _hexbytes(hexs)
            s = _cmap_decode(bs, cmap, lens)
            if s:
                parts.append(s)
                # per-glyph writers (Keynote) emit one show op per character with
                # real space glyphs — separating those would shatter every word;
                # multi-char runs get a separator since theirs may be positional
                if len(s) > 1:
                    parts.append(" ")
        elif lit is not None:
            emit_plain(lit)
            parts.append(" ")

    for m in PDF_OPS.finditer(buf):
        fname, lit, hexs, arr, et = m.groups()
        if fname is not None:
            cur = fonts.get(fname)
        elif et is not None:
            parts.append("\n")
        elif arr is not None:
            for im in PDF_TJ_ITEM.finditer(arr):
                show(im.group(1), im.group(2))
        else:
            show(lit, hexs)


def extract_pdf_text(data: bytes):
    """Deterministic ladder: per-font ToUnicode CMap decoding (Page→Resources→Font
    chain, Form XObjects included) → raw literal-string harvest of every stream →
    name-only. Each rung is wordish-gated so image/binary streams never leak.

    Returns (text, title, ok, pages) — pages is per-content-stream text when the
    CMap rung wins (a slide/page is the natural search section), else None."""

    def clean(parts):
        text = "".join(ch for ch in "".join(parts) if ch.isprintable() or ch in "\n ")
        return re.sub(r"[ \t]{2,}", " ", text)

    def emit_into(parts):
        def emit(raw):
            s = _pdf_str(raw)
            if _pdf_wordish(s):
                parts.append(s)
        return emit

    try:
        jobs = _pdf_text_jobs(_pdf_objects(data))
    except Exception:
        jobs = []  # malformed object graph — the raw harvest rung still runs
    pages = []
    for buf, fonts in jobs:
        parts = []
        _decode_content(buf, fonts, emit_into(parts), parts)
        page = clean(parts).strip("\n")
        if page.strip():
            pages.append(page)
    text = "\n".join(pages)
    ok = len(PDF_WORD.findall(text)) >= 5
    if not (ok and _pdf_wordish(text)):
        pages, parts = None, []
        emit = emit_into(parts)

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
        text = clean(parts)
        ok = len(PDF_WORD.findall(text)) >= 5

    title = ""
    tm = PDF_TITLE.search(data)
    if tm:
        title = " ".join(_pdf_str(tm.group(1)).split())
        if not _pdf_wordish(title):
            title = ""
    return (text, title, ok, pages) if ok else ("", title, ok, None)


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
        text, pdf_title, ok, pages = extract_pdf_text(data)
        if ok and pages:
            sections, start = [], 1
            for pg in pages:
                pg_lines = pg.splitlines()
                heading = next((l.strip()[:60] for l in pg_lines if l.strip()), "(page)")
                sections.append((rel, start, 1, heading, pg))
                start += len(pg_lines)
        elif ok:
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
    html_hrefs, html_imgs, tags, aliases = [], [], [], []

    if suffix == ".md":
        meta, body_start = parse_frontmatter(lines)
        title = meta.get("title") or ""
        tags = parse_frontmatter_tags(meta.get("tags", ""))
        aliases = parse_frontmatter_tags(meta.get("aliases", ""))
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
        "aliases": aliases,
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
    # aliases first so a real file stem always wins a name collision
    stems = {a.lower(): p for p, a in db.execute("SELECT path, alias FROM aliases")}
    for (p,) in db.execute("SELECT path FROM files"):
        stems[Path(p).stem.lower()] = p
    return stems


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
        # any record with a "type" is kept, even one this build doesn't understand: a newer
        # wikimap may write kinds we predate, and rewriters (prune, mv) round-trip this list
        # back to the SSOT file — dropping them here would delete the newer build's data
        if isinstance(r, dict) and isinstance(r.get("type"), str):
            recs.append(r)
    return recs


def known_semantics(recs):
    return [r for r in recs if r["type"] in ("note", "edge", "embed")]


def compact_semantics(recs):
    # append-only log: the last line wins per edge key (repin appends, never rewrites)
    out, seen = [], set()
    for r in reversed(recs):
        if r["type"] == "edge":
            key = ("edge", r.get("src"), r.get("dst"), r.get("relation"))
            if key in seen:
                continue
            seen.add(key)
        elif r["type"] == "embed":
            key = ("embed", r.get("path"))
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
    db.execute("DELETE FROM embeds")
    for r in known_semantics(compact_semantics(load_semantics(root))):
        if r["type"] == "note":
            db.execute(
                "INSERT INTO notes(question, insight, created, sources) VALUES(?,?,?,?)",
                (r.get("question", ""), r.get("insight", ""), r.get("created", ""),
                 json.dumps(r.get("sources", []), ensure_ascii=False)),
            )
        elif r["type"] == "embed":
            db.execute(
                "INSERT OR REPLACE INTO embeds(path, sha, vec) VALUES(?,?,?)",
                (r.get("path"), r.get("sha"),
                 json.dumps(r.get("vec", []), ensure_ascii=False)),
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


def import_graphify_edges(root, db, graph_path):
    data = json.loads(Path(graph_path).expanduser().read_text())
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
    return added, len(pairs), skipped


def cmd_import_graphify(root, db, args):
    added, pairs, skipped = import_graphify_edges(root, db, args.graph)
    print(
        f"imported {added} doc-pair edges (from {pairs} pairs; "
        f"{skipped} entity-edges skipped: same-doc or unresolved path)"
    )


TOKEN = re.compile(r"[^\W\d_]\w+")


def name_tokens(path):
    return {t.lower() for t in re.split(r"[-_ .]", Path(path).stem) if len(t) >= 2}


def structure_words(paths, ratio=0.06):
    """Filename tokens common enough to be this vault's structure vocabulary
    (e.g. 'policy', '정책', 'spec') rather than content. Derived from the corpus,
    not hardcoded — every vault has different conventions."""
    df = {}
    for p in paths:
        for t in name_tokens(p):
            df[t] = df.get(t, 0) + 1
    cutoff = max(2, len(paths) * ratio)
    return {t for t, c in df.items() if c >= cutoff}


def minimal_variants(variants):
    """The subset of variants sufficient for a boolean substring hit-test: if a
    kept variant is itself a substring of another, the longer one can never match
    without the shorter also matching, so it is dropped. Score counting must keep
    the full set — only any()-style checks may use this."""
    out = []
    for v in sorted(variants, key=len):
        if not any(m in v for m in out):
            out.append(v)
    return out


def doc_haystacks(db):
    hays = {}
    for path, title in db.execute("SELECT path, title FROM files"):
        hays[path] = [(title or "").lower() + " " + path.lower()]
    for path, heading, content in db.execute("SELECT path, heading, content FROM sections"):
        if path in hays:
            hays[path].append(heading.lower())
            hays[path].append(content.lower())
    return {p: " ".join(parts) for p, parts in hays.items()}


def query_idf(db, terms, hays=None):
    """log(ndocs/df) per term over the corpus's searchable text — high for rare
    content words, ~0 for function words that appear almost everywhere. Purely
    corpus-derived (no hardcoded stoplist), so it stays language-agnostic.
    Also returns per-doc hit sets so search can skip terms a doc can't match."""
    if not terms:
        return {}, 0, {}, {}
    if hays is None:
        hays = doc_haystacks(db)
    df = {t: 0 for t in terms}
    variants = {t: minimal_variants(term_variants(t)) for t in terms}
    # a token repeated in the query has always been df-counted once per
    # occurrence; keep that so rankings stay byte-identical
    counts = {}
    for t in terms:
        counts[t] = counts.get(t, 0) + 1
    doc_hits = {}
    for path, hay in hays.items():
        hits = {t for t in counts if any(v in hay for v in variants[t])}
        if hits:
            doc_hits[path] = hits
            for t in hits:
                df[t] += counts[t]
    ndocs = max(len(hays), 1)
    idf = {t: math.log(ndocs / df[t]) if df[t] else math.log(ndocs) for t in terms}
    return idf, sum(idf.values()), df, doc_hits


def dir_proximity(a, b):
    pa, pb = Path(a).parent, Path(b).parent
    if pa == pb:
        return "same"
    if pa.parent == pb.parent:
        return "sibling"
    return "far"


def cmd_suggest(root, db, args):
    prose_exts = {".md", ".pdf"} | PLAIN_EXTS | HTML_EXTS
    docs = {p: t for p, t in db.execute("SELECT path, title FROM files")
            if Path(p).suffix.lower() in prose_exts}
    doc_terms = {p: {} for p in docs}
    for path, heading, content in db.execute("SELECT path, heading, content FROM sections"):
        if path not in doc_terms:
            continue
        tw = doc_terms[path]
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

    by_parent = {}
    for p in docs:
        if Path(p).suffix.lower() != ".pdf":
            by_parent.setdefault(str(Path(p).parent.parent), []).append(p)
    for group in by_parent.values():
        group = sorted(group)
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = group[i], group[j]
                if dir_proximity(a, b) == "far":
                    continue
                key = (a, b)
                if key in linked or (doc_filter and doc_filter not in key):
                    continue
                scores.setdefault(key, 0.0)
                why.setdefault(key, [])

    ntoks = {p: name_tokens(p) for p in docs}
    ndf = {}
    for ts in ntoks.values():
        for t in ts:
            ndf[t] = ndf.get(t, 0) + 1
    ndocs = len(docs) or 1
    for key, s in scores.items():
        a, b = key
        prox = dir_proximity(a, b)
        s += 4.0 if prox == "same" else 2.0 if prox == "sibling" else 0.0
        shared = ntoks.get(a, set()) & ntoks.get(b, set())
        s += 1.5 * sum(math.log(ndocs / ndf[t]) for t in shared)
        scores[key] = s
        w = why[key]
        for t in sorted(shared, key=lambda t: ndf[t])[:2]:
            if len(w) < 8:
                w.append(f"name:{t}")
        if prox != "far" and len(w) < 8:
            w.append(f"dir:{prox}")

    top = sorted(scores.items(), key=lambda x: -x[1])[: args.n or None]
    if args.json:
        print(json.dumps({"doc": doc_filter, "candidates": [
            {"a": a, "b": b, "score": round(s, 2), "dir": dir_proximity(a, b),
             "signals": why[(a, b)][:6]}
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
    row = db.execute("SELECT value FROM meta WHERE key='parser_version'").fetchone()
    if (row[0] if row else None) != PARSER_VERSION:
        # cached rows predate the current parser — they'd silently miss new fields
        for t in ("files", "sections", "links", "tags", "aliases", "img_alts"):
            db.execute(f"DELETE FROM {t}")
        if has_fts(db):
            db.execute("DELETE FROM sections_fts")
        db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('parser_version', ?)",
                   (PARSER_VERSION,))
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
        db.execute("DELETE FROM aliases WHERE path=?", (rel,))
        db.execute("DELETE FROM img_alts WHERE src=?", (rel,))
        db.execute(
            "INSERT OR REPLACE INTO files VALUES(?,?,?,?,?)",
            (rel, parsed["sha"], parsed["mtime"], parsed["title"], parsed["words"]),
        )
        db.executemany("INSERT INTO sections VALUES(?,?,?,?,?)", parsed["sections"])
        db.executemany("INSERT INTO links VALUES(?,?,?)", parsed["links"])
        db.executemany("INSERT INTO tags VALUES(?,?)",
                       [(rel, t) for t in parsed.get("tags", [])])
        db.executemany("INSERT INTO aliases VALUES(?,?)",
                       [(rel, a) for a in parsed.get("aliases", [])])
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
        db.execute("DELETE FROM aliases WHERE path=?", (rel,))
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


def candidate_paths(db, terms, titles, doc_aliases):
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
        cur = {
            p for p in titles
            if t in p.lower() or t in (titles[p] or "").lower()
            or any(t in a for a in doc_aliases.get(p, ()))
        }
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
WEAK_SCORE = 10
LONG_QUERY = 6
COVER_RATIO = 0.3
RRF_K = 60
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


LATIN_RUN = re.compile(r"[a-z0-9]{2,}")


def term_variants(term):
    """Substrings to try when matching a query token against content — the token
    carries surface morphology (particles/inflection) the doc doesn't: 'core:ui로'
    → 'core','ui'; 'designsystem에' → 'designsystem'; '컴포넌트들' → prefixes down
    to len 2. Language-agnostic: latin/digit runs cover embedded technical terms,
    trailing-char trimming covers agglutinative scripts without any grammar table —
    only prefixes that actually occur in the corpus survive the match test."""
    # a quoted/multi-word phrase is an exact-order constraint — never loosen it
    if " " in term:
        return {term}
    vs = {term}
    latin = LATIN_RUN.findall(term)
    vs.update(latin)
    # trim up to 3 trailing chars so a particle/inflection-suffixed token can match
    # its bare stem ('컴포넌트들'→'컴포넌트', '옮기고'→'옮기'); bounded to short tails
    # to avoid 2-char prefixes of long tokens matching noise. Skip clean latin runs.
    if not (len(latin) == 1 and latin[0] == term):
        for i in range(1, 4):
            if len(term) - i >= 2:
                vs.add(term[: len(term) - i])
    return {v for v in vs if len(v) >= 2}


def cmd_search(root, db, args):
    queries = args.query

    titles = {p: t for p, t in db.execute("SELECT path, title FROM files")}
    doc_tags = {}
    for p, t in db.execute("SELECT path, tag FROM tags"):
        doc_tags.setdefault(p, set()).add(t)
    doc_aliases = {}
    for p, a in db.execute("SELECT path, alias FROM aliases"):
        doc_aliases.setdefault(p, []).append(a.lower())

    full_scan = []

    def all_sections():
        if not full_scan:
            full_scan.append(
                db.execute("SELECT path, line, heading, content FROM sections").fetchall())
        return full_scan[0]

    hay_cache = []

    def doc_hays():
        if not hay_cache:
            hay_cache.append(doc_haystacks(db))
        return hay_cache[0]

    def collect(section_rows, require_all, terms, plain, idf, total_idf, long_query, pvariants, doc_hits, hitvs):
        # roll matches up to the DOCUMENT: plain terms scattered across several
        # sections of one doc are unioned, so a doc that mentions each query term
        # once (in different sections) still clears the coverage gate. The single
        # best-scoring section is kept for display.
        docs = {}
        # title/alias/path hits are doc-level facts — compute once per doc, and
        # only for terms the doc-level prefilter says can match at all
        field_cache = {}
        for path, line, heading, content in section_rows:
            fc = field_cache.get(path)
            if fc is None:
                title_l = titles.get(path, "").lower()
                path_l = path.lower()
                al = doc_aliases.get(path, ())
                pfield = {}
                for t in doc_hits.get(path, ()):
                    vs = hitvs[t]
                    pfield[t] = (
                        any(v in title_l for v in vs),
                        any(v in a for v in vs for a in al),
                        any(v in path_l for v in vs),
                    )
                fc = field_cache[path] = (title_l, path_l, al, pfield)
            title_l, path_l, al, pfield = fc
            heading_l, content_l = heading.lower(), content.lower()
            sec_score, ok, sec_terms = 0, True, set()
            for f, t in terms:
                if f == "title":
                    hit = t in title_l or any(t in a for a in al)
                elif f == "path":
                    hit = t in path_l
                elif f == "heading":
                    hit = t in heading_l
                elif f == "tag":
                    hit = any(t in tag for tag in doc_tags.get(path, ()))
                elif f == "type":
                    hit = Path(path).suffix.lower() in TYPE_EXTS[t]
                else:
                    doc_field = pfield.get(t)
                    if doc_field is None:
                        continue
                    in_title, alias_hit, in_path = doc_field
                    vs = hitvs[t]
                    in_head = any(v in heading_l for v in vs)
                    in_body = any(v in content_l for v in vs)
                    if in_title or alias_hit or in_path or in_head or in_body:
                        sec_terms.add(t)
                        w = 1 + idf.get(t, 0)
                        sec_score += w * (
                            8 * (in_title or alias_hit) + 6 * in_path
                            + 5 * in_head + min(sum(content_l.count(v) for v in pvariants[t]), 5)
                        )
                    continue
                # field filters are explicit constraints — hard even in partial mode.
                # they're evaluated per section: a section that fails is skipped, but
                # another section of the same doc may still satisfy the constraint
                # (heading:X only ever matches the section whose heading is X).
                if not hit:
                    ok = False
                    break
                sec_score += FIELD_WEIGHT[f]
            if not ok:
                continue
            d = docs.get(path)
            if d is None:
                docs[path] = [set(sec_terms), sec_score, line, heading, content]
            else:
                d[0] |= sec_terms
                if sec_score > d[1]:
                    d[1], d[2], d[3], d[4] = sec_score, line, heading, content

        out = []
        for path, (mterms, score, line, heading, content) in docs.items():
            if plain and not mterms:
                continue
            if require_all and len(mterms) < len(plain):
                continue
            matched_idf = sum(idf.get(t, 0) for t in mterms)
            top_idf = max((idf.get(t, 0) for t in plain), default=0)
            if not require_all and plain:
                if long_query:
                    # conversational query: function words dilute any count-based
                    # coverage, so gate on idf mass — a doc passes if its matched
                    # terms carry a majority of the query's information, or if it
                    # matches the single most distinctive term outright.
                    covers = matched_idf >= COVER_RATIO * total_idf
                    hits_rare = top_idf > 0 and any(
                        idf.get(t, 0) >= top_idf * 0.999 for t in mterms)
                    if not (covers or hits_rare):
                        continue
                else:
                    # short OR-fallback keeps the original count majority: a lone
                    # stray hit ('scan' inside a 'scans/' path) must not surface.
                    if len(mterms) * 2 < len(plain):
                        continue
            out.append((len(mterms), matched_idf, score, path, line, heading, content))
        return out

    def rank(query):
        terms = parse_query(query)
        if not terms:
            sys.exit("empty query")
        for f, t in terms:
            if f == "type" and t not in TYPE_EXTS:
                sys.exit(f"unknown type: {t} (known: {', '.join(sorted(TYPE_EXTS))})")
        # a 1-char plain token is a function word with no discriminative value, yet
        # as a rare literal it scores a deceptively high idf and can dominate the
        # coverage gate. Drop it — field-qualified terms are kept regardless.
        terms = [(f, t) for f, t in terms if f is not None or len(t) >= 2]
        plain = [t for f, t in terms if f is None]
        idf, total_idf, df, doc_hits = query_idf(db, plain, doc_hays())
        hitvs = {t: minimal_variants(term_variants(t)) for t in plain}
        # haystacks don't carry aliases, so an alias-only match must be added to
        # the prefilter or collect() would never see it
        for path, al in doc_aliases.items():
            for t in plain:
                if any(v in a for v in hitvs[t] for a in al):
                    doc_hits.setdefault(path, set()).add(t)
        # a conversational query is mostly function words: AND-matching every plain
        # term is impossible, so long queries skip the AND pre-filter and go
        # straight to an idf-gated OR over a full scan. Short queries keep strict AND.
        long_query = len(plain) >= LONG_QUERY
        fts_terms = [] if long_query else [t for f, t in terms if f in (None, "heading")]
        paths = candidate_paths(db, fts_terms, titles, doc_aliases) if fts_terms else None
        if paths is None:
            rows = all_sections()
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
        pvariants = {t: term_variants(t) for f, t in terms if f is None}
        results = collect(rows, not long_query, terms, plain, idf, total_idf, long_query, pvariants, doc_hits, hitvs)
        partial = long_query
        if not results and not long_query and len(plain) >= 2:
            # every-term AND came up empty — relax plain terms to an idf-gated OR
            results = collect(all_sections(), False, terms, plain, idf, total_idf, long_query, pvariants, doc_hits, hitvs)
            partial = True
        results.sort(key=lambda r: (-r[2], -r[1], -r[0]))
        # a weak result set (empty, partial-fallback, or a low top score) is the signal
        # for an agent to reformulate the dead terms (see `terms` df feedback) or fall
        # to the semantic path. Keyword search stays the fast $0 default.
        top_score = results[0][2] if results else 0
        weak = (not results) or partial or top_score < WEAK_SCORE
        return {"query": query, "results": results, "partial": partial, "weak": weak,
                "plain": plain, "pvariants": pvariants, "df": df}

    ranked = [rank(q) for q in queries]
    first = ranked[0]

    # notes match against the first query — in a fan-out call that is the user's
    # raw question, the phrasing past notes were saved under
    matched_notes = []
    plain0 = first["plain"]
    for q, ins, created, src in db.execute(
        "SELECT question, insight, created, sources FROM notes ORDER BY id DESC"
    ):
        hay = (q + " " + ins).lower()
        if plain0 and all(t in hay for t in plain0) and note_is_fresh(db, src):
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

    # --hybrid: if the agent supplied a query vector, blend semantic hits into the
    # ranking so a single call returns both halves. Generation still lives in the
    # caller (vector via --hybrid/stdin); the core only ranks.
    sem_ranks = {}
    if getattr(args, "hybrid", None) is not None:
        try:
            qvec = json.loads(sys.stdin.read() if args.hybrid == "-" else args.hybrid)
            for r_i, (c, p) in enumerate(semantic_hits(db, qvec, max(args.n, 20))):
                sem_ranks[p] = (r_i, c)
        except (ValueError, TypeError):
            sys.exit("--hybrid expects a JSON query vector (or '-' to read one from stdin)")

    def first_section(p):
        return db.execute(
            "SELECT line, heading, content FROM sections WHERE path=? ORDER BY line LIMIT 1",
            (p,)).fetchone()

    fused = len(ranked) > 1
    nsources = {}
    nvotes = len(ranked) + (1 if sem_ranks else 0)
    if not fused:
        results, partial, weak = first["results"], first["partial"], first["weak"]
        plain = first["plain"]
        if sem_ranks:
            kw_paths = {r[3] for r in results}
            for p, (r_i, c) in sem_ranks.items():
                if p not in kw_paths:
                    # semantic-only doc: splice in with a synthetic score below keyword
                    # hits but above nothing, ordered by cosine
                    row = first_section(p)
                    if row:
                        results.append((0, 0.0, WEAK_SCORE * c, p, row[0], row[1], row[2]))
            # re-rank: docs found by BOTH signals float up; cosine breaks keyword ties
            def blended(r):
                sem = sem_ranks.get(r[3])
                boost = (1.0 + sem[1]) if sem else 1.0
                return -(r[2] * boost)
            results.sort(key=blended)
            weak = not results
    else:
        # fan-out fusion: absolute scores aren't comparable across differently-worded
        # queries, but ranks are — each query's doc ranking votes via reciprocal rank
        # (RRF). A doc several phrasings agree on beats a doc one phrasing loved; a
        # hybrid vector, when given, is just one more voter.
        acc = {}
        for qi, r in enumerate(ranked):
            for r_i, row in enumerate(r["results"]):
                e = acc.get(row[3])
                if e is None:
                    acc[row[3]] = e = [0.0, 0, (r_i, qi), row]
                e[0] += 1.0 / (RRF_K + r_i)
                e[1] += 1
                if (r_i, qi) < e[2]:
                    e[2], e[3] = (r_i, qi), row
        for p, (r_i, c) in sem_ranks.items():
            e = acc.get(p)
            if e is None:
                row = first_section(p)
                if not row:
                    continue
                acc[p] = e = [0.0, 0, (r_i, len(ranked)),
                              (0, 0.0, 0.0, p, row[0], row[1], row[2])]
            e[0] += 1.0 / (RRF_K + r_i)
            e[1] += 1
        results = []
        for p, (rrf, n_src, _, row) in sorted(
                acc.items(), key=lambda kv: (-kv[1][0], kv[1][2], kv[0])):
            results.append((row[0], row[1], rrf, p, row[4], row[5], row[6]))
            nsources[p] = n_src
        partial = all(r["partial"] for r in ranked)
        weak = all(r["weak"] for r in ranked)
        plain = first["plain"]

    # highlight on the term variants actually matched, not the raw tokens — a
    # particle-suffixed query token ('컴포넌트들') should still light up its stem
    hl = {v for r in ranked for t in r["plain"] for v in r["pvariants"][t]}
    dead = [t for t in first["plain"] if first["df"].get(t, 0) == 0]

    if args.json:
        out = []
        for nmatched, midf, score, path, line, heading, content in results[: args.n]:
            lines = content.splitlines()
            hits = [ln.strip() for ln in lines if any(t in ln.lower() for t in hl)]
            rec = {"path": path, "line": line, "heading": heading,
                   "score": round(score, 4 if fused else 2), "matched": hits[:3]}
            if fused:
                rec["sources"] = f"{nsources[path]}/{nvotes}"
            if path in sem_ranks:
                rec["cosine"] = round(sem_ranks[path][1], 4)
            if partial and not fused:
                rec["partial"] = f"{nmatched}/{len(plain)}"
            if args.full:
                rec["content"] = content
            out.append(rec)
        payload = {"query": queries[0] if not fused else queries,
                   "notes": matched_notes, "partial": partial, "weak": weak,
                   "hybrid": bool(sem_ranks),
                   "terms": [{"term": t, "df": first["df"][t]} for t in first["plain"]],
                   "results": out}
        if fused:
            payload["fused"] = True
            payload["queries"] = [
                {"query": r["query"], "weak": r["weak"], "partial": r["partial"],
                 "terms": [{"term": t, "df": r["df"][t]} for t in r["plain"]]}
                for r in ranked]
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    if not results and not matched_notes:
        print("no results")
        if dead:
            print(f"~ no corpus hits for: {', '.join(dead)} — swap these for document vocabulary")
        return
    for nmatched, midf, score, path, line, heading, content in results[: args.n]:
        if fused:
            tag = f"rrf {score:.4f}, {nsources[path]}/{nvotes} queries"
        elif partial:
            tag = f"partial {nmatched}/{len(plain)}, score {score:.1f}"
        else:
            tag = f"score {score:.1f}"
        if path in sem_ranks:
            tag += f", cos {sem_ranks[path][1]:.3f}"
        print(f"{path}:{line}  [{heading}]  ({tag})")
        lines = content.splitlines()
        if args.full:
            for ln in lines:
                print(f"  {ln.rstrip()}")
            continue
        hits = [i for i, ln in enumerate(lines) if any(t in ln.lower() for t in hl)]
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
    if weak and dead:
        print(f"~ no corpus hits for: {', '.join(dead)} — swap these for document vocabulary")


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


def cosine(a, b):
    dot = na = nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


def doc_sha(db, path):
    row = db.execute("SELECT sha FROM files WHERE path=?", (path,)).fetchone()
    return row[0] if row else None


def cmd_embed(root, db, args):
    if args.action == "set":
        path = norm_rel(args.doc)
        sha = doc_sha(db, path)
        if sha is None:
            sys.exit(f"not in index (run update first?): {path}")
        raw = sys.stdin.read() if args.stdin else args.vector
        if not raw:
            sys.exit("no vector given (use --vector <json> or --stdin)")
        try:
            vec = json.loads(raw)
        except ValueError:
            sys.exit("vector must be a JSON array of numbers")
        if not isinstance(vec, list) or not all(isinstance(x, (int, float)) for x in vec):
            sys.exit("vector must be a JSON array of numbers")
        append_semantics(root, {"type": "embed", "path": path, "sha": sha, "vec": vec})
        db.execute("INSERT OR REPLACE INTO embeds(path, sha, vec) VALUES(?,?,?)",
                   (path, sha, json.dumps(vec, ensure_ascii=False)))
        db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('semantics_sha', ?)",
                   (hashlib.sha256(semantics_path(root).read_bytes()).hexdigest(),))
        db.commit()
        print(f"stored embedding for {path} ({len(vec)} dims)")
        return

    # status: coverage + which docs need (re)embedding
    files = {p: s for p, s in db.execute("SELECT path, sha FROM files")}
    embeds = {p: (s, v) for p, s, v in db.execute("SELECT path, sha, vec FROM embeds")}
    dim = None
    for _, v in embeds.values():
        arr = json.loads(v)
        if arr:
            dim = len(arr)
            break
    fresh = [p for p, (s, _) in embeds.items() if files.get(p) == s]
    stale = [p for p, (s, _) in embeds.items() if p in files and files[p] != s]
    missing = [p for p in files if p not in embeds]
    if args.json:
        print(json.dumps({"total_docs": len(files), "embedded": len(fresh),
                          "stale": stale, "missing": missing, "dims": dim},
                         ensure_ascii=False, indent=2))
        return
    print(f"embedded: {len(fresh)}/{len(files)} docs" + (f" ({dim} dims)" if dim else ""))
    if stale:
        print(f"stale (re-embed {len(stale)}): " + ", ".join(stale[:10])
              + (" ..." if len(stale) > 10 else ""))
    if missing:
        print(f"missing ({len(missing)}): " + ", ".join(missing[:10])
              + (" ..." if len(missing) > 10 else ""))


def semantic_hits(db, qvec, limit):
    """Cosine rank of fresh embeddings (sha matches the current file)."""
    files = {p: s for p, s in db.execute("SELECT path, sha FROM files")}
    scored = []
    for path, sha, vec in db.execute("SELECT path, sha, vec FROM embeds"):
        if files.get(path) != sha:
            continue
        arr = json.loads(vec)
        if len(arr) != len(qvec):
            continue
        scored.append((cosine(qvec, arr), path))
    scored.sort(reverse=True)
    return scored[:limit]


def cmd_semsearch(root, db, args):
    try:
        qvec = json.loads(sys.stdin.read() if args.stdin else args.vector)
    except (ValueError, TypeError):
        sys.exit("query vector must be a JSON array (use --vector <json> or --stdin)")
    hits = semantic_hits(db, qvec, args.n)
    if args.json:
        print(json.dumps({"results": [{"path": p, "cosine": round(c, 4)} for c, p in hits]},
                         ensure_ascii=False, indent=2))
        return
    if not hits:
        print("no embeddings match (run embed set first, or check vector dims)")
        return
    for c, p in hits:
        print(f"{c:.4f}  {p}")


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


# English standards only — language-specific conventions (관련 문서, 関連, 相关…) would
# bake one locale into a tool meant for everyone. For any other heading, pass --section.
RELATED_HEADINGS = {"related", "related pages", "related docs", "see also"}


def cmd_link_add(root, db, args):
    doc = norm_rel(args.doc)
    p = root / doc
    if not p.is_file():
        sys.exit(f"not found: {doc}")
    if p.suffix.lower() != ".md":
        sys.exit(f"link add writes wikilinks into markdown docs only: {doc}")
    stems = stem_map(db)
    known = {q for (q,) in db.execute("SELECT path FROM files")}
    text = p.read_text(encoding="utf-8", errors="replace")

    linked = set()
    for m in WIKILINK.finditer(text):
        t = resolve_stem(stems, m.group(1).strip())
        if t:
            linked.add(t)
    for m in MDLINK.finditer(text):
        url = m.group(1)
        try:
            linked.add((p.parent / url).resolve().relative_to(root).as_posix())
        except ValueError:
            linked.add(norm_rel(url))

    to_add = []
    for raw in args.targets:
        target = resolve_stem(stems, raw)
        if not target and norm_rel(raw) in known:
            target = norm_rel(raw)
        if not target:
            sys.exit(f"target not in index: {raw} (run update first?)")
        if target == doc:
            print(f"  skip {raw}: a doc cannot link to itself")
            continue
        if target in linked:
            print(f"  already linked: {target} — no change")
            continue
        stem = Path(target).stem
        # a colliding stem resolves to another file — fall back to a path-style link
        label = stem if stems.get(stem.lower()) == target else (
            target[: -len(Path(target).suffix)] if Path(target).suffix else target)
        to_add.append((target, label))
        linked.add(target)

    if not to_add:
        print("nothing to add")
        return

    lines = text.splitlines()
    wanted = {args.section.casefold()} if args.section else RELATED_HEADINGS
    sec_start = sec_level = None
    for i, ln in enumerate(lines):
        m = HEADING.match(ln)
        if m and m.group(2).strip().casefold() in wanted:
            sec_start, sec_level = i, len(m.group(1))
            break

    new_items = ["- [[%s]]" % label for _, label in to_add]
    if sec_start is None:
        heading = "## " + (args.section or "Related")
        while lines and not lines[-1].strip():
            lines.pop()
        insert_desc = f"new '{heading}' section at end of file"
        lines += ["", heading] + new_items
    else:
        end = len(lines)
        for j in range(sec_start + 1, len(lines)):
            m = HEADING.match(lines[j])
            if m and len(m.group(1)) <= sec_level:
                end = j
                break
        while end > sec_start + 1 and not lines[end - 1].strip():
            end -= 1
        insert_desc = f"under '{lines[sec_start].strip()}' (line {sec_start + 1})"
        lines[end:end] = new_items

    for target, label in to_add:
        print(f"link add {doc} + [[{label}]]  ({target})")
    print(f"  insert: {insert_desc}")
    if not args.apply:
        print("dry run — nothing written. Re-run with --apply to execute.")
        return
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    cmd_update(root, db, argparse.Namespace(ignore=[], map_path=None, no_map=False))


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
    # [[alias]] links travel with the file's frontmatter — still valid after the move
    alias_keys = {a.lower() for (a,) in db.execute(
        "SELECT alias FROM aliases WHERE path=?", (old,))}

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
            if link_stem(target) in alias_keys:
                return m.group(0)
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
        elif r["type"] == "embed":
            if r.get("path") == old:
                r["path"] = new
                sem_changed += 1
        elif r["type"] == "edge" and old in (r.get("src"), r.get("dst")):
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


AGENTS_MD_START = "<!-- wikimap:start -->"
AGENTS_MD_END = "<!-- wikimap:end -->"
AGENTS_MD_BLOCK = AGENTS_MD_START + """
## wikimap — document index for this folder

These docs are indexed by [wikimap](https://github.com/dhha22/wikimap) (zero-LLM, sub-second updates). Run commands as `wikimap <cmd>` (pip install) or `python3 ~/.agents/skills/wikimap/wikimap.py <cmd>`.

- To answer a question about these docs: run `wikimap search "query"` first and read only the sections it returns — never sweep whole files. For fact/value questions, re-search with `-C 3` or `--full` to capture the value line. On 0 results or a `partial` marker, re-query once with synonyms or the other language before concluding it's absent.
- After creating, editing, or deleting files here: run `wikimap update`.
- After substantially editing a doc: `wikimap suggest --doc <path> -n 5 --wikilink`, verify the candidates, and paste only the genuine `[[links]]` into the doc body.
- To bootstrap a link-less corpus: `wikimap suggest -n 0 --json`, judge pairs by their `dir` stratum (same directory first, then sibling; take far pairs only on high scores), then `wikimap link add <doc> <target> --apply`.
- `search`, `links`, `path`, and `suggest` accept `--json` for structured output.
""" + AGENTS_MD_END


def install_agents_md(cwd):
    p = cwd / "AGENTS.md"
    if not p.exists():
        p.write_text(AGENTS_MD_BLOCK + "\n", encoding="utf-8")
        print(f"created {p} with the wikimap block")
        return
    text = p.read_text(encoding="utf-8")
    if AGENTS_MD_START in text and AGENTS_MD_END in text:
        pre, rest = text.split(AGENTS_MD_START, 1)
        _, post = rest.split(AGENTS_MD_END, 1)
        p.write_text(pre + AGENTS_MD_BLOCK + post, encoding="utf-8")
        print(f"refreshed the wikimap block in {p} (rest of the file untouched)")
    else:
        sep = "" if text.endswith("\n\n") else "\n" if text.endswith("\n") else "\n\n"
        p.write_text(text + sep + AGENTS_MD_BLOCK + "\n", encoding="utf-8")
        print(f"appended the wikimap block to {p} (existing content untouched)")


GRAPHIFY_ARTIFACT_DIRS = ("graphify-out",)
GRAPHIFY_ARTIFACT_FILES = (".graphifyignore",)


def find_graphify_artifacts(root: Path):
    """graphify가 *생성한* 것만 고른다. 사용자가 쓴 문서는 이름에 graphify가 들어가도 손대지 않는다."""
    dirs, files, graphs = [], [], []
    for p in root.rglob("*"):
        if any(part in IGNORE_DIRS for part in p.relative_to(root).parts[:-1]):
            continue
        rel = p.relative_to(root).as_posix()
        if p.is_dir() and p.name in GRAPHIFY_ARTIFACT_DIRS:
            dirs.append(rel)
        elif p.is_file() and p.name in GRAPHIFY_ARTIFACT_FILES:
            files.append(rel)
    for d in dirs:
        g = root / d / "graph.json"
        if g.is_file():
            graphs.append(g.relative_to(root).as_posix())
    # 아티팩트 디렉터리 밖의 graph.json 은 사용자 파일일 수 있으므로 자동 삭제 대상에서 뺀다
    dirs = [d for d in dirs if not any(d.startswith(o + "/") for o in dirs if o != d)]
    return dirs, files, graphs


def cmd_migrate(root, db, args):
    dirs, files, graphs = find_graphify_artifacts(root)
    if not dirs and not files:
        print("no graphify artifacts found — nothing to migrate.")
        print("if this vault never used graphify, just run: wikimap update")
        return

    graph = args.graph or (graphs[0] if graphs else None)
    print(f"migrate graphify → wikimap  ({root})")
    print()
    print("  will REMOVE (graphify's own artifacts):")
    for d in dirs:
        print(f"    {d}/")
    for f in files:
        print(f"    {f}")
    print()
    if graph and not args.no_import:
        print(f"  will IMPORT inferred edges from {graph} first (before removal)")
    elif graph:
        print(f"  will SKIP the edge import (--no-import); {graph} is deleted with its directory")
    else:
        print("  no graph.json found — nothing to import, only artifacts to remove")
    print("  will REINDEX with wikimap (sub-second, 0 tokens) and write MAP.md")
    print()
    print("  your source documents are never touched.")
    if not args.apply:
        print()
        print("dry run — nothing written. Re-run with --apply to execute.")
        return

    if graph and not args.no_import:
        added, pairs, skipped = import_graphify_edges(root, db, root / graph)
        print(f"imported {added} doc-pair edges (from {pairs} pairs; {skipped} skipped)")

    for d in dirs:
        shutil.rmtree(root / d, ignore_errors=True)
        print(f"removed {d}/")
    for f in files:
        (root / f).unlink(missing_ok=True)
        print(f"removed {f}")

    args.ignore = None
    cmd_update(root, db, args)
    print()
    print("migration done. Next: point your agent rules at wikimap")
    print("  - navigation: read MAP.md, then `wikimap search \"<q>\" --json`")
    print("  - after editing vault files: `wikimap update`")
    print("  - `wikimap install --agents-md` drops a usage block into AGENTS.md")


def cmd_install(args):
    if args.hook:
        install_hook(find_root(args.root))
        return
    if args.agents_md:
        install_agents_md(Path.cwd())
        return
    base = Path.cwd() if args.project else Path.home()
    skill_dirs = {
        "claude": base / ".claude" / "skills" / "wikimap",
        "agents": base / ".agents" / "skills" / "wikimap",
    }
    chosen = skill_dirs if args.target == "all" else {args.target: skill_dirs[args.target]}
    src = Path(__file__).resolve()
    for dest in chosen.values():
        dest.mkdir(parents=True, exist_ok=True)
        tool = dest / "wikimap.py"
        if src != tool:
            tool.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        skill = dest / "SKILL.md"
        if skill.exists():
            print(f"kept existing {skill} (customizations preserved)")
        else:
            skill.write_text(SKILL_TEMPLATE.replace("__WIKIMAP__", f"python3 {tool}"),
                             encoding="utf-8")
            print(f"wrote {skill}")
        migrate = dest.parent / "graphify-to-wikimap" / "SKILL.md"
        if migrate.exists():
            print(f"kept existing {migrate} (customizations preserved)")
        else:
            migrate.parent.mkdir(parents=True, exist_ok=True)
            migrate.write_text(MIGRATE_SKILL, encoding="utf-8")
            print(f"wrote {migrate}")
        print(f"installed wikimap {VERSION} to {dest}")
    print("next: cd <your-vault> && wikimap update")


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

    ins = sub.add_parser("install", help="install as an agent skill (~/.claude/skills + ~/.agents/skills)")
    ins.add_argument("--project", action="store_true",
                     help="install to ./.claude and ./.agents instead of the home directory")
    ins.add_argument("--target", choices=["claude", "agents", "all"], default="all",
                     help="skill location: claude (~/.claude/skills, Claude Code), "
                          "agents (~/.agents/skills, open agent-skills standard: Codex, Copilot, ...), "
                          "or all (default)")
    ins.add_argument("--agents-md", action="store_true",
                     help="insert a wikimap usage block into ./AGENTS.md for tools without "
                          "skill support (marker-delimited, idempotent, never touches other content)")
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
    sp.add_argument("query", nargs="+",
                    help="one or more queries — several phrasings of one question "
                         "are rank-fused (RRF) into a single document ranking")
    sp.add_argument("-n", type=int, default=8)
    sp.add_argument("-C", type=int, default=0, dest="context",
                    help="show N context lines around each matched line")
    sp.add_argument("--full", action="store_true", help="print the whole matched section")
    sp.add_argument("--json", action="store_true", help="structured output for agents/scripts")
    sp.add_argument("--hybrid", metavar="VEC", nargs="?", const="-",
                    help="blend agent-supplied query embedding into the ranking "
                         "(JSON array, or '-'/omitted value to read from stdin)")

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

    lk = sub.add_parser("link", help="insert a [[wikilink]] into a doc's link-list section "
                                     "(idempotent; dry run by default)")
    lk.add_argument("action", choices=["add"])
    lk.add_argument("doc", help="vault-relative markdown doc to edit")
    lk.add_argument("targets", nargs="+", help="link targets (stem, alias, or vault-relative path)")
    lk.add_argument("--section", default=None,
                    help="heading of the list section to use, in any language (default: "
                         "reuse an existing Related/See also section, else create '## Related')")
    lk.add_argument("--apply", action="store_true", help="actually write (default: dry run)")

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

    mg = sub.add_parser("migrate", help="migrate a graphify vault to wikimap (one command, dry run by default)")
    mg.add_argument("--apply", action="store_true", help="actually do it (default: dry run)")
    mg.add_argument("--no-import", action="store_true", help="discard graphify's inferred edges instead of importing them")
    mg.add_argument("--graph", help="path to graph.json (default: auto-detect in graphify-out/)")

    sg = sub.add_parser("suggest", help="heuristic candidates for inferred doc connections (no LLM)")
    sg.add_argument("--doc", help="limit to pairs involving this vault-relative path")
    sg.add_argument("-n", type=int, default=10, help="max candidates (0 = no cap)")
    sg.add_argument("--max-df", type=int, default=4, dest="max_df")
    sg.add_argument("--wikilink", action="store_true",
                    help="print candidates as paste-ready [[wikilinks]] for the doc body")
    sg.add_argument("--json", action="store_true", help="structured output for agents/scripts")

    em = sub.add_parser("embed", help="store/inspect agent-supplied embedding vectors "
                                      "(wikimap stores and searches; the agent generates)")
    em.add_argument("action", choices=["set", "status"])
    em.add_argument("doc", nargs="?", help="vault-relative path (for set)")
    em.add_argument("--vector", help="embedding as a JSON array of numbers (for set)")
    em.add_argument("--stdin", action="store_true", help="read the JSON vector from stdin")
    em.add_argument("--json", action="store_true", help="structured output for agents/scripts")

    ss = sub.add_parser("semsearch", help="cosine-rank docs by an agent-supplied query vector")
    ss.add_argument("--vector", help="query embedding as a JSON array of numbers")
    ss.add_argument("--stdin", action="store_true", help="read the JSON query vector from stdin")
    ss.add_argument("-n", type=int, default=10, help="max results")
    ss.add_argument("--json", action="store_true", help="structured output for agents/scripts")

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
        elif args.cmd == "link":
            cmd_link_add(root, db, args)
        elif args.cmd == "note":
            cmd_note_add(root, db, args)
        elif args.cmd == "notes":
            cmd_notes(root, db, args)
        elif args.cmd == "import-graphify":
            cmd_import_graphify(root, db, args)
        elif args.cmd == "migrate":
            cmd_migrate(root, db, args)
        elif args.cmd == "embed":
            cmd_embed(root, db, args)
        elif args.cmd == "semsearch":
            cmd_semsearch(root, db, args)
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
