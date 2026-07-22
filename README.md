# wikimap

[![ci](https://github.com/dhha22/wikimap/actions/workflows/ci.yml/badge.svg)](https://github.com/dhha22/wikimap/actions/workflows/ci.yml) [![PyPI](https://img.shields.io/pypi/v/wikimap)](https://pypi.org/project/wikimap/) [![Python](https://img.shields.io/badge/python-3.8%2B-blue)](https://pypi.org/project/wikimap/) [![license](https://img.shields.io/github/license/dhha22/wikimap)](LICENSE)

English | [한국어](README.ko.md)

**Zero-LLM incremental index + lazy semantic layer for knowledge vaults — markdown, HTML, PDF, and images.**

One Python file. Zero dependencies. Zero LLM cost at build time — always. Sub-second updates, no matter how stale your index is.

Built for AI coding assistants (Claude Code and friends) working against a knowledge vault: an Obsidian vault, a team wiki, a folder of specs, slides, and plans.

## Why not a knowledge-graph tool or RAG?

> **What's [graphify](https://github.com/Graphify-Labs/graphify)?** A tool that runs your documents through an LLM to extract concepts and the relationships between them, building a knowledge graph. It's the comparison point throughout this README.

Knowledge-graph tools like graphify, and RAG, both do their thinking **up front**: send the whole corpus through an LLM, get a graph or a vector store back. That works — but you pay for it again on every update. Change one doc, pay to re-extract. Ignore your vault for a week, and the "incremental" update quietly re-processes half of it.

wikimap flips this: **parse the structure now, learn the meaning later.**

- **Structure is free** — titles, headings, links, requirement IDs. Plain parsing, no LLM, no API key, no embeddings to maintain.
- **Meaning is earned when you ask.** When your agent works out an answer, it saves that answer. When it confirms two docs are related, it saves the link. Nothing is precomputed on the off-chance you'll need it.

The trick that makes this safe: **everything saved is stamped with the source file's content hash.** Edit the file and the stale answer disappears on its own, rather than quietly feeding your agent an outdated fact.

So the LLM cost tracks **what you actually asked**, not how big your vault is.

## Why wikimap

- **Instant, free indexing.** Full build and incremental updates finish in well under a second, with **zero LLM tokens** — graphify needs minutes and millions of tokens for the same vault.
- **More accurate search, not less.** On 135 blind questions (written by agents that only read the corpus, verified before any query ran), wikimap beats a freshly-built graphify graph on recall and puts the answer line right in the snippet.
- **Deterministic and self-cleaning.** Same input → byte-identical index; deleted files disappear on their own. No ghost nodes, no reshuffled results between runs.
- **Any language, any format.** No hardcoded stopword list, so Korean/English/mixed all work; indexes Markdown plus `.txt/.rst/.org/.adoc`, HTML, PDF, and image filenames.

| | wikimap | graphify |
|---|---|---|
| Full index build | **sub-second, $0** | minutes + LLM cost |
| Update after edits | **~0.07 s, 0 tokens** | ~95 s + tens of thousands of tokens |
| Search accuracy (recall@5, 135 blind Qs) | **0.83** | 0.57 |
| Snippet shows the answer line | yes (section + line) | no (entity labels only) |
| Determinism | byte-identical every run | non-deterministic graphs |

<sub>Measured on a Korean/English vault (M-series Mac); full methodology and the pre-registered blind benchmark are in the [changelog](CHANGELOG.md). The test suite is 136 tests, stdlib only, on macOS/Linux/Windows and Python 3.8–3.13.</sub>

**Fan-out for the hard queries.** Pass the question *plus* a rewrite or two in one call and the rankings fuse, so a document several phrasings agree on rises to the top:

```bash
wikimap search "how long do sessions last?" "session expiry" "REQ-02 timeout"
```

Your agent writes the rewrites (no extra API call), and `weak: true` in the output tells it when a query actually needs them — so easy queries stay sharp and rewrites are spent only where words are missing.

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

Either way, `wikimap install` (or `python3 wikimap.py install`) registers it with your AI agents — see below. Requires Python 3.8+, nothing else.

## Use with any AI agent

wikimap is not tied to one assistant. The core is a plain CLI (`--json` on every query command), and registration follows the open standards:

- **Claude Code, Codex, GitHub Copilot, and other [agent-skills](https://agentskills.io) tools** — `wikimap install` copies the skill (a `SKILL.md` + the tool itself) to both `~/.claude/skills/wikimap/` (Claude Code) and `~/.agents/skills/wikimap/` (the open agent-skills location that Codex and friends scan). The agent auto-discovers it and reaches for wikimap on vault questions. Pick one location with `--target claude|agents`.
- **Per-repo, shared with your team** — `wikimap install --project` writes to `./.claude` + `./.agents`; commit them and every teammate's agent gets the same setup.
- **Cursor and other tools that read `AGENTS.md`** — `wikimap install --agents-md` inserts a marker-delimited usage block into `./AGENTS.md` (idempotent: re-running refreshes the block and never touches your other content).
- **Everything else** — any agent that can run a shell command can use `wikimap search/links/path/suggest ... --json` directly; the skill file is just a usage manual, not a runtime dependency.

**Two skills get installed**, and your agent picks the right one on its own:

| Skill | Your agent reaches for it when… |
|---|---|
| `wikimap` | you ask a question about the vault, or edit it and it needs reindexing |
| `graphify-to-wikimap` | it spots a `graphify-out/` directory and you want off graphify — it drives `wikimap migrate` and then fixes up the rules and git config the command can't touch |

Customize freely: edit the installed `SKILL.md` (your vault path, language, house rules) — upgrades never overwrite an existing `SKILL.md`, only the tool itself. Both skills are preserved that way, and it's gated by tests.

## What it looks like

```console
$ wikimap update
wikimap: 304 files indexed (2 changed, 0 deleted) in 147ms | skipped 2 non-indexed files (.tsv 2) | notes: 3 fresh, 0 stale | edges: 112 fresh, 2 stale | MAP.md updated

$ wikimap search "session expiry policy"
[NOTE fresh 2026-07-02] Q: how long do sessions last?
  30 min sliding expiry; refresh token lives 14 days (REQ-02)
  sources: specs/auth-spec.md
specs/auth-spec.md:12  [Login policy]  (score 27)
  REQ-01 session expiry is 30 minutes. See [[auth-plan]].
```

Every result is a file, a line number, and the matched lines — your agent jumps straight to the right section instead of re-reading whole files. The `[NOTE fresh]` on top is a previously saved answer, served only while its source hashes still match.

## Commands

The two you'll actually type:

| Command | What it does |
|---|---|
| `update` | Re-index what changed and refresh `MAP.md`. Sub-second, $0. Run it after edits (or let the git hook do it) |
| `search "query" ["rewrite" ...]` | Find the section that answers a question. Returns file, line number, and the matching lines (top results with ±2 lines of context; `--compact` for one line per result). Pass extra phrasings to fuse them into one ranking — the JSON's `weak: true` tells you when that's worth doing |

Everything else, grouped by what it's for:

| | Command | What it does |
|---|---|---|
| **Follow connections** | `links <doc>` | What links to this, what it links to — plus every doc mentioning a `REQ-nn` ID. Each entry says whether a human wrote the link or the agent inferred it |
| | `path <a> <b>` | The shortest chain of links between two docs |
| **Grow connections** | `suggest` | Propose links that *should* exist, from free signals (shared rare terms, same requirement IDs, folder proximity). Sub-second, no LLM |
| | `link add <doc> <target>` | Write a confirmed link into the doc body. Dry run unless `--apply` |
| **Remember answers** | `note add` | Save an answer your agent worked out, pinned to the sources it came from |
| | `edge add` / `edge repin` | Confirm a connection between two docs / re-pin it after an edit |
| | `notes` / `edges` | List what's cached; stale entries hide themselves |
| **Semantic search** | `embed set` / `semsearch` | For questions that share *no words* with the answer. Your agent supplies the vectors (any model); wikimap just stores and ranks them |
| **Housekeeping** | `mv <old> <new>` | Rename a doc and rewrite every link pointing at it |
| | `fix-links` | Suggest targets for broken links (never auto-applies) |
| | `doctor` | Read-only integrity check: index freshness, semantics validity, broken links, stale pins — one verdict |
| | `install` | Register as an agent skill, or `--hook` to auto-`update` on every commit |
| | `migrate` | Move a graphify vault over in one command (see below). Dry run unless `--apply` |

Anything cached — a note, an edge, an embedding — is **pinned to the source file's content hash**. Edit that file and the cached knowledge goes stale and drops out on its own, instead of feeding your agent a stale fact.

Every query command takes `--json`. Run `wikimap <command> --help` for the full flags: phrase/field/type filters, context lines, ignore rules, and the rest.
### Coming from graphify?

```bash
wikimap migrate            # shows you exactly what it'll do
wikimap migrate --apply    # does it
```

One command: it imports the connections graphify inferred, deletes graphify's artifacts (`graphify-out/`, `.graphifyignore`), and reindexes. **Your documents are never touched** — and a file *you* wrote called `why-we-left-graphify.md` is content, not an artifact, so it stays.

The ordering matters and the command gets it right: **edges are imported before `graph.json` is deleted.** Do it by hand in the wrong order and those connections are gone for good. Imported edges come out *better* than they went in — each is pinned to both documents' content hashes, so it goes stale on its own when either doc changes, which graphify's graph never did.

Want a clean break instead? `--apply --no-import` throws the old edges away; `suggest` can rebuild candidates from scratch, deterministically and for free.

Or just tell your agent *"migrate this vault off graphify"* — `wikimap install` ships a `graphify-to-wikimap` skill that runs the command for you, then handles the parts it can't: repointing your `CLAUDE.md`/`AGENTS.md` rules at wikimap, and untracking the artifacts from git.

## How connections get discovered without an LLM

1. **`suggest` proposes candidates for free.** Two docs that share a rare term, cite the same requirement ID, or just live in the same folder are probably related. The folder structure you already built is free semantics — no LLM needed to notice it.
2. **Your agent judges only the candidates**, then writes the real ones into the doc with `link add`. It reads a shortlist, never the whole corpus — so cost scales with your edit, not your vault.
3. **Confirmed links go stale on their own** when either doc changes. Still valid after an edit? `edge repin` keeps it without retyping the rationale.

**Starting from a folder with no links at all?** Run `suggest -n 0 --json`, let your agent judge the candidates, apply the real ones with `link add`.

We tested this the hard way: took a 348-doc vault, **stripped all 949 of its human-written links**, and tried to rebuild them. The candidate sweep takes under half a second and recovers **85% of the original links** — and the LLM only ever looks at candidate pairs, never the corpus.

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
- **PDF** — text extracted with the standard library alone, no dependencies. Handles the awkward cases (CJK and subset-embedded fonts), and treats each page as its own searchable section. A scanned-image PDF can't be read by anyone without OCR — so wikimap falls back to indexing it by name and **says so in the update line**, rather than pretending it worked.
- **Images** (`.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`) — no content analysis; indexed by filename plus every **alt text** that references them (`![alt](img.png)`, `<img alt=…>`), and image references join the link graph. "Where is that checkout-flow diagram?" resolves by name or alt. `.svg` additionally contributes its `<title>`/`<desc>`/text nodes.

It does not parse code ASTs — if you need a call graph of a codebase, use a code-aware tool. It shines where your corpus is prose with structure: specs, policies, plans, notes, research.

## Stability

**1.0 means the interface is settled.** Within 1.x, these won't break:

- **The CLI** — command names, flags, and their meanings.
- **`--json` output** — existing fields keep their names and types. New fields may be *added*, so parse leniently.
- **`.wikimap/semantics.jsonl`** — the file you commit. A newer wikimap may write record types an older one doesn't know; older builds skip them and **preserve them on rewrite**, so upgrading is safe in both directions.

Deliberately **not** covered, so the tool can keep getting better:

- **`.wikimap/index.db`** — schema, tables, ranking internals. It's a disposable cache: delete it and `update` rebuilds it from your files. Don't read it directly; use the CLI.
- **Result ordering** — search rankings improve between releases. The golden set gates accuracy in CI, not exact order.

Breaking any of the first group means 2.0. See the [changelog](CHANGELOG.md).

## License

MIT
