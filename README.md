# wikimap

**Zero-LLM incremental index + lazy semantic layer for markdown knowledge bases.**

One Python file. Zero dependencies. Zero LLM cost at build time — always. Sub-second updates, no matter how stale your index is.

Built for AI coding assistants (Claude Code and friends) working against a markdown vault: an Obsidian vault, a team wiki, a folder of specs and plans.

## Why not a knowledge-graph tool or RAG?

Tools like [graphify](https://github.com/Graphify-Labs/graphify) extract entities and relationships with an LLM **at build time** (eager extraction). That buys you inferred connections, but the bill comes on every update: change a doc, pay for re-extraction. Let the index drift for a week and your "incremental" update re-extracts half the corpus. RAG has the same eagerness problem with embeddings, plus a vector store to babysit.

wikimap inverts the design: **eager structure, lazy semantics.**

- **Structure is free.** Titles, headings, wikilinks, markdown links, requirement IDs, code-file references — all extracted by deterministic parsing. No LLM, no embeddings, no API key.
- **Semantics are earned at answer time.** When your assistant answers a question by synthesizing documents, it saves the conclusion as a *note* pinned to the source files' content hashes. When it confirms an unwritten connection between two docs, that becomes an *edge* pinned to both hashes. Change a source file and the cached knowledge goes stale automatically — it silently disappears from search results instead of feeding the model outdated facts.

The LLM cost is proportional to **what you actually asked**, never to corpus size.

## Measured (306-doc Korean/English vault, M-series Mac)

| Operation | wikimap | graphify (same vault, same change set) |
|---|---|---|
| Full index build | 0.4 s, $0 | minutes + LLM extraction cost |
| Update after editing 2 docs + deleting 1 | **0.35 s, 0 tokens** | **~95 s + 46k tokens** (measured), plus community re-labeling |
| Update after index drifted for days | still sub-second (sha-diff) | re-detected 287 of 306 files as changed → near-full re-extraction |
| Search | ~60 ms, returns section + line number + snippet | graph traversal returns entity labels; you still re-read the source files |
| Deleted file cleanup | automatic, verified | ghost nodes remained (path-prefix mismatch) |
| CJK queries | native substring matching | default query filter drops terms shorter than 4 chars — every Korean query returned 0 nodes |

## Install

```bash
curl -O https://raw.githubusercontent.com/hadonghyun/wikimap/main/wikimap.py
python3 wikimap.py install          # → ~/.claude/skills/wikimap/ (Claude Code)
cd your-vault && python3 ~/.claude/skills/wikimap/wikimap.py update
```

That's the whole thing. `install --project` writes to `./.claude` for per-repo setup. Existing `SKILL.md` customizations are never overwritten. Requires Python 3.8+, nothing else.

## Commands

| Command | What it does |
|---|---|
| `update` | Incremental re-index (sha-diff) + regenerate `MAP.md`, the one-page vault map agents read first |
| `search "query" [-n 8]` | Ranked section search — exact file:line + snippet. Fresh notes surface first |
| `links <target>` | Outlinks, backlinks, and inferred connections of a doc; or every doc mentioning a `REQ-nn` ID |
| `note add` | Save an answer-time insight, pinned to source content hashes |
| `suggest [--doc path]` | Heuristic candidates for unwritten connections: shared rare terms, shared requirement IDs, shared code references. 0.2 s, no LLM |
| `edge add` | Confirm a connection (agent judges `suggest` candidates); pinned to both files' hashes |
| `notes` / `edges` `[--all] [--prune]` | List cached semantics; stale entries are hidden by default and prunable |
| `import-graphify <graph.json>` | One-time migration of INFERRED edges from an existing graphify graph — with hash freshness retrofitted |

## How inferred connections work without eager LLM extraction

1. `suggest` proposes candidate pairs from free signals: rare terms shared by only 2–4 documents, shared requirement IDs, references to the same source files. Pairs already linked explicitly are excluded; cross-directory pairs get a boost (more surprising).
2. Your assistant reads only the top candidates for the doc that changed and confirms the real ones with `edge add`. Cost scales with the edit, not the corpus.
3. Confirmed edges appear in `links` output and `MAP.md`, and go stale automatically when either endpoint changes.

## Outputs

- `MAP.md` — vault root. Directory taxonomy, hub documents, recent changes, cross-document requirement IDs, inferred connections, fresh notes. The agent entry point.
- `.wikimap/index.db` — SQLite. Disposable; rebuild anytime with `update`.

## Scope

wikimap indexes **markdown knowledge bases**. It does not parse code ASTs — if you need a call graph of a codebase, use a code-aware tool. It shines where your corpus is prose with structure: specs, policies, plans, notes, research.

## License

MIT
