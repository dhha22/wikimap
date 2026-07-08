# wikimap

[![ci](https://github.com/dhha22/wikimap/actions/workflows/ci.yml/badge.svg)](https://github.com/dhha22/wikimap/actions/workflows/ci.yml)

**Zero-LLM incremental index + lazy semantic layer for markdown knowledge bases.**

One Python file. Zero dependencies. Zero LLM cost at build time — always. Sub-second updates, no matter how stale your index is.

Built for AI coding assistants (Claude Code and friends) working against a markdown vault: an Obsidian vault, a team wiki, a folder of specs and plans.

## Why not a knowledge-graph tool or RAG?

Tools like [graphify](https://github.com/Graphify-Labs/graphify) extract entities and relationships with an LLM **at build time** (eager extraction). That buys you inferred connections, but the bill comes on every update: change a doc, pay for re-extraction. Let the index drift for a week and your "incremental" update re-extracts half the corpus. RAG has the same eagerness problem with embeddings, plus a vector store to babysit.

wikimap inverts the design: **eager structure, lazy semantics.**

- **Structure is free.** Titles, headings, wikilinks, markdown links, requirement IDs, code-file references — all extracted by deterministic parsing. No LLM, no embeddings, no API key.
- **Semantics are earned at answer time.** When your assistant answers a question by synthesizing documents, it saves the conclusion as a *note* pinned to the source files' content hashes. When it confirms an unwritten connection between two docs, that becomes an *edge* pinned to both hashes. Change a source file and the cached knowledge goes stale automatically — it silently disappears from search results instead of feeding the model outdated facts.

The LLM cost is proportional to **what you actually asked**, never to corpus size.

## Measured (262-doc Korean/English vault, M-series Mac, wikimap 0.4.0)

| Operation | wikimap | graphify (same vault, same change set) |
|---|---|---|
| Full index build | 0.5 s, $0 | minutes + LLM extraction cost |
| Update after editing 1 doc + adding 1 + deleting 1 | **0.1 s, 0 tokens** | **~95 s + 46k tokens** (measured), plus community re-labeling |
| Update after index drifted for days | still sub-second (sha-diff) | re-detected 287 of 306 files as changed → near-full re-extraction |
| Search recall@5 (10 mixed Korean/English queries) | **10/10**, ~60 ms | 5/10 start-node matches — default query filter drops terms shorter than 4 chars, so every short Korean term is discarded |
| Search output | section + line number + matched snippet | entity labels; you still re-read the source files |
| Deleted file cleanup | automatic, verified | 9.7% of source files in the graph were ghosts (already deleted); 40 duplicate node labels |
| Determinism | same input → byte-identical index | non-deterministic graphs from identical inputs ([upstream #1695](https://github.com/Graphify-Labs/graphify/issues/1695)) |

At scale (same vault duplicated to **3,760 docs**): full build 12 s (one-time — an FTS5 trigram index kicks in at ≥500 docs), incremental update with 3 changes **0.19 s**, search 60–100 ms via FTS5 (vs ~0.3 s linear fallback). Queries containing terms under 3 characters fall back to the exact linear scan, so CJK short-word recall is never sacrificed for speed.

On an expanded 30-query golden set (Korean/English/mixed, 309-doc vault): **recall@5 30/30, avg 67 ms** (re-verified at 30/30 after HTML indexing in 0.5.0, the semantics-file migration in 0.6.0, and PDF/image indexing in 0.7.0). Ranking changes are gated by this kind of golden set in CI — the test suite (`python3 tests.py`, stdlib only) covers incremental sync, ghost-free deletes, byte-identical determinism, FTS consistency at scale, CJK short-term fallback, ignore config, map relocation, HTML tag-strip indexing, semantics surviving DB deletion, the ≤0.5.x migration path, `--json` schemas, hook append-preservation, phrase/field/tag queries, PDF noise exclusion, image alt indexing, `mv` reference rewriting, and install never touching an existing `SKILL.md`. CI runs it on macOS, Linux, and Windows, Python 3.8 and 3.13.

Reproduce on your own vault: `python3 bench.py --root <vault> --cold`, or with your own golden set: `bench.py --root <vault> --queries q.tsv` (lines of `query<TAB>expected-path-substring`).

## Install

```bash
pipx install wikimap                # or: uv tool install wikimap / pip install wikimap
cd your-vault && wikimap update
```

Or copy the single file — same thing, works offline and without pip:

```bash
curl -O https://raw.githubusercontent.com/dhha22/wikimap/main/wikimap.py
cd your-vault && python3 wikimap.py update
```

Either way, `wikimap install` (or `python3 wikimap.py install`) sets it up as a Claude Code skill at `~/.claude/skills/wikimap/`; `install --project` writes to `./.claude` for per-repo setup. Existing `SKILL.md` customizations are never overwritten. Requires Python 3.8+, nothing else.

## Commands

| Command | What it does |
|---|---|
| `update [--ignore <dir\|glob>] [--map-path <rel> \| --no-map]` | Incremental re-index (sha-diff) + regenerate `MAP.md`, the one-page vault map agents read first. Prints coverage — indexed vs skipped counts by extension, so nothing is dropped silently. `MAP.md` ends with a Health section: orphan docs, broken links, stale semantics. Excludes: `.wikimapignore` at the vault root (one dir/glob per line, persistent) or `--ignore` (this run only). `--map-path`/`--no-map` relocate or disable the generated map — persisted in the index |
| `search "query" [-n 8] [-C 3 \| --full]` | Ranked section search — filename, title, and heading matches boosted; FTS5-accelerated on vaults ≥500 docs. Exact file:line + matched lines (≤3). `-C N` adds N context lines, `--full` prints the whole section. Fresh notes surface first. Query syntax: `"exact phrase"`, `title:` / `path:` / `heading:` / `tag:` field filters (frontmatter `tags: [a, b]` are indexed and summarized in the map), and `type:md\|html\|pdf\|image\|text` file-type filter. When no section matches every term, results relax to a majority-of-terms OR marked `partial k/n` — never mixed with full matches; field filters stay hard |
| `links <target>` | Outlinks, backlinks, and inferred connections of a doc; or every doc mentioning a `REQ-nn` ID. Trust tags on every entry: `[linked\|…]` = a human wrote it in the source, `[inferred\|…]` = guessed then confirmed, sha-verified |
| `path <a> <b>` | Shortest connection path between two docs — BFS over wiki/markdown links (both directions) plus fresh inferred edges |
| `note add` | Save an answer-time insight, pinned to source content hashes |
| `suggest [--doc path] [--wikilink]` | Heuristic candidates for unwritten connections: shared rare terms, shared requirement IDs, shared code references. 0.2 s, no LLM. `--wikilink` prints paste-ready `[[links]]` — promote real connections into the doc body, where every tool can read them |
| `edge add` | Confirm a connection (agent judges `suggest` candidates); pinned to both files' hashes |
| `edge repin --src a --dst b` | An edge went stale because an endpoint was edited, but the connection still holds? Refresh the sha pins and keep the rationale — no retyping |
| `notes` / `edges` `[--all] [--prune]` | List cached semantics; stale entries are hidden by default and prunable |
| `import-graphify <graph.json>` | One-time migration of INFERRED edges from an existing graphify graph — with hash freshness retrofitted |
| `install --hook` | Git post-commit hook that runs `update` after every commit — appends to an existing hook, never replaces it |
| `mv <old> <new> [--apply]` | Move/rename a doc and rewrite every wikilink, markdown, and image reference to it — including the moved file's own relative links and `semantics.jsonl` paths (content hash unchanged, so pinned semantics stay fresh). Dry run unless `--apply` |
| `fix-links [--json]` | For each broken link the Health section counts: suggest close-match targets. Suggestions only — nothing is auto-applied |

`search`, `links`, `path`, `suggest`, `notes`, and `edges` all take **`--json`** — structured output for agents and scripts, no regex-scraping of human output. Schemas are stable and covered by the test suite.

## How inferred connections work without eager LLM extraction

1. `suggest` proposes candidate pairs from free signals: rare terms shared by only 2–4 documents, shared requirement IDs, references to the same source files. Pairs already linked explicitly are excluded; cross-directory pairs get a boost (more surprising).
2. Your assistant reads only the top candidates for the doc that changed and confirms the real ones with `edge add`. Cost scales with the edit, not the corpus.
3. Confirmed edges appear in `links` output and `MAP.md`, and go stale automatically when either endpoint changes. Stale-because-edited but still valid? `edge repin` re-pins it after review, rationale intact.

## Outputs

- `MAP.md` — vault root. Directory taxonomy, hub documents, recent changes, cross-document requirement IDs, inferred connections, fresh notes. The agent entry point.
- `.wikimap/semantics.jsonl` — the notes and edges themselves, append-only JSON lines. **This file is the source of truth** for the semantic layer: commit it to git to back up and share what your assistant has learned about the vault. Hand-editable; one bad line never takes the layer down.
- `.wikimap/index.db` — SQLite. A derived cache, genuinely disposable: delete it anytime, `update` rebuilds it from your files plus `semantics.jsonl` with nothing lost.

Upgrading from ≤0.5.x: the first run migrates existing DB notes/edges into `semantics.jsonl` automatically, one-time, nothing to do.

## Coexisting with other vault tools

wikimap is a standalone library — it assumes nothing about what else manages your folder. If another app (Obsidian, a second-brain app with its own index, a static-site generator) also watches the same root, three knobs keep the two from stepping on each other:

- **`.wikimapignore`** — one dir name or glob per line at the vault root. Keeps the other tool's artifacts (trash folders, build output) out of wikimap's index. `.trash/`, `.obsidian/`, and common build dirs are already excluded by default.
- **`--map-path .wikimap/MAP.md`** — if the other tool indexes markdown at the root, a generated `MAP.md` there would pollute its graph as a giant hub node. Relocating it into `.wikimap/` (which the other tool should skip anyway) hides it from everyone but your agent. Or `--no-map` to skip generation entirely. Both persist across runs.
- **`suggest --wikilink`** — when confirming discovered connections, prefer pasting explicit `[[links]]` into the document body over `edge add`. Files are the source of truth; explicit links are the one connection format every vault tool understands.

## Scope

wikimap's goal is that **every document in the folder is findable — whatever its format** — plus a relationship layer on top. Currently indexed:

- **Markdown** — the core: frontmatter (`title`, `tags`), headings, wikilinks, md links.
- **Plain-text prose** (`.txt`, `.rst`, `.org`, `.adoc`) — sectioned by paragraph blocks.
- **HTML** (`.html`, `.htm`) — tag-stripped, `<title>`/`<h1>` as title, sectioned by heading tags; `<a href>` anchors to local docs join the link graph, `<script>`/`<style>` excluded.
- **PDF** — deterministic text extraction with stdlib only (FlateDecode + literal-string text operators inside `BT..ET` blocks, noise-filtered). Scanned or CID-encoded (most CJK) PDFs can't be decoded without external libs, so they fall back to **name+path indexing** and the update line says so explicitly: no OCR, no silent garbage.
- **Images** (`.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`) — no content analysis; indexed by filename plus every **alt text** that references them (`![alt](img.png)`, `<img alt=…>`), and image references join the link graph. "Where is that checkout-flow diagram?" resolves by name or alt. `.svg` additionally contributes its `<title>`/`<desc>`/text nodes.

It does not parse code ASTs — if you need a call graph of a codebase, use a code-aware tool. It shines where your corpus is prose with structure: specs, policies, plans, notes, research.

## License

MIT
