# Changelog

All notable changes to wikimap. Versions follow [semantic versioning](https://semver.org/) — see [Stability](README.md#stability) for what exactly is covered by that promise.

## 1.2.0 — 2026-07-21

### Changed

- **`weak` no longer saturates — it now means what agents need it to mean: fan out.** The old formula treated partial/OR mode as weakness, but every natural-language query runs in that mode by design, so `weak: true` fired on 135/135 blind natural-language queries and carried no information. The new signal fires on *dead vocabulary* (a query token with `df: 0` — the definition of a vocabulary gap), on an empty result set, on a short query's AND→OR fallback, or on a genuinely low top score. Measured on the 135-question blind set: fires on 37/135, catches 9 of the 10 queries where fan-out rescues the answer, false-fires on 1 of the 22 where fan-out would hurt. The skill protocol changes with it: search the raw question alone first, fan out with rewrites **only when `weak: true`**. This conditional protocol scores recall@5 0.889 vs 0.830 (always-single) and 0.741 (always-fan-out) — the vocabulary-gap stratum rises 0.517 → 0.724 without diluting easy queries. On the frozen 55-question vocabulary-gap golden set: +12 recovered, 0 regressions. Document rankings are untouched (0 changes across 290 benchmark rankings and all 135 blind-set rankings).
- **Top results now carry ±2 lines of context around each matched line.** Of 51 evidence misses on the blind set, 23 had the answer line sitting *right next to* an already-displayed line (within 2 lines) — the display was cutting the answer off, not missing the document. The first three results now pick up to 8 lines (5 before) and expand each into a contiguous block with 2 lines of context either side, merged on overlap; results beyond the top three keep the compact 5-line form. Blind-set evidence@10 rose 0.474 → 0.659 (evidence@1: 0.415 → 0.607); the cost is response size (median 14 KB → 22 KB at `-n 40`), which `--compact` exists to claw back. `matched` in `--json` keeps its shape (a list of strings; blocks are newline-joined).

### Added

- **`search --compact`** — one best line per result, no context blocks: the diet mode for agent loops that only need to locate documents, not read evidence from the response.

### Fixed

- **Rankings no longer depend on the process's hash seed.** A document's matched idf mass (the tie-break key right after score) was summed by iterating a Python set, and float addition is not associative — different `PYTHONHASHSEED` draws produced last-ulp differences that flipped near-tie rankings between processes (measured: 1 seed in 24 on a fan-out benchmark query, against the tool's core determinism promise). The sum now runs in query-token order; all 24 seeds produce identical rankings on the affected queries.

### Decided against

- **Always-on fan-out** (the 1.0-era skill guidance): measured cost is recall@5 0.830 → 0.741 on 135 blind queries — rewrites dilute the top-5 precision of queries that didn't need help. Fan-out is now gated on `weak`.
- **Typo-tolerance work (fuzzy fallback on dead tokens)**: the blind set's 12 deterministic typo pairs (space collapse, adjacent-jamo substitution, cross-IME) cost wikimap a net **zero** — CJK substring matching is structurally immune to the common cases, and the conditional fan-out protocol lifts the typo stratum to 0.917 anyway. Deprioritized until a measured gap appears.
- **Rule/condition-type reinforcement**: v13's alarming 0.4 recall@5 on rule-type queries remeasured at 0.933 on n=30 — a small-sample artifact, not a weakness.

## 1.1.0 — 2026-07-15

### Changed

- **Search snippets now show the answer line, not just the echo.** Matched lines used to be the first 3 lines *of the top-scoring section* that contained a query term — but on a fact-finding benchmark, 25 of 28 evidence misses had the answer line sitting in a *different* section of a document that was already ranked correctly. Candidate lines now come from the whole document, ranked by matched idf mass (the same principle that ranks sections), displayed-section first on ties, capped at 5. Fact-benchmark evidence@10 rose from 0.135 → 0.243 (single query) and 0.189 → 0.419 (fan-out) with document rankings byte-identical (0 changes across 290 benchmark rankings) and no measurable latency cost. `matched` in `--json` keeps its shape (a list of strings); `-C` context now follows the picked lines.

### Added

- **`wikimap doctor`** — one read-only command for vault integrity: is the index behind the disk, does `semantics.jsonl` parse (malformed lines counted, unknown record types reported but kept), how many links are broken, and which pins went stale. Ends with a verdict and the command that fixes each finding. `--json` for agents.

### Decided against

- Pre-aggregating term document-frequencies at index time (a 0.15.0 leftover): profiling shows the df scan is 14 ms of a 137 ms query, the scan also produces the per-doc term prefilter that search needs anyway, and a token-level df table cannot reproduce the substring-variant matching semantics exactly. Not worth the ranking-drift risk.

## 1.0.3 — 2026-07-15

### Fixed

- **1.0.2's repin was a no-op on Windows.** The index pins text docs by the sha of their decoded text, but the repin compared against the sha of the raw bytes on disk — identical on macOS/Linux, different on Windows (CRLF), so the pre-edit match never fired and the records stayed stale (1.0.1 behavior — no corruption, the fix just didn't take). Both sides of the repin now hash in the same text domain.

## 1.0.2 — 2026-07-15

### Fixed

- **`mv` and `link add` no longer let a routine `--prune` delete records pinned to docs they rewrote.** Both commands mechanically rewrite document bytes (`mv` fixes inbound/relative links, `link add` inserts a wikilink), which silently staled every note, edge, and embedding pinned to those docs — the next `notes --prune` or `edges --prune` then deleted them. Pins that matched the pre-edit content are now repinned to the new bytes; records that were already stale stay stale.
- **`mv` broke a document's links to itself.** Moving a doc across directories rewrote its self-links to point at the old location; renaming it left self-wikilinks and self-md-links untouched. Self-links now travel with the file.
- **Anchored markdown links (`doc.md#section`) are now real links.** The parser and `mv` both ignored any md link with a `#` fragment: it never appeared in backlinks, and `mv` left it pointing at the old path. Anchored links are now indexed (anchor stripped) and rewritten on `mv` with the anchor preserved. Existing indexes reparse automatically (parser version bump); search rankings are unaffected — verified zero rank changes across all 290 benchmark rankings.

## 1.0.1 — 2026-07-15

### Fixed

- **A word repeated in a query no longer counts twice.** Repeating a token ("register … register button") inflated its document frequency by the repeat count (so its idf sank), double-added its section score, and made every-term AND matching unsatisfiable — the query always fell back to partial mode, and `--json` `terms` listed the token twice with the inflated `df`. Query tokens are now deduplicated (order preserved) and `df` is a true document count. Measured blast radius: 4 of 290 benchmark rankings moved, all of them queries that repeat a word (v5 fan-out recall@5 0.873 → 0.887, recall@1 0.493 → 0.479; v7: zero changes).

## 1.0.0 — 2026-07-13

The interface is now stable. Nothing about how wikimap works changed in this release; what changed is the commitment: **the CLI, the `--json` shapes, and the `semantics.jsonl` format won't break within 1.x.** Two data-loss bugs found while writing that guarantee down are fixed below.

### Fixed

- **`edges --prune` could delete records written by a newer wikimap.** Pruning rewrites `semantics.jsonl` — the file you commit to git — and the loader silently discarded any record type it didn't recognize. An older build running `--prune` on a vault touched by a newer one would drop that data permanently. Unknown records are now carried through every rewrite untouched.
- **`mv` orphaned a document's embedding.** Renaming a doc rewrote its notes and edges but not its `embed` record, so the vector stayed pinned to the old path and `semsearch` quietly stopped returning that document. `mv` now moves the embedding with the file.

### Changed

- `semantics.jsonl` forward compatibility is now a documented guarantee, not an accident: unknown record types are preserved on read *and* on rewrite, so new record kinds can ship in 1.x without breaking older builds.
- PyPI classifier: Beta → Production/Stable.

## 0.15.0 — 2026-07-13

- **Search is ~2× faster with byte-identical results.** Match caching (dominated-variant elimination, doc-level prefilter, one haystack build per process) cut single queries 0.30 s → 0.15 s and 3-phrasing fan-out 0.66 s → 0.26 s. Verified rank-invariant: all 148 rankings identical to 0.14.0.
- **`wikimap migrate`** — moves a graphify vault to wikimap in one command: imports the inferred edges, then removes graphify's artifacts. Dry run by default; user-authored files are never touched.

## 0.14.0 — 2026-07-13

- **Multi-query fan-out search** — `wikimap search "q" "rewrite 1" "rewrite 2"` fuses the rankings (RRF) so documents several phrasings agree on rise to the top. Closed 14 hard misses → 0 on the v5 benchmark; recall@10 0.803 → 0.944.
- Dropped the latin/CJK whitelists from PDF word extraction and `suggest` terms — every script is indexed.
- A graphify→wikimap migration skill ships alongside the main skill.

## 0.13.0 — 2026-07-09

- **Query-time semantic matching**, all deterministic and $0: corpus-derived stopword weighting (no hardcoded list, so it works in any language), document-level score rollup, OR-matching for long queries, and generic word-ending handling. First release to beat graphify on every v5 metric.
- `search --hybrid` mixes in on-demand embeddings.

## 0.12.1 — 2026-07-09

- Fix the test harness to use an absolute wikimap path for cwd-changing tests.

## 0.12.0 — 2026-07-09

- **Language-agnostic semantic search.** Agent-supplied embeddings (`embed` / `semsearch`) — wikimap stores vectors and computes cosine similarity; generating them stays with the caller, so the core still makes no LLM calls. Vectors are sha-pinned like everything else.
- Removed the last vault-specific and Korean-specific vocabulary; stopwords are now derived from your corpus.

## 0.11.0 — 2026-07-09

- **Agent-agnostic install.** `wikimap install` registers with `~/.claude/skills` *and* `~/.agents/skills` (the open agent-skills location Codex and others scan), plus `--agents-md` for tools that read `AGENTS.md`.

## 0.10.0 — 2026-07-09

- **`suggest` ranking, generation 2** — directory proximity plus filename-token idf. Link-benchmark true positives 45 → 64; rediscovery rate 67% → 85%.

## 0.9.0 — 2026-07-09

- Alias indexing, the `link add` bootstrap pipeline (suggest → confirm → insert), and `PARSER_VERSION` so a parser change forces a clean reparse instead of silently serving stale cached rows.

## 0.8.0 — 2026-07-08

- **On PyPI** (`pip install wikimap`), with trusted-publishing CI.
- Search v3: partial-match fallback and a `type:` filter.
- PDF CID/CJK text decoding via per-font ToUnicode CMaps.

## 0.4.0 – 0.7.0 — 2026-07-08

Early development: the incremental sha-diff index, `MAP.md` generation, the semantic note/edge layer, and multi-format parsing (markdown, plain text, HTML, PDF, images). Tagged but never published to PyPI.
