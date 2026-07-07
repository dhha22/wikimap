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
import hashlib
import json
import re
import sqlite3
import sys
import time
from collections import Counter, deque
from datetime import datetime, timezone
from pathlib import Path

VERSION = "0.4.0"

IGNORE_DIRS = {
    ".obsidian", ".git", ".wikimap", "graphify-out", "node_modules",
    ".claude", ".github", "__pycache__", ".venv", "venv",
}
IGNORE_FILES = {"MAP.md"}
PLAIN_EXTS = {".txt", ".rst", ".org", ".adoc"}
INDEX_EXTS = {".md"} | PLAIN_EXTS

SKILL_TEMPLATE = """---
name: wikimap
description: Zero-LLM incremental index + lazy semantic notes for a markdown knowledge base (wiki, Obsidian vault, spec folder; plain-text .txt/.rst/.org/.adoc indexed too). Use when searching vault documents ("where is the X policy/spec?"), tracing links, backlinks, or requirement IDs across documents, and refreshing the index after creating or editing vault files.
---

# wikimap

Index tool for a markdown knowledge base. Principle: **eager structure, lazy semantics** — builds are deterministic parsing only (zero LLM calls, sub-second), semantic knowledge accumulates at answer time.

All commands: `python3 ~/.claude/skills/wikimap/wikimap.py [--root <vault>] <cmd>`
(`--root` optional when cwd is inside the vault — the `.wikimap/` directory is auto-detected upward)

| Command | Purpose |
|---------|---------|
| `update` | incremental re-index + regenerate MAP.md (sha-diff, changed files only; prints coverage: indexed vs skipped; MAP.md ends with a Health section — orphans, broken links, stale semantics) |
| `search "query" [-n 8] [-C 3 \| --full]` | ranked section search (filename/title/heading boosted; FTS5-accelerated when available); shows matched lines (≤3); `-C N` adds N context lines, `--full` prints the whole section; fresh notes surface first; CJK substring-safe |
| `links <REQ-ID|filename|path>` | docs mentioning a requirement ID, or a doc's outlinks/backlinks/inferred connections — entries tagged `[linked|…]` (written by a human) vs `[inferred|…]` (confirmed guess) |
| `path <a> <b>` | shortest connection path between two docs (BFS over wiki/md links + fresh edges, both directions) |
| `note add --question "..." --insight "..." --sources a.md,b.md` | save an answer-time insight (source shas pinned) |
| `notes [--all] [--prune]` | list notes / prune stale ones |
| `suggest [--doc path] [-n 10]` | heuristic candidates for unwritten doc connections (shared rare terms, requirement IDs, code refs — no LLM) |
| `edge add --src a.md --dst b.md --relation ... --rationale "..."` | confirm a connection (both shas pinned; goes stale if either file changes) |
| `edges [--all] [--prune]` | list inferred connections |
| `import-graphify <graph.json>` | one-time import of INFERRED edges from an existing graphify graph |

## Rules for the agent

1. **On a vault question**: read `MAP.md` at the vault root first, then `search` for relevant sections, then Read only those file sections. Never sweep whole files. For fact/value questions ("what is the limit/period/owner?"), retry with `-C 3` or `--full` before falling back to Read.
2. **After answering**: if the answer synthesized multiple documents into a non-obvious conclusion, save it with `note add` (sources = the actual evidence files, vault-relative paths).
3. **After creating/editing/deleting vault files**: run `update` before the session ends (sub-second, zero tokens).
4. **`[NOTE fresh]` in search results**: sha-verified cache — trust and reuse it. Stale notes are hidden automatically.
5. **After creating or substantially editing a doc**: run `suggest --doc <path> -n 5`, read the candidates' relevant sections, and `edge add` only the genuinely related ones. Requirement IDs are per-document local numbers — a match across unrelated projects is a false signal; discard it.
6. **Trust tags in `links` output**: `[linked|…]` means a human wrote that connection in the source text; `[inferred|…]` means it was guessed and then confirmed (sha-verified). Weight answers accordingly.
"""

HEADING = re.compile(r"^(#{1,6})\s+(.*)")
WIKILINK = re.compile(r"\[\[([^\]|#]+)")
MDLINK = re.compile(r"\]\(([^)#\s]+\.md)\)")
CODEREF = re.compile(r"\b[\w/.-]*\w\.(?:kt|kts|swift|java|py|ts|tsx|gradle)\b")
REQID = re.compile(r"\bREQ-\d+\b")


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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


def parse_file(root: Path, path: Path):
    rel = str(path.relative_to(root))
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()

    if path.suffix.lower() == ".md":
        meta, body_start = parse_frontmatter(lines)
        title = meta.get("title") or ""
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
    else:
        sections = parse_plain_sections(rel, lines)
        title = next((l.strip()[:80] for l in lines if l.strip()), path.stem)

    links = []
    for m in WIKILINK.finditer(text):
        links.append((rel, m.group(1).strip(), "wiki"))
    for m in MDLINK.finditer(text):
        dst = m.group(1)
        if not dst.startswith("http"):
            resolved = (path.parent / dst).resolve()
            try:
                dst = str(resolved.relative_to(root))
            except ValueError:
                pass
            links.append((rel, dst, "md"))
    for m in set(CODEREF.findall(text)):
        links.append((rel, m, "code"))
    for m in set(REQID.findall(text)):
        links.append((rel, m, "req"))

    return {
        "path": rel,
        "sha": hashlib.sha256(text.encode()).hexdigest(),
        "mtime": path.stat().st_mtime,
        "title": title,
        "words": len(text.split()),
        "sections": sections,
        "links": links,
    }


def scan_files(root: Path, skipped: Counter = None):
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        if any(part in IGNORE_DIRS for part in p.relative_to(root).parts):
            continue
        if p.name in IGNORE_FILES:
            continue
        if p.suffix.lower() not in INDEX_EXTS:
            if skipped is not None:
                skipped[p.suffix.lower() or "(no ext)"] += 1
            continue
        yield p


def stem_map(db):
    return {Path(p).stem.lower(): p for (p,) in db.execute("SELECT path FROM files")}


def resolve_stem(stems, dst):
    # wikilink targets may be path-style ([[insights/foo]]) — match by final stem
    return stems.get(Path(dst).stem.lower())


def note_is_fresh(db, sources_json):
    for s in json.loads(sources_json):
        row = db.execute("SELECT sha FROM files WHERE path=?", (s["path"],)).fetchone()
        if not row or row[0] != s["sha"]:
            return False
    return True


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
    now = datetime.now(timezone.utc).isoformat()
    added = 0
    for (a, b), info in pairs.items():
        rel = max(set(info["relations"]), key=info["relations"].count)
        cur = db.execute(
            "INSERT OR IGNORE INTO edges(src,dst,relation,rationale,origin,created,src_sha,dst_sha)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (a, b, rel, " | ".join(info["rationales"]), "graphify-import", now, shas[a], shas[b]),
        )
        added += cur.rowcount
    db.commit()
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

    def bump(pa, pb, amount, signal):
        key = tuple(sorted([pa, pb]))
        if key in linked:
            return
        if args.doc and args.doc not in key:
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
    if not top:
        print("no candidates")
        return
    for (a, b), s in top:
        print(f"({s:.1f}) {a}")
        print(f"      ↔ {b}")
        print(f"      shared signals: {', '.join(why[(a, b)][:6])}")
    print(
        "\nTo confirm: wikimap edge add --src <a> --dst <b> "
        "--relation conceptually_related_to --rationale '...'"
    )


def cmd_edge_add(root, db, args):
    shas = {}
    for p in (args.src, args.dst):
        row = db.execute("SELECT sha FROM files WHERE path=?", (p,)).fetchone()
        if not row:
            sys.exit(f"not in index (run update first?): {p}")
        shas[p] = row[0]
    a, b = sorted([args.src, args.dst])
    db.execute(
        "INSERT OR REPLACE INTO edges(src,dst,relation,rationale,origin,created,src_sha,dst_sha)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (a, b, args.relation, args.rationale, "claude",
         datetime.now(timezone.utc).isoformat(), shas[a], shas[b]),
    )
    db.commit()
    write_map(root, db)
    print(f"edge saved: {a} ↔ {b} ({args.relation})")


def cmd_edges(root, db, args):
    r = fresh_edges(db)
    for src, dst, rel, rat, origin in r["fresh"]:
        print(f"[fresh|{origin}] {src} --{rel}→ {dst}\n   {rat[:140]}")
    if args.all:
        for src, dst, rel, rat, origin in r["stale"]:
            print(f"[STALE|{origin}] {src} --{rel}→ {dst}")
    if args.prune and r["stale"]:
        for src, dst, rel, _, _ in r["stale"]:
            db.execute("DELETE FROM edges WHERE src=? AND dst=? AND relation=?", (src, dst, rel))
        db.commit()
        write_map(root, db)
        print(f"pruned {len(r['stale'])} stale edges")
    elif r["stale"] and not args.all:
        print(f"({len(r['stale'])} stale edges hidden — use --all to show, --prune to delete)")


def cmd_update(root, db, args):
    t0 = time.time()
    skipped = Counter()
    seen, changed_rels = set(), []
    known = {p: (sha, mt) for p, sha, mt in db.execute("SELECT path, sha, mtime FROM files")}
    for p in scan_files(root, skipped):
        rel = str(p.relative_to(root))
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
        db.execute(
            "INSERT OR REPLACE INTO files VALUES(?,?,?,?,?)",
            (rel, parsed["sha"], parsed["mtime"], parsed["title"], parsed["words"]),
        )
        db.executemany("INSERT INTO sections VALUES(?,?,?,?,?)", parsed["sections"])
        db.executemany("INSERT INTO links VALUES(?,?,?)", parsed["links"])
        changed_rels.append(rel)

    deleted = set(known) - seen
    for rel in deleted:
        db.execute("DELETE FROM files WHERE path=?", (rel,))
        db.execute("DELETE FROM sections WHERE path=?", (rel,))
        db.execute("DELETE FROM links WHERE src=?", (rel,))
    sync_fts(db, changed_rels, deleted)
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
    print(
        f"wikimap: {total} files indexed ({len(changed_rels)} changed, {len(deleted)} deleted) "
        f"in {ms}ms | skipped {n_skipped} non-indexed files"
        + (f" ({top})" if top else "")
        + f" | notes: {fresh} fresh, {stale} stale | "
        f"edges: {len(e['fresh'])} fresh, {len(e['stale'])} stale | MAP.md updated"
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
        "SELECT src, dst, kind FROM links WHERE kind IN ('wiki','md')"
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

    (root / "MAP.md").write_text("\n".join(out) + "\n", encoding="utf-8")


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


def cmd_search(root, db, args):
    terms = [t.lower() for t in args.query.split() if t.strip()]
    if not terms:
        sys.exit("empty query")

    shown_notes = 0
    for q, ins, created, src in db.execute(
        "SELECT question, insight, created, sources FROM notes ORDER BY id DESC"
    ):
        hay = (q + " " + ins).lower()
        if all(t in hay for t in terms) and note_is_fresh(db, src):
            files = ", ".join(s["path"] for s in json.loads(src))
            print(f"[NOTE fresh {created[:10]}] Q: {q}\n  {ins}\n  sources: {files}\n")
            shown_notes += 1
            if shown_notes >= 3:
                break

    titles = {p: t for p, t in db.execute("SELECT path, title FROM files")}
    paths = candidate_paths(db, terms, titles)
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

    results = []
    for path, line, heading, content in rows:
        title_l, heading_l, content_l = titles.get(path, "").lower(), heading.lower(), content.lower()
        path_l = path.lower()
        if not all(t in title_l or t in path_l or t in heading_l or t in content_l for t in terms):
            continue
        score = 0
        for t in terms:
            score += (
                8 * (t in title_l) + 6 * (t in path_l) + 5 * (t in heading_l)
                + min(content_l.count(t), 5)
            )
        results.append((score, path, line, heading, content))

    results.sort(key=lambda r: -r[0])
    if not results and not shown_notes:
        print("no results")
        return
    for score, path, line, heading, content in results[: args.n]:
        print(f"{path}:{line}  [{heading}]  (score {score})")
        lines = content.splitlines()
        if args.full:
            for ln in lines:
                print(f"  {ln.rstrip()}")
            continue
        hits = [i for i, ln in enumerate(lines) if any(t in ln.lower() for t in terms)]
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
    target = args.target
    if REQID.fullmatch(target):
        rows = db.execute("SELECT src FROM links WHERE kind='req' AND dst=?", (target,)).fetchall()
        print(f"{target} appears in {len(rows)} docs:")
        for (src,) in rows:
            print(f"  {src}")
        return

    stems = stem_map(db)
    path = target if db.execute("SELECT 1 FROM files WHERE path=?", (target,)).fetchone() else stems.get(
        Path(target).stem.lower()
    )
    if not path:
        sys.exit(f"not found: {target}")

    print(f"== {path}")
    print("outlinks:")
    for dst, kind in db.execute("SELECT dst, kind FROM links WHERE src=? ORDER BY kind", (path,)):
        resolved = (resolve_stem(stems, dst) or dst) if kind == "wiki" else dst
        tag = f"linked|{kind}" if kind in ("wiki", "md") else kind
        print(f"  [{tag}] {resolved}")
    print("backlinks:")
    seen_back = set()
    for src, dst, kind in db.execute("SELECT src, dst, kind FROM links WHERE kind IN ('wiki','md')"):
        resolved = resolve_stem(stems, dst) if kind == "wiki" else dst
        if resolved == path and src not in seen_back:
            seen_back.add(src)
            print(f"  [linked|{kind}] {src}")
    inferred = [
        e for e in fresh_edges(db)["fresh"] if path in (e[0], e[1])
    ]
    if inferred:
        print("inferred:")
        for src, dst, rel, rat, origin in inferred:
            other = dst if src == path else src
            print(f"  [inferred|{rel}|{origin}] {other}")
            print(f"    ∵ {rat[:120]}")


def cmd_path(root, db, args):
    stems = stem_map(db)
    known = {p for (p,) in db.execute("SELECT path FROM files")}

    def resolve(t):
        if t in known:
            return t
        return stems.get(Path(t).stem.lower())

    src, dst = resolve(args.src), resolve(args.dst)
    if not src:
        sys.exit(f"not found: {args.src}")
    if not dst:
        sys.exit(f"not found: {args.dst}")
    if src == dst:
        print(src)
        return

    adj = {}

    def add(a, b, label):
        adj.setdefault(a, {}).setdefault(b, label)

    for s, d, kind in db.execute("SELECT src, dst, kind FROM links WHERE kind IN ('wiki','md')"):
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
        print(f"no path: {src} ↮ {dst}")
        return
    chain = []
    cur = dst
    while cur is not None:
        chain.append(cur)
        cur = prev[cur]
    chain.reverse()
    print(chain[0])
    for a, b in zip(chain, chain[1:]):
        print(f"  {adj[a][b]} {b}")
    print(f"({len(chain) - 1} hops)")


def cmd_note_add(root, db, args):
    sources = []
    for p in args.sources.split(","):
        p = p.strip()
        row = db.execute("SELECT sha FROM files WHERE path=?", (p,)).fetchone()
        if not row:
            sys.exit(f"source not in index (run update first?): {p}")
        sources.append({"path": p, "sha": row[0]})
    db.execute(
        "INSERT INTO notes(question, insight, created, sources) VALUES(?,?,?,?)",
        (args.question, args.insight, datetime.now(timezone.utc).isoformat(), json.dumps(sources)),
    )
    db.commit()
    write_map(root, db)
    print(f"note saved ({len(sources)} sources pinned)")


def cmd_notes(root, db, args):
    rows = db.execute("SELECT id, question, insight, created, sources FROM notes ORDER BY id DESC").fetchall()
    stale_ids = []
    for nid, q, ins, created, src in rows:
        fresh = note_is_fresh(db, src)
        if not fresh:
            stale_ids.append(nid)
        if fresh or args.all:
            mark = "fresh" if fresh else "STALE"
            print(f"#{nid} [{mark}] {created[:10]} Q: {q}\n   {ins}")
    if args.prune and stale_ids:
        db.executemany("DELETE FROM notes WHERE id=?", [(i,) for i in stale_ids])
        db.commit()
        write_map(root, db)
        print(f"pruned {len(stale_ids)} stale notes")
    elif stale_ids and not args.all:
        print(f"({len(stale_ids)} stale notes hidden — use --all to show, --prune to delete)")


def cmd_install(args):
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

    sub.add_parser("update", help="incremental re-index + regenerate MAP.md")
    sub.add_parser("map", help="regenerate MAP.md only")

    sp = sub.add_parser("search", help="ranked section search")
    sp.add_argument("query")
    sp.add_argument("-n", type=int, default=8)
    sp.add_argument("-C", type=int, default=0, dest="context",
                    help="show N context lines around each matched line")
    sp.add_argument("--full", action="store_true", help="print the whole matched section")

    lp = sub.add_parser("links", help="outlinks/backlinks of a doc, or docs for a REQ-ID")
    lp.add_argument("target")

    pp = sub.add_parser("path", help="shortest link path between two docs (BFS over links + fresh edges)")
    pp.add_argument("src")
    pp.add_argument("dst")

    np_ = sub.add_parser("note", help="save an answer-time semantic insight")
    np_.add_argument("add", choices=["add"])
    np_.add_argument("--question", required=True)
    np_.add_argument("--insight", required=True)
    np_.add_argument("--sources", required=True, help="comma-separated vault-relative paths")

    lsp = sub.add_parser("notes", help="list semantic notes")
    lsp.add_argument("--all", action="store_true")
    lsp.add_argument("--prune", action="store_true")

    ig = sub.add_parser("import-graphify", help="import INFERRED edges from a graphify graph.json")
    ig.add_argument("graph", help="path to graph.json")

    sg = sub.add_parser("suggest", help="heuristic candidates for inferred doc connections (no LLM)")
    sg.add_argument("--doc", help="limit to pairs involving this vault-relative path")
    sg.add_argument("-n", type=int, default=10)
    sg.add_argument("--max-df", type=int, default=4, dest="max_df")

    ea = sub.add_parser("edge", help="confirm an inferred connection (sha-pinned both ends)")
    ea.add_argument("add", choices=["add"])
    ea.add_argument("--src", required=True)
    ea.add_argument("--dst", required=True)
    ea.add_argument("--relation", default="conceptually_related_to")
    ea.add_argument("--rationale", required=True)

    le = sub.add_parser("edges", help="list inferred connections")
    le.add_argument("--all", action="store_true")
    le.add_argument("--prune", action="store_true")

    args = ap.parse_args()
    if args.cmd == "install":
        cmd_install(args)
        return
    root = find_root(args.root)
    db = open_db(root)
    try:
        if args.cmd == "update":
            cmd_update(root, db, args)
        elif args.cmd == "map":
            write_map(root, db)
            print("MAP.md updated")
        elif args.cmd == "search":
            cmd_search(root, db, args)
        elif args.cmd == "links":
            cmd_links(root, db, args)
        elif args.cmd == "path":
            cmd_path(root, db, args)
        elif args.cmd == "note":
            cmd_note_add(root, db, args)
        elif args.cmd == "notes":
            cmd_notes(root, db, args)
        elif args.cmd == "import-graphify":
            cmd_import_graphify(root, db, args)
        elif args.cmd == "suggest":
            cmd_suggest(root, db, args)
        elif args.cmd == "edge":
            cmd_edge_add(root, db, args)
        elif args.cmd == "edges":
            cmd_edges(root, db, args)
    finally:
        db.close()


if __name__ == "__main__":
    main()
