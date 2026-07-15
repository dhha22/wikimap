#!/usr/bin/env python3
"""wikimap test suite — stdlib only, Python 3.8+.

Run: python3 tests.py -v
Every test drives the real CLI via subprocess against a synthetic vault,
so what passes here is exactly what a user gets.
"""
import json
import os
import random
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

# absolute — tests that set cwd (install into a temp dir) must still find the tool
WIKIMAP = str((Path(__file__).parent / "wikimap.py").resolve())


def has_trigram():
    try:
        db = sqlite3.connect(":memory:")
        db.execute("CREATE VIRTUAL TABLE t USING fts5(c, tokenize='trigram')")
        return True
    except sqlite3.OperationalError:
        return False


def run(root, *cmd, env=None):
    e = dict(os.environ)
    if env:
        e.update(env)
    r = subprocess.run(
        [sys.executable, WIKIMAP, "--root", str(root), *cmd],
        capture_output=True, text=True, encoding="utf-8", env=e,
    )
    if r.returncode != 0:
        raise AssertionError(f"exit {r.returncode}: {' '.join(cmd)}\n{r.stderr}")
    return r.stdout


def write(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def write_crlf(root, rel, text):
    # write_bytes bypasses write_text's platform newline translation — a CRLF file
    # on every OS, so the Windows-only sha-domain class is reproducible locally
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.replace("\n", "\r\n").encode("utf-8"))
    return p


def make_vault(root):
    write(root, "specs/auth-spec.md", "\n".join([
        "---", "title: 인증 스펙", "---",
        "# 인증 스펙", "",
        "## 로그인 정책", "REQ-01 세션 만료는 30분. 담당은 [[auth-plan]] 참고.",
        "구현: LoginViewModel.kt", "",
        "## 토큰 갱신", "REQ-02 리프레시 토큰은 14일. see [broken](missing-doc.md) and [[ghost-doc]].",
    ]))
    write(root, "plans/auth-plan.md", "\n".join([
        "# auth plan", "",
        "## PR breakdown", "REQ-01 first, then REQ-02. Spec: [spec](../specs/auth-spec.md)",
        "path-style wikilink resolves by stem: [[specs/auth-spec]]",
        "touches LoginViewModel.kt",
    ]))
    write(root, "notes/orphan-note.md", "# 고립 문서\n\n아무도 연결하지 않은 메모. 결제 위젯 실험.")
    write(root, "notes/readme.txt", "plain text file\n\nsecond paragraph about billing widgets and payment retries in the checkout flow, long enough to matter for sectioning behavior across paragraph blocks in plain text mode here.")
    write(root, "assets/logo.png", "not really a png")


class VaultTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wikimap-test-"))
        self.root = self.tmp / "vault"
        self.root.mkdir()
        make_vault(self.root)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)


class TestUpdateAndCoverage(VaultTest):
    def test_counts_and_coverage(self):
        write(self.root, "assets/notes.xyz", "unknown extension stays skipped")
        out = run(self.root, "update")
        self.assertIn("5 files indexed", out)
        self.assertIn("skipped 1 non-indexed files", out)
        self.assertIn(".xyz 1", out)
        map_md = (self.root / "MAP.md").read_text(encoding="utf-8")
        self.assertIn("coverage: every file accounted for — 5 indexed, 1 skipped", map_md)

    def test_incremental_and_ghost_free_delete(self):
        run(self.root, "update")
        write(self.root, "specs/auth-spec.md",
              (self.root / "specs/auth-spec.md").read_text(encoding="utf-8") + "\nzxqmarker\n")
        out = run(self.root, "update")
        self.assertIn("(1 changed, 0 deleted)", out)
        self.assertIn("auth-spec.md", run(self.root, "search", "zxqmarker"))
        (self.root / "notes/orphan-note.md").unlink()
        out = run(self.root, "update")
        self.assertIn("(0 changed, 1 deleted)", out)
        self.assertIn("no results", run(self.root, "search", "고립"))

    def test_determinism(self):
        run(self.root, "update")
        dump1 = self.index_dump()
        shutil.rmtree(self.root / ".wikimap")
        run(self.root, "update")
        self.assertEqual(dump1, self.index_dump())

    def index_dump(self):
        db = sqlite3.connect(self.root / ".wikimap" / "index.db")
        try:
            return {
                t: db.execute("SELECT * FROM %s ORDER BY 1,2,3" % t).fetchall()
                for t in ("files", "sections", "links")
            }
        finally:
            db.close()


class TestSearch(VaultTest):
    def setUp(self):
        super().setUp()
        run(self.root, "update")

    def test_korean_short_term(self):
        self.assertIn("auth-spec.md", run(self.root, "search", "인증"))

    def test_multi_term_and_heading_boost(self):
        out = run(self.root, "search", "로그인 정책")
        self.assertIn("auth-spec.md", out.splitlines()[0])

    def test_filename_token(self):
        out = run(self.root, "search", "auth plan")
        self.assertIn("plans/auth-plan.md", out.splitlines()[0])

    def test_plain_text_indexed(self):
        self.assertIn("notes/readme.txt", run(self.root, "search", "billing widgets"))


class TestLinksAndTrustTags(VaultTest):
    def setUp(self):
        super().setUp()
        run(self.root, "update")

    def test_req_id(self):
        out = run(self.root, "links", "REQ-01")
        self.assertIn("appears in 2 docs", out)

    def test_linked_and_inferred_tags(self):
        run(self.root, "edge", "add", "--src", "notes/orphan-note.md",
            "--dst", "specs/auth-spec.md", "--rationale", "test edge")
        out = run(self.root, "links", "specs/auth-spec.md")
        self.assertIn("[linked|wiki]", out)
        self.assertIn("[linked|md]", out)
        self.assertIn("[inferred|conceptually_related_to|claude]", out)

    def test_path_bfs(self):
        out = run(self.root, "path", "auth-spec", "auth-plan")
        self.assertIn("(1 hops)", out)


class TestHealth(VaultTest):
    def test_health_section(self):
        run(self.root, "update")
        map_md = (self.root / "MAP.md").read_text(encoding="utf-8")
        self.assertIn("## Health", map_md)
        self.assertIn("orphan docs (no links in or out): 3", map_md)
        self.assertIn("`notes/orphan-note.md`", map_md)
        self.assertIn("broken links (target missing): 2", map_md)
        self.assertIn("`[[ghost-doc]]` in specs/auth-spec.md", map_md)

    def test_edge_rescues_orphan_and_goes_stale(self):
        run(self.root, "update")
        run(self.root, "edge", "add", "--src", "notes/orphan-note.md",
            "--dst", "specs/auth-spec.md", "--rationale", "test edge")
        map_md = (self.root / "MAP.md").read_text(encoding="utf-8")
        self.assertIn("orphan docs (no links in or out): 2", map_md)
        write(self.root, "notes/orphan-note.md", "# 고립 문서\n\n내용 변경으로 sha 불일치.")
        run(self.root, "update")
        map_md = (self.root / "MAP.md").read_text(encoding="utf-8")
        self.assertIn("stale semantics: 0 notes, 1 edges", map_md)


class TestNotes(VaultTest):
    def test_note_lifecycle(self):
        run(self.root, "update")
        run(self.root, "note", "add", "--question", "세션 만료 정책은?",
            "--insight", "30분, REQ-01", "--sources", "specs/auth-spec.md")
        self.assertIn("[NOTE fresh", run(self.root, "search", "세션 만료"))
        write(self.root, "specs/auth-spec.md", "# 인증 스펙\n\n전면 개정.")
        run(self.root, "update")
        self.assertNotIn("[NOTE fresh", run(self.root, "search", "세션 만료"))
        self.assertIn("pruned 1 stale notes", run(self.root, "notes", "--prune"))


@unittest.skipUnless(has_trigram(), "sqlite without fts5 trigram (<3.34)")
class TestFtsAtScale(VaultTest):
    def test_fts_kicks_in_and_stays_in_sync(self):
        for i in range(520):
            write(self.root, f"bulk/doc-{i:03d}.md",
                  f"# bulk doc {i}\n\nfiller text alpha beta gamma row {i}\n")
        write(self.root, "bulk/needle.md", "# needle\n\nzxqneedletoken lives here\n")
        run(self.root, "update")
        db = sqlite3.connect(self.root / ".wikimap" / "index.db")
        n = db.execute("SELECT COUNT(*) FROM sections_fts").fetchone()[0]
        db.close()
        self.assertGreater(n, 0, "FTS should be populated at >=500 docs")
        self.assertIn("bulk/needle.md", run(self.root, "search", "zxqneedletoken"))
        self.assertIn("auth-spec.md", run(self.root, "search", "인증"))
        (self.root / "bulk/needle.md").unlink()
        run(self.root, "update")
        self.assertIn("no results", run(self.root, "search", "zxqneedletoken"))


class TestRecallGate(VaultTest):
    GOLDEN = [
        ("로그인 정책", "specs/auth-spec.md"),
        ("토큰 갱신", "specs/auth-spec.md"),
        ("auth plan", "plans/auth-plan.md"),
        ("REQ-02", "specs/auth-spec.md"),
        ("payment retries", "notes/readme.txt"),
        ("결제 위젯", "notes/orphan-note.md"),
    ]

    def test_recall_at_5(self):
        run(self.root, "update")
        hits = 0
        for q, expected in self.GOLDEN:
            out = run(self.root, "search", q)
            top5 = [l for l in out.splitlines() if ":" in l and not l.startswith(" ")][:5]
            hits += any(expected in l for l in top5)
        self.assertEqual(hits, len(self.GOLDEN))


class TestIgnoreConfig(VaultTest):
    def test_trash_ignored_by_default(self):
        write(self.root, ".trash/deleted-doc.md", "# 삭제된 문서\n\nzxqtrashghost token")
        run(self.root, "update")
        self.assertIn("no results", run(self.root, "search", "zxqtrashghost"))

    def test_wikimapignore_file(self):
        write(self.root, "drafts/wip.md", "# wip\n\nzxqdraftmarker")
        write(self.root, ".synapse/cortex.json.md", "# marker\n\nzxqmarkerfile")
        write(self.root, ".wikimapignore", "# comment line\ndrafts\n.synapse/\n")
        out = run(self.root, "update")
        self.assertIn("5 files indexed", out)
        self.assertIn("no results", run(self.root, "search", "zxqdraftmarker"))
        self.assertIn("no results", run(self.root, "search", "zxqmarkerfile"))

    def test_ignore_flag_and_reindex_on_removal(self):
        write(self.root, "drafts/wip.md", "# wip\n\nzxqdraftmarker")
        run(self.root, "update", "--ignore", "drafts")
        self.assertIn("no results", run(self.root, "search", "zxqdraftmarker"))
        run(self.root, "update")
        self.assertIn("drafts/wip.md", run(self.root, "search", "zxqdraftmarker"))

    def test_glob_pattern(self):
        write(self.root, "notes/scratch.tmp.md", "# scratch\n\nzxqtmpmarker")
        write(self.root, ".wikimapignore", "*.tmp.md\n")
        run(self.root, "update")
        self.assertIn("no results", run(self.root, "search", "zxqtmpmarker"))


class TestMapPlacement(VaultTest):
    def test_map_path_moves_and_persists(self):
        run(self.root, "update")
        self.assertTrue((self.root / "MAP.md").exists())
        out = run(self.root, "update", "--map-path", ".wikimap/MAP.md")
        self.assertIn(".wikimap/MAP.md updated", out)
        self.assertFalse((self.root / "MAP.md").exists(), "old generated map should be removed")
        self.assertIn("# Wiki Map", (self.root / ".wikimap/MAP.md").read_text(encoding="utf-8"))
        out = run(self.root, "update")
        self.assertIn(".wikimap/MAP.md updated", out)
        self.assertFalse((self.root / "MAP.md").exists(), "setting must persist across runs")

    def test_custom_map_not_indexed(self):
        run(self.root, "update", "--map-path", "docs/vault-map.md")
        out = run(self.root, "update")
        self.assertIn("5 files indexed", out)
        self.assertIn("no results", run(self.root, "search", "auto-generated"))

    def test_no_map(self):
        run(self.root, "update")
        out = run(self.root, "update", "--no-map")
        self.assertIn("map disabled", out)
        self.assertFalse((self.root / "MAP.md").exists())
        run(self.root, "update", "--map-path", "MAP.md")
        self.assertTrue((self.root / "MAP.md").exists())

    def test_user_file_at_old_map_path_survives(self):
        write(self.root, "MAP.md", "# my hand-written map\n")
        run(self.root, "update", "--map-path", ".wikimap/MAP.md")
        self.assertEqual(
            (self.root / "MAP.md").read_text(encoding="utf-8"), "# my hand-written map\n"
        )


class TestHtmlIndexing(VaultTest):
    def setUp(self):
        super().setUp()
        write(self.root, "reports/quarterly.html", "\n".join([
            "<!doctype html><html><head>",
            "<title>분기 리포트 Q3</title>",
            "<style>body { color: red; } .zxqcssnoise {}</style>",
            "<script>var zxqjsnoise = 1;</script>",
            "</head><body>",
            "<h1>분기 리포트</h1>",
            "<p>매출 요약과 결제 전환율 분석. REQ-01 반영 결과.</p>",
            "<h2>세부 지표</h2>",
            "<p>zxqhtmlneedle 지표는 <a href='../specs/auth-spec.md'>인증 스펙</a> 참고.</p>",
            "<p>대시보드는 <a href='dashboard.html'>여기</a>, 외부는 <a href='https://x.com/a.md'>링크</a>.</p>",
            "</body></html>",
        ]))
        write(self.root, "reports/dashboard.html",
              "<html><head><title>대시보드</title></head><body><p>지표 모음 zxqdash</p></body></html>")
        run(self.root, "update")

    def test_indexed_and_searchable(self):
        out = run(self.root, "update")
        self.assertIn("7 files indexed", out)
        self.assertNotIn(".html", out.split("|")[1], "html must not appear in the skipped list")
        hit = run(self.root, "search", "zxqhtmlneedle")
        self.assertIn("reports/quarterly.html", hit)
        self.assertIn("세부 지표", hit, "heading sectioning should survive tag stripping")

    def test_title_and_noise_stripped(self):
        out = run(self.root, "search", "분기 리포트")
        self.assertIn("reports/quarterly.html", out)
        self.assertIn("no results", run(self.root, "search", "zxqcssnoise"))
        self.assertIn("no results", run(self.root, "search", "zxqjsnoise"))

    def test_anchor_links_join_graph(self):
        out = run(self.root, "links", "reports/quarterly.html")
        self.assertIn("[linked|md] specs/auth-spec.md", out)
        self.assertIn("[linked|md] reports/dashboard.html", out)
        self.assertNotIn("x.com", out)
        out = run(self.root, "path", "dashboard", "auth-plan")
        self.assertIn("hops)", out)

    def test_req_id_from_html(self):
        self.assertIn("appears in 3 docs", run(self.root, "links", "REQ-01"))


class TestSuggestWikilink(VaultTest):
    def test_paste_ready_output(self):
        write(self.root, "a/topic-alpha.md", "# alpha\n\nzxqsharedterm appears here twice zxqsharedterm")
        write(self.root, "b/topic-beta.md", "# beta\n\nzxqsharedterm appears here twice zxqsharedterm")
        run(self.root, "update")
        out = run(self.root, "suggest", "--doc", "a/topic-alpha.md", "--wikilink")
        self.assertIn("[[topic-beta]]", out)
        self.assertNotIn("edge add --src", out.splitlines()[0])


class TestSuggestProximity(VaultTest):
    def suggest_pairs(self, *cmd):
        out = json.loads(run(self.root, "suggest", "-n", "0", "--json", *cmd))
        return {tuple(sorted((c["a"], c["b"]))): c for c in out["candidates"]}

    def test_same_dir_pair_enumerated_without_shared_terms(self):
        write(self.root, "policy/feed/feed-list-policy.md", "# 리스트 정책\n\n스크롤 페이징 zxqaaa")
        write(self.root, "policy/feed/feed-detail-policy.md", "# 상세 정책\n\n댓글 노출 zxqbbb")
        run(self.root, "update")
        pairs = self.suggest_pairs()
        key = ("policy/feed/feed-detail-policy.md", "policy/feed/feed-list-policy.md")
        self.assertIn(key, pairs)
        self.assertEqual(pairs[key]["dir"], "same")
        self.assertIn("dir:same", pairs[key]["signals"])

    def test_name_token_overlap_outranks_bare_same_dir(self):
        write(self.root, "policy/feed/feed-list-logging-policy.md", "# a\n\nzxqccc")
        write(self.root, "policy/feed/feed-detail-logging-policy.md", "# b\n\nzxqddd")
        write(self.root, "policy/feed/unrelated-thing.md", "# c\n\nzxqeee")
        run(self.root, "update")
        pairs = self.suggest_pairs()
        logging_pair = pairs[("policy/feed/feed-detail-logging-policy.md",
                             "policy/feed/feed-list-logging-policy.md")]
        bare_pair = pairs[("policy/feed/feed-list-logging-policy.md",
                          "policy/feed/unrelated-thing.md")]
        self.assertGreater(logging_pair["score"], bare_pair["score"])
        self.assertTrue(any(s.startswith("name:") for s in logging_pair["signals"]))

    def test_shared_term_signal_is_script_agnostic(self):
        # far-dir pair with disjoint filenames: the only bridge is a shared rare
        # Cyrillic term — the content tokenizer must not be a script whitelist
        write(self.root, "reports/q1.md", "# Бюджетирование квартала\n\nzxqjjj")
        write(self.root, "archive/z9.md", "# Бюджетирование прошлое\n\nzxqkkk")
        run(self.root, "update")
        pairs = self.suggest_pairs()
        key = ("archive/z9.md", "reports/q1.md")
        self.assertIn(key, pairs)
        self.assertTrue(any("бюджетирование" in s for s in pairs[key]["signals"]),
                        pairs.get(key))

    def test_sibling_dir_pair_enumerated(self):
        write(self.root, "policy/feed/feed-list-policy.md", "# a\n\nzxqfff")
        write(self.root, "policy/comment/comment-state-policy.md", "# b\n\nzxqggg")
        run(self.root, "update")
        pairs = self.suggest_pairs()
        key = ("policy/comment/comment-state-policy.md", "policy/feed/feed-list-policy.md")
        self.assertIn(key, pairs)
        self.assertEqual(pairs[key]["dir"], "sibling")

    def test_linked_same_dir_pair_excluded(self):
        write(self.root, "policy/feed/feed-list-policy.md", "# a\n\n[[feed-detail-policy]] zxqhhh")
        write(self.root, "policy/feed/feed-detail-policy.md", "# b\n\nzxqiii")
        run(self.root, "update")
        pairs = self.suggest_pairs()
        self.assertNotIn(("policy/feed/feed-detail-policy.md", "policy/feed/feed-list-policy.md"),
                         pairs)

    def test_images_not_enumerated(self):
        write(self.root, "assets/diagram.png", "fake png bytes")
        run(self.root, "update")
        pairs = self.suggest_pairs()
        for a, b in pairs:
            self.assertFalse(a.endswith(".png") or b.endswith(".png"))


class TestSemanticsFileSSOT(VaultTest):
    def setUp(self):
        super().setUp()
        run(self.root, "update")

    def add_semantics(self):
        run(self.root, "note", "add", "--question", "세션 만료 정책은?",
            "--insight", "30분, REQ-01", "--sources", "specs/auth-spec.md")
        run(self.root, "edge", "add", "--src", "notes/orphan-note.md",
            "--dst", "specs/auth-spec.md", "--rationale", "test edge")

    def test_note_and_edge_land_in_jsonl(self):
        self.add_semantics()
        p = self.root / ".wikimap" / "semantics.jsonl"
        recs = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()]
        self.assertEqual({r["type"] for r in recs}, {"note", "edge"})

    def test_db_is_a_disposable_cache(self):
        self.add_semantics()
        (self.root / ".wikimap" / "index.db").unlink()
        run(self.root, "update")
        self.assertIn("[NOTE fresh", run(self.root, "search", "세션 만료"))
        out = run(self.root, "edges")
        self.assertIn("[fresh|claude]", out)
        self.assertIn("test edge", out)

    def test_migration_from_pre_060_db(self):
        db = sqlite3.connect(self.root / ".wikimap" / "index.db")
        sha = db.execute("SELECT sha FROM files WHERE path='specs/auth-spec.md'").fetchone()[0]
        sha2 = db.execute("SELECT sha FROM files WHERE path='plans/auth-plan.md'").fetchone()[0]
        db.execute(
            "INSERT INTO edges(src,dst,relation,rationale,origin,created,src_sha,dst_sha)"
            " VALUES('plans/auth-plan.md','specs/auth-spec.md','rel','legacy row','claude','x',?,?)",
            (sha2, sha),
        )
        db.commit()
        db.close()
        self.assertFalse((self.root / ".wikimap" / "semantics.jsonl").exists())
        out = run(self.root, "edges")
        self.assertIn("legacy row", out)
        recs = [json.loads(l) for l in
                (self.root / ".wikimap" / "semantics.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["rationale"], "legacy row")

    def test_prune_rewrites_the_file(self):
        self.add_semantics()
        write(self.root, "notes/orphan-note.md", "# 고립 문서\n\nsha가 바뀌어 엣지가 stale.")
        run(self.root, "update")
        run(self.root, "edges", "--prune")
        recs = [json.loads(l) for l in
                (self.root / ".wikimap" / "semantics.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([r["type"] for r in recs], ["note"], "stale edge must leave the file too")

    def test_a_record_type_from_a_newer_wikimap_is_skipped_not_fatal(self):
        self.add_semantics()
        p = self.root / ".wikimap" / "semantics.jsonl"
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"type": "cluster-from-the-future", "payload": {"x": 1}}) + "\n")
        (self.root / ".wikimap" / "index.db").unlink()
        run(self.root, "update")
        self.assertIn("[NOTE fresh", run(self.root, "search", "세션 만료"))
        self.assertIn("test edge", run(self.root, "edges"))

    def test_prune_never_drops_a_record_type_it_doesnt_understand(self):
        self.add_semantics()
        p = self.root / ".wikimap" / "semantics.jsonl"
        future = {"type": "cluster-from-the-future", "payload": {"x": 1}}
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(future) + "\n")
        write(self.root, "notes/orphan-note.md", "# 고립 문서\n\nsha가 바뀌어 엣지가 stale.")
        run(self.root, "update")
        run(self.root, "edges", "--prune")
        recs = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()]
        self.assertIn(future, recs,
                      "prune rewrites the SSOT file — an unknown record must be carried through, "
                      "or an older build would silently delete a newer build's data")


class TestEdgeRepin(VaultTest):
    def test_repin_keeps_rationale_and_refreshes_shas(self):
        run(self.root, "update")
        run(self.root, "edge", "add", "--src", "notes/orphan-note.md",
            "--dst", "specs/auth-spec.md", "--rationale", "connection survives edits")
        write(self.root, "notes/orphan-note.md", "# 고립 문서\n\n편집 후에도 관계는 유효.")
        run(self.root, "update")
        self.assertIn("1 stale edges hidden", run(self.root, "edges"))
        out = run(self.root, "edge", "repin", "--src", "notes/orphan-note.md",
                  "--dst", "specs/auth-spec.md")
        self.assertIn("repinned 1 edge(s)", out)
        out = run(self.root, "edges")
        self.assertIn("[fresh|claude]", out)
        self.assertIn("connection survives edits", out)
        map_md = (self.root / "MAP.md").read_text(encoding="utf-8")
        self.assertIn("stale semantics: 0 notes, 0 edges", map_md)


class TestSemanticsRoundtrip(VaultTest):
    """mv / prune / rebuild must never lose or silently stale a record they didn't target."""

    def setUp(self):
        super().setUp()
        write(self.root, "aa/target.md", "# target\n\nplain content\n")
        write(self.root, "aa/mover.md", "# mover\n\nsee [other](target.md)\n")
        write(self.root, "bb/referrer.md", "# referrer\n\nlink: [mover](../aa/mover.md)\n")
        run(self.root, "update")

    def jsonl(self):
        p = self.root / ".wikimap" / "semantics.jsonl"
        return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]

    def test_mv_repins_records_when_it_rewrites_the_referrer(self):
        run(self.root, "edge", "add", "--src", "bb/referrer.md",
            "--dst", "aa/target.md", "--rationale", "pinned to referrer")
        run(self.root, "mv", "aa/mover.md", "aa/renamed.md", "--apply")
        out = run(self.root, "edges")
        self.assertIn("pinned to referrer", out)
        self.assertNotIn("stale", out,
                         "mv rewrote the referrer's link itself — records pinned to that "
                         "doc must be repinned, or a routine --prune deletes them")

    def test_mv_repins_records_when_it_rewrites_own_links(self):
        run(self.root, "edge", "add", "--src", "aa/mover.md",
            "--dst", "aa/target.md", "--rationale", "pinned to mover")
        run(self.root, "note", "add", "--question", "q1", "--insight", "i1",
            "--sources", "aa/mover.md")
        run(self.root, "embed", "set", "aa/mover.md", "--vector", "[1.0, 0.0]")
        run(self.root, "mv", "aa/mover.md", "bb/mover.md", "--apply")
        self.assertNotIn("stale", run(self.root, "edges"))
        self.assertNotIn("stale", run(self.root, "notes"))
        self.assertNotIn("stale", run(self.root, "embed", "status"))

    def test_mv_does_not_resurrect_already_stale_records(self):
        run(self.root, "edge", "add", "--src", "bb/referrer.md",
            "--dst", "aa/target.md", "--rationale", "goes stale first")
        write(self.root, "bb/referrer.md",
              "# referrer\n\nedited body\n\nlink: [mover](../aa/mover.md)\n")
        run(self.root, "update")
        self.assertIn("1 stale edges hidden", run(self.root, "edges"))
        run(self.root, "mv", "aa/mover.md", "aa/renamed.md", "--apply")
        self.assertIn("1 stale edges hidden", run(self.root, "edges"),
                      "a record stale before mv must stay stale — repin only pins that "
                      "matched the pre-mv content")

    def test_mv_carries_unknown_records_through_rewrite(self):
        run(self.root, "note", "add", "--question", "q1", "--insight", "i1",
            "--sources", "aa/mover.md")
        future = {"type": "cluster-from-the-future", "payload": {"x": 1}}
        with (self.root / ".wikimap" / "semantics.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(future) + "\n")
        run(self.root, "mv", "aa/mover.md", "bb/mover.md", "--apply")
        self.assertIn(future, self.jsonl())

    def test_mv_keeps_edge_fresh_and_sorted_when_rename_flips_order(self):
        run(self.root, "edge", "add", "--src", "bb/referrer.md",
            "--dst", "aa/target.md", "--rationale", "flip test")
        run(self.root, "mv", "bb/referrer.md", "a0/referrer.md", "--apply")
        data = json.loads(run(self.root, "edges", "--json"))
        e = next(x for x in data["edges"] if x["rationale"] == "flip test")
        self.assertTrue(e["fresh"])
        self.assertLess(e["src"], e["dst"])
        rec = next(r for r in self.jsonl() if r["type"] == "edge")
        self.assertLess(rec["src"], rec["dst"])

    def test_link_add_repins_records_pinned_to_the_edited_doc(self):
        run(self.root, "note", "add", "--question", "q1", "--insight", "i1",
            "--sources", "aa/target.md")
        run(self.root, "edge", "add", "--src", "aa/target.md",
            "--dst", "aa/mover.md", "--rationale", "survives link add")
        run(self.root, "embed", "set", "aa/target.md", "--vector", "[1.0, 0.0]")
        run(self.root, "link", "add", "aa/target.md", "bb/referrer.md", "--apply")
        self.assertNotIn("stale", run(self.root, "notes"))
        self.assertNotIn("stale", run(self.root, "edges"),
                         "link add rewrote the doc itself — pinned records must be "
                         "repinned, or a routine --prune deletes them")
        self.assertNotIn("stale", run(self.root, "embed", "status"))
        run(self.root, "notes", "--prune")
        run(self.root, "edges", "--prune")
        types = [r["type"] for r in self.jsonl()]
        self.assertIn("note", types)
        self.assertIn("edge", types)

    def test_link_add_repins_a_crlf_document(self):
        write_crlf(self.root, "aa/target.md", "# target\n\nplain content\n")
        run(self.root, "update")
        run(self.root, "note", "add", "--question", "q1", "--insight", "i1",
            "--sources", "aa/target.md")
        run(self.root, "embed", "set", "aa/target.md", "--vector", "[1.0, 0.0]")
        run(self.root, "link", "add", "aa/target.md", "bb/referrer.md", "--apply")
        self.assertNotIn("stale", run(self.root, "notes"),
                         "pins live in the decoded-text sha domain — a CRLF doc must "
                         "repin exactly like an LF one (1.0.3's Windows-only no-op)")
        self.assertNotIn("stale", run(self.root, "embed", "status"))

    def test_mv_repins_crlf_documents_it_rewrites(self):
        write_crlf(self.root, "aa/mover.md", "# mover\n\nsee [other](target.md)\n")
        write_crlf(self.root, "bb/referrer.md",
                   "# referrer\n\nlink: [mover](../aa/mover.md)\n")
        run(self.root, "update")
        run(self.root, "edge", "add", "--src", "bb/referrer.md",
            "--dst", "aa/target.md", "--rationale", "pinned to crlf referrer")
        run(self.root, "note", "add", "--question", "q2", "--insight", "i2",
            "--sources", "aa/mover.md")
        run(self.root, "mv", "aa/mover.md", "bb/mover.md", "--apply")
        self.assertNotIn("stale", run(self.root, "edges"),
                         "mv rewrote a CRLF referrer — repin must fire in the text "
                         "sha domain, not raw disk bytes")
        self.assertNotIn("stale", run(self.root, "notes"))

    def test_notes_prune_keeps_stale_edges(self):
        run(self.root, "note", "add", "--question", "q1", "--insight", "i1",
            "--sources", "aa/target.md")
        run(self.root, "edge", "add", "--src", "aa/target.md",
            "--dst", "aa/mover.md", "--rationale", "kind isolation")
        write(self.root, "aa/target.md", "# target\n\nedited: both go stale\n")
        run(self.root, "update")
        run(self.root, "notes", "--prune")
        types = [r["type"] for r in self.jsonl()]
        self.assertNotIn("note", types)
        self.assertIn("edge", types,
                      "notes --prune must only remove notes — the stale edge is "
                      "edges --prune's business")

    def test_prune_compacts_a_repinned_edge_and_keeps_the_latest_pin(self):
        run(self.root, "edge", "add", "--src", "aa/target.md",
            "--dst", "aa/mover.md", "--rationale", "survives compaction")
        run(self.root, "edge", "add", "--src", "aa/target.md",
            "--dst", "bb/referrer.md", "--rationale", "left to rot")
        write(self.root, "aa/target.md", "# target\n\nedited\n")
        run(self.root, "update")
        run(self.root, "edge", "repin", "--src", "aa/target.md", "--dst", "aa/mover.md")
        self.assertEqual(len([r for r in self.jsonl() if r["type"] == "edge"]), 3,
                         "the log is append-only — repin adds a line, history stays "
                         "until a prune rewrite")
        run(self.root, "edges", "--prune")
        edges = [r for r in self.jsonl() if r["type"] == "edge"]
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0]["rationale"], "survives compaction")
        self.assertIn("repinned", edges[0],
                      "prune's rewrite must keep the latest pin, not resurrect the "
                      "stale original")
        self.assertIn("[fresh|claude]", run(self.root, "edges"))

    def test_cache_rebuild_reproduces_semantics_after_lifecycle(self):
        run(self.root, "note", "add", "--question", "q1", "--insight", "i1",
            "--sources", "aa/target.md")
        run(self.root, "edge", "add", "--src", "aa/target.md",
            "--dst", "aa/mover.md", "--rationale", "lifecycle")
        run(self.root, "embed", "set", "aa/target.md", "--vector", "[0.5, 0.5]")
        write(self.root, "aa/mover.md", "# mover\n\nedited\n\nsee [other](target.md)\n")
        run(self.root, "update")
        run(self.root, "edge", "repin", "--src", "aa/target.md", "--dst", "aa/mover.md")
        run(self.root, "mv", "aa/target.md", "bb/target.md", "--apply")
        run(self.root, "edges", "--prune")
        run(self.root, "notes", "--prune")

        def snapshot():
            notes = json.loads(run(self.root, "notes", "--json"))
            for n in notes["notes"]:
                n.pop("id", None)  # cache rowid, not SSOT data — renumbers on rebuild
            return (notes, json.loads(run(self.root, "edges", "--json")),
                    run(self.root, "embed", "status"))

        before = snapshot()
        (self.root / ".wikimap" / "index.db").unlink()
        run(self.root, "update")
        self.assertEqual(before, snapshot(),
                         "index.db is a disposable cache — a rebuild from semantics.jsonl "
                         "must reproduce notes/edges/embeds exactly")


class TestRoundtripProperty(unittest.TestCase):
    """Randomized op sequences over a clean vault: wikimap's own writes must never
    stale a pin, prune must never delete anything, and no link may break."""

    SEEDS = (7, 42)
    OPS_PER_SEED = 10

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wikimap-prop-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def build_vault(self, root):
        root.mkdir()
        write(root, "a/one.md", "# one\n\nsee [two](two.md) and [[three]]\n")
        write(root, "a/two.md", "# two\n\nbody about tokens\n")
        write(root, "b/three.md", "# three\n\nlink: [one](../a/one.md#one)\n")
        write(root, "b/four.md", "# four\n\nplain body\n")
        write(root, "five.md", "# five\n\n[[four]] mention\n")
        run(root, "update")
        return ["a/one.md", "a/two.md", "b/three.md", "b/four.md", "five.md"]

    def check_invariants(self, root, note_count, edge_pairs):
        notes = json.loads(run(root, "notes", "--json"))["notes"]
        self.assertEqual(len(notes), note_count)
        self.assertTrue(all(n["fresh"] for n in notes))
        edges = json.loads(run(root, "edges", "--json"))["edges"]
        self.assertEqual(len(edges), len(edge_pairs))
        self.assertTrue(all(e["fresh"] for e in edges))
        status = run(root, "embed", "status")
        self.assertNotIn("stale", status)
        self.assertEqual(json.loads(run(root, "fix-links", "--json"))["broken"], [])

    def drive(self, seed):
        rng = random.Random(seed)
        root = self.tmp / f"vault-{seed}"
        docs = self.build_vault(root)
        note_count = 0
        edge_pairs = set()  # keyed by doc index, stable across mv renames
        for i in range(self.OPS_PER_SEED):
            op = rng.choice(["mv", "link", "note", "edge", "embed", "prune", "update"])
            if op == "mv":
                j = rng.randrange(len(docs))
                new = "m{}/doc{}-{}.md".format(seed, seed, i)
                run(root, "mv", docs[j], new, "--apply")
                docs[j] = new
            elif op == "link":
                a, b = rng.sample(docs, 2)
                run(root, "link", "add", a, b, "--apply")
            elif op == "note":
                note_count += 1
                run(root, "note", "add", "--question", f"q{i}",
                    "--insight", f"i{i}", "--sources", rng.choice(docs))
            elif op == "edge":
                ja, jb = rng.sample(range(len(docs)), 2)
                if frozenset((ja, jb)) in edge_pairs:
                    continue
                edge_pairs.add(frozenset((ja, jb)))
                run(root, "edge", "add", "--src", docs[ja], "--dst", docs[jb],
                    "--rationale", f"r{i}")
            elif op == "embed":
                run(root, "embed", "set", rng.choice(docs), "--vector", f"[{i}.0, 1.0]")
            elif op == "prune":
                run(root, "notes", "--prune")
                run(root, "edges", "--prune")
            else:
                run(root, "update")
        self.check_invariants(root, note_count, edge_pairs)
        run(root, "notes", "--prune")
        run(root, "edges", "--prune")
        self.check_invariants(root, note_count, edge_pairs)  # prune of a healthy vault must be a no-op

    def test_random_op_sequences_never_stale_lose_or_break(self):
        for seed in self.SEEDS:
            with self.subTest(seed=seed):
                self.drive(seed)


class TestJsonOutput(VaultTest):
    def setUp(self):
        super().setUp()
        run(self.root, "update")

    def test_search_json(self):
        data = json.loads(run(self.root, "search", "로그인 정책", "--json"))
        self.assertEqual(data["query"], "로그인 정책")
        top = data["results"][0]
        self.assertEqual(top["path"], "specs/auth-spec.md")
        for key in ("line", "heading", "score", "matched"):
            self.assertIn(key, top)

    def test_repeated_query_token_counts_once(self):
        single = json.loads(run(self.root, "search", "로그인 정책", "--json"))
        doubled = json.loads(run(self.root, "search", "로그인 로그인 정책", "--json"))
        self.assertEqual(doubled["terms"], single["terms"])
        self.assertFalse(doubled["partial"])
        self.assertEqual(doubled["results"][0]["path"], single["results"][0]["path"])

    def test_links_json(self):
        data = json.loads(run(self.root, "links", "REQ-01", "--json"))
        self.assertEqual(len(data["docs"]), 2)
        data = json.loads(run(self.root, "links", "specs/auth-spec.md", "--json"))
        self.assertTrue(any(l["source"] == "plans/auth-plan.md" for l in data["backlinks"]))

    def test_path_json(self):
        data = json.loads(run(self.root, "path", "auth-spec", "auth-plan", "--json"))
        self.assertTrue(data["found"])
        self.assertEqual(data["hops"], 1)
        self.assertEqual(len(data["chain"]), 2)

    def test_suggest_notes_edges_json(self):
        run(self.root, "note", "add", "--question", "q", "--insight", "i",
            "--sources", "specs/auth-spec.md")
        run(self.root, "edge", "add", "--src", "notes/orphan-note.md",
            "--dst", "specs/auth-spec.md", "--rationale", "r")
        self.assertEqual(json.loads(run(self.root, "notes", "--json"))["notes"][0]["question"], "q")
        self.assertEqual(json.loads(run(self.root, "edges", "--json"))["edges"][0]["rationale"], "r")
        self.assertIn("candidates", json.loads(run(self.root, "suggest", "--json")))


class TestFanOutSearch(VaultTest):
    def setUp(self):
        super().setUp()
        run(self.root, "update")

    def test_single_query_json_shape_unchanged(self):
        d = json.loads(run(self.root, "search", "세션", "--json"))
        self.assertEqual(d["query"], "세션")
        self.assertNotIn("fused", d)
        self.assertNotIn("queries", d)

    def test_terms_report_df_per_token(self):
        d = json.loads(run(self.root, "search", "세션 파랑나비", "--json"))
        df = {t["term"]: t["df"] for t in d["terms"]}
        self.assertGreaterEqual(df["세션"], 1)
        self.assertEqual(df["파랑나비"], 0)

    def test_dead_terms_hint_in_human_output(self):
        out = run(self.root, "search", "파랑나비")
        self.assertIn("no results", out)
        self.assertIn("no corpus hits for: 파랑나비", out)

    def test_fusion_unions_docs_across_phrasings(self):
        # each phrasing alone reaches a different doc; the fused ranking has both
        d = json.loads(run(self.root, "search", "결제 위젯", "billing widgets", "--json"))
        self.assertTrue(d["fused"])
        self.assertEqual([q["query"] for q in d["queries"]], ["결제 위젯", "billing widgets"])
        paths = [r["path"] for r in d["results"]]
        self.assertIn("notes/orphan-note.md", paths)
        self.assertIn("notes/readme.txt", paths)

    def test_fusion_prefers_agreement(self):
        d = json.loads(run(self.root, "search", "세션 만료", "로그인 정책", "--json"))
        top = d["results"][0]
        self.assertEqual(top["path"], "specs/auth-spec.md")
        self.assertEqual(top["sources"], "2/2")

    def test_fused_per_query_terms_feedback(self):
        d = json.loads(run(self.root, "search", "세션 파랑나비", "세션 만료", "--json"))
        q0 = d["queries"][0]
        self.assertEqual({t["term"]: t["df"] for t in q0["terms"]}["파랑나비"], 0)
        df1 = {t["term"]: t["df"] for t in d["queries"][1]["terms"]}
        self.assertTrue(all(v >= 1 for v in df1.values()), df1)


class TestQueryLanguage(VaultTest):
    def setUp(self):
        super().setUp()
        write(self.root, "specs/tagged-spec.md", "\n".join([
            "---", "title: 결제 스펙", "tags: [payment, android]", "---",
            "# 결제 스펙", "", "환불은 30일 이내. 세션 문구가 여기도 있지만 만료 얘기는 아님.",
        ]))
        run(self.root, "update")

    def test_phrase_vs_scattered_words(self):
        self.assertIn("auth-spec.md", run(self.root, "search", '"세션 만료"'))
        out = run(self.root, "search", '"만료는 세션"')
        self.assertIn("no results", out)

    def test_field_prefixes(self):
        out = run(self.root, "search", "title:결제")
        self.assertIn("tagged-spec.md", out)
        self.assertNotIn("orphan-note", out, "결제 in body must not match title: filter")
        out = run(self.root, "search", "path:plans REQ-01")
        headers = [l for l in out.splitlines() if not l.startswith(" ")]
        self.assertTrue(any("auth-plan.md" in h for h in headers))
        self.assertFalse(any("auth-spec.md" in h for h in headers))
        out = run(self.root, "search", "heading:로그인")
        self.assertIn("auth-spec.md", out)

    def test_tag_filter_and_map_summary(self):
        out = run(self.root, "search", "tag:payment")
        self.assertIn("tagged-spec.md", out)
        self.assertIn("no results", run(self.root, "search", "tag:ios"))
        map_md = (self.root / "MAP.md").read_text(encoding="utf-8")
        self.assertIn("## Tags", map_md)
        self.assertIn("`payment` (1)", map_md)

    def test_type_filter(self):
        self.assertIn("assets/logo.png", run(self.root, "search", "type:image logo"))
        self.assertIn("auth-spec.md", run(self.root, "search", "세션 type:md"))
        self.assertIn("no results", run(self.root, "search", "세션 type:text"))
        self.assertIn("notes/readme.txt", run(self.root, "search", "billing type:text"))
        with self.assertRaises(AssertionError):
            run(self.root, "search", "type:docx foo")

    def test_partial_fallback_on_zero_and_results(self):
        out = run(self.root, "search", "세션 만료 스크린샷")
        self.assertIn("auth-spec.md", out)
        self.assertIn("partial 2/3", out)
        self.assertNotIn("partial", run(self.root, "search", "세션 만료"),
                         "full-match results must never carry a partial marker")

    def test_partial_requires_majority(self):
        self.assertIn("no results",
                      run(self.root, "search", "만료 zx존재안함 qq없음 ww없음"),
                      "1/4 matched is below majority — must not surface")

    def test_partial_keeps_field_filters_hard(self):
        out = run(self.root, "search", "세션 만료 스크린샷 type:text")
        self.assertIn("no results", out,
                      "type: filter stays hard even in partial fallback")

    def test_partial_json_flag(self):
        data = json.loads(run(self.root, "search", "세션 만료 스크린샷", "--json"))
        self.assertTrue(data["partial"])
        self.assertEqual(data["results"][0]["partial"], "2/3")
        data = json.loads(run(self.root, "search", "세션 만료", "--json"))
        self.assertFalse(data["partial"])
        self.assertNotIn("partial", data["results"][0])


PDF_WITH_TEXT = (
    b"%PDF-1.1\n1 0 obj\n<< /Length 80 >>\nstream\n"
    b"BT /F1 12 Tf (zxqpdfneedle quarterly payment summary REQ-77 rollout) Tj ET\n"
    b"endstream\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF\n"
)
PDF_SCANNED = b"%PDF-1.4\n\x89\x50\x4e\x47\x00\x01\x02binary scan blob no text operators\n%%EOF\n"


class TestPdfIndexing(VaultTest):
    def test_text_pdf_searchable_and_req_extracted(self):
        (self.root / "reports").mkdir()
        (self.root / "reports/quarterly-report.pdf").write_bytes(PDF_WITH_TEXT)
        out = run(self.root, "update")
        self.assertIn("6 files indexed", out)
        self.assertNotIn("text-extraction failed", out)
        self.assertIn("reports/quarterly-report.pdf", run(self.root, "search", "zxqpdfneedle"))
        self.assertIn("reports/quarterly-report.pdf", run(self.root, "links", "REQ-77"))

    def test_scanned_pdf_name_only_and_honest_coverage(self):
        (self.root / "scans").mkdir()
        (self.root / "scans/계약서-2026-스캔본.pdf").write_bytes(PDF_SCANNED)
        out = run(self.root, "update")
        self.assertIn("pdf text-extraction failed: 1 (indexed name+path only)", out)
        self.assertIn("계약서-2026-스캔본.pdf", run(self.root, "search", "계약서"))
        self.assertIn("no results", run(self.root, "search", "binary scan blob"),
                      "binary noise must never leak into the index")

    def test_non_latin_non_cjk_pdf_text_indexed(self):
        # the word-count gate must accept any script, not a whitelist — a PDF
        # written entirely in Cyrillic (UTF-16BE literals) is real text
        cyr = "Отчет платеж бюджет квартал компания"
        lit = b"\xfe\xff" + cyr.encode("utf-16-be")
        pdf = (b"%PDF-1.1\n1 0 obj\n<< /Length 120 >>\nstream\n"
               b"BT /F1 12 Tf (" + lit + b") Tj ET\n"
               b"endstream\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF\n")
        (self.root / "reports").mkdir()
        (self.root / "reports/otchet.pdf").write_bytes(pdf)
        out = run(self.root, "update")
        self.assertNotIn("text-extraction failed", out)
        self.assertIn("reports/otchet.pdf", run(self.root, "search", "платеж"))


def make_pdf(*objects):
    body = b"%PDF-1.4\n"
    for i, obj in enumerate(objects, 1):
        body += b"%d 0 obj %s endobj\n" % (i, obj)
    return body + b"trailer << >>\n%%EOF\n"


CID_CMAP = (b"<< >> stream\n"
            b"1 begincodespacerange <0000> <FFFF> endcodespacerange\n"
            b"3 beginbfchar <0041> <D55C> <0042> <AE00> <0001> <0020> endbfchar\n"
            b"1 beginbfrange <0050> <0052> <AC00> endbfrange\n"
            b"endstream")


class TestPdfCmapDecoding(VaultTest):
    def test_cid_hex_tj_decoded_via_bfchar_and_bfrange(self):
        pdf = make_pdf(
            b"<< /Type /Page /Resources << /Font << /F1 3 0 R >> >> /Contents 2 0 R >>",
            b"<< >> stream\nBT /F1 12 Tf "
            b"<00410042 0001 00410042 0001 00410042 0001 00410042 0001 00410042 0001 005000510052> Tj"
            b" ET\nendstream",
            b"<< /Type /Font /Subtype /Type0 /ToUnicode 4 0 R >>",
            CID_CMAP,
        )
        (self.root / "docs").mkdir()
        (self.root / "docs/cid.pdf").write_bytes(pdf)
        out = run(self.root, "update")
        self.assertNotIn("text-extraction failed", out)
        self.assertIn("docs/cid.pdf", run(self.root, "search", "한글"))
        self.assertIn("docs/cid.pdf", run(self.root, "search", "가각갂"),
                      "bfrange-mapped codes must decode too")

    def test_per_font_cmaps_not_unioned(self):
        # same code 0x0041 means a different char in each font — union decoding
        # would corrupt one of them (the spike's 'Rakdrensbd' failure)
        pdf = make_pdf(
            b"<< /Type /Page /Resources << /Font << /F1 3 0 R /F2 5 0 R >> >>"
            b" /Contents 2 0 R >>",
            b"<< >> stream\nBT /F1 12 Tf <00410041 0001 00410041 0001 00410041> Tj "
            b"/F2 12 Tf <00410041 0001 00410041 0001 00410041> Tj ET\nendstream",
            b"<< /Type /Font /ToUnicode 4 0 R >>",
            b"<< >> stream\n1 begincodespacerange <0000> <FFFF> endcodespacerange\n"
            b"2 beginbfchar <0041> <B098> <0001> <0020> endbfchar\nendstream",
            b"<< /Type /Font /ToUnicode 6 0 R >>",
            b"<< >> stream\n1 begincodespacerange <0000> <FFFF> endcodespacerange\n"
            b"2 beginbfchar <0041> <B2E4> <0001> <0020> endbfchar\nendstream",
        )
        (self.root / "docs").mkdir()
        (self.root / "docs/two-fonts.pdf").write_bytes(pdf)
        run(self.root, "update")
        self.assertIn("docs/two-fonts.pdf", run(self.root, "search", "나나"))
        self.assertIn("docs/two-fonts.pdf", run(self.root, "search", "다다"))

    def test_ascii85_flate_filter_chain(self):
        import base64
        content = zlib.compress(b"BT (quarterly zebra target metrics dashboard alpha) Tj ET")
        encoded = base64.a85encode(content) + b"~>"
        pdf = make_pdf(
            b"<< /Type /Page /Contents 2 0 R >>",
            b"<< /Filter [/ASCII85Decode /FlateDecode] >> stream\n" + encoded + b"\nendstream",
        )
        (self.root / "docs").mkdir()
        (self.root / "docs/a85.pdf").write_bytes(pdf)
        run(self.root, "update")
        self.assertIn("docs/a85.pdf", run(self.root, "search", "zebra target metrics"))

    def test_form_xobject_text_reached(self):
        pdf = make_pdf(
            b"<< /Type /Page /Resources << /XObject << /X1 2 0 R >> >> >>",
            b"<< /Subtype /Form /Resources << /Font << /F1 3 0 R >> >> stream\n"
            b"BT /F1 12 Tf <00410042 0001 00410042 0001 00410042 0001 00410042 0001 00410042> Tj"
            b" ET\nendstream",
            b"<< /Type /Font /Subtype /Type0 /ToUnicode 4 0 R >>",
            CID_CMAP,
        )
        (self.root / "docs").mkdir()
        (self.root / "docs/form.pdf").write_bytes(pdf)
        run(self.root, "update")
        self.assertIn("docs/form.pdf", run(self.root, "search", "한글"),
                      "text living only in a Form XObject must be indexed")

    def test_type3_one_byte_literal_tj(self):
        pdf = make_pdf(
            b"<< /Type /Page /Resources << /Font << /F1 3 0 R >> >> /Contents 2 0 R >>",
            b"<< >> stream\nBT /F1 12 Tf (AB AB AB AB AB) Tj ET\nendstream",
            b"<< /Type /Font /Subtype /Type3 /ToUnicode 4 0 R >>",
            b"<< >> stream\n1 begincodespacerange <00> <FF> endcodespacerange\n"
            b"3 beginbfchar <41> <D0C0> <42> <C790> <20> <0020> endbfchar\nendstream",
        )
        (self.root / "docs").mkdir()
        (self.root / "docs/type3.pdf").write_bytes(pdf)
        run(self.root, "update")
        self.assertIn("docs/type3.pdf", run(self.root, "search", "타자"),
                      "1-byte literal Tj codes must decode through the Type3 ToUnicode")


class TestImageIndexing(VaultTest):
    def setUp(self):
        super().setUp()
        write(self.root, "assets/checkout-flow-v2.png", "fake png bytes")
        write(self.root, "docs/checkout.md", "\n".join([
            "# 결제 문서", "",
            "플로우는 ![결제 승인 전체 플로우 다이어그램](../assets/checkout-flow-v2.png) 참고.",
        ]))
        write(self.root, "assets/arch.svg",
              "<svg xmlns='http://www.w3.org/2000/svg'><title>모듈 아키텍처 zxqsvgtitle</title>"
              "<text>presentation domain data</text></svg>")
        run(self.root, "update")

    def test_filename_and_alt_searchable(self):
        self.assertIn("assets/checkout-flow-v2.png", run(self.root, "search", "checkout flow"))
        self.assertIn("assets/checkout-flow-v2.png", run(self.root, "search", "승인 전체 플로우"))

    def test_img_link_joins_graph(self):
        out = run(self.root, "links", "assets/checkout-flow-v2.png")
        self.assertIn("[linked|img] docs/checkout.md", out)
        map_md = (self.root / "MAP.md").read_text(encoding="utf-8")
        self.assertNotIn("`assets/checkout-flow-v2.png`", map_md.split("## Health")[1],
                         "referenced image must not be an orphan")

    def test_svg_title_indexed(self):
        self.assertIn("assets/arch.svg", run(self.root, "search", "zxqsvgtitle"))

    def test_alt_updates_when_doc_changes(self):
        write(self.root, "docs/checkout.md",
              "# 결제 문서\n\n![환불 예외 처리 흐름](../assets/checkout-flow-v2.png)")
        run(self.root, "update")
        self.assertIn("assets/checkout-flow-v2.png", run(self.root, "search", "환불 예외 처리"))
        self.assertIn("no results", run(self.root, "search", "승인 전체 플로우"))


class TestMvAndFixLinks(VaultTest):
    def test_mv_dry_run_writes_nothing(self):
        run(self.root, "update")
        out = run(self.root, "mv", "specs/auth-spec.md", "archive/auth-spec.md")
        self.assertIn("dry run", out)
        self.assertTrue((self.root / "specs/auth-spec.md").exists())

    def test_mv_apply_rewrites_references_and_semantics(self):
        run(self.root, "update")
        run(self.root, "edge", "add", "--src", "notes/orphan-note.md",
            "--dst", "specs/auth-spec.md", "--rationale", "moves with the file")
        run(self.root, "mv", "specs/auth-spec.md", "archive/auth-spec-v2.md", "--apply")
        self.assertFalse((self.root / "specs/auth-spec.md").exists())
        plan = (self.root / "plans/auth-plan.md").read_text(encoding="utf-8")
        self.assertIn("[spec](../archive/auth-spec-v2.md)", plan)
        self.assertIn("[[archive/auth-spec-v2]]", plan)
        moved = (self.root / "archive/auth-spec-v2.md").read_text(encoding="utf-8")
        self.assertIn("[[auth-plan]]", moved)
        out = run(self.root, "edges")
        self.assertIn("archive/auth-spec-v2.md", out)
        self.assertIn("[fresh|claude]", out, "content unchanged — shas must stay valid")
        self.assertIn("(1 hops)", run(self.root, "path", "auth-spec-v2", "auth-plan"))

    def test_mv_carries_the_embedding_to_the_new_path(self):
        run(self.root, "update")
        run(self.root, "embed", "set", "specs/auth-spec.md", "--vector", "[1.0, 0.0]")
        run(self.root, "mv", "specs/auth-spec.md", "archive/auth-spec.md", "--apply")
        out = run(self.root, "semsearch", "--vector", "[1.0, 0.0]")
        self.assertIn("archive/auth-spec.md", out,
                      "a renamed doc must keep its embedding, not orphan it at the old path")

    def test_mv_rewrites_self_links_to_the_new_location(self):
        write(self.root, "notes/selfy.md",
              "# selfy\n\nwiki [[selfy]], md [top](selfy.md), anchored [s](selfy.md#selfy)\n")
        run(self.root, "update")
        run(self.root, "mv", "notes/selfy.md", "archive/renamed.md", "--apply")
        moved = (self.root / "archive/renamed.md").read_text(encoding="utf-8")
        self.assertIn("[[renamed]]", moved)
        self.assertIn("[top](renamed.md)", moved,
                      "a self-link must travel with the file, not point back at the "
                      "old location")
        self.assertIn("[s](renamed.md#selfy)", moved)
        out = run(self.root, "fix-links")
        self.assertNotIn("selfy", out)
        self.assertNotIn("renamed", out)

    def test_mv_rewrites_anchored_links_and_keeps_the_anchor(self):
        write(self.root, "notes/anchored.md",
              "# anchored\n\nonly [s](../specs/auth-spec.md#scope) here\n")
        run(self.root, "update")
        self.assertIn("notes/anchored.md", run(self.root, "links", "specs/auth-spec.md"),
                      "an anchored md link is still a link — it must be indexed")
        run(self.root, "mv", "specs/auth-spec.md", "archive/auth-spec.md", "--apply")
        text = (self.root / "notes/anchored.md").read_text(encoding="utf-8")
        self.assertIn("[s](../archive/auth-spec.md#scope)", text)

    def test_fix_links_suggests_close_match(self):
        write(self.root, "notes/typo.md", "# typo\n\nsee [[auth-spce]] for details")
        run(self.root, "update")
        out = run(self.root, "fix-links")
        self.assertIn("[[auth-spce]]", out)
        self.assertIn("specs/auth-spec.md", out)
        data = json.loads(run(self.root, "fix-links", "--json"))
        broken = [b for b in data["broken"] if b["link"] == "[[auth-spce]]"]
        self.assertEqual(broken[0]["candidates"][0], "specs/auth-spec.md")


class TestDottedFilenameLinks(VaultTest):
    def setUp(self):
        super().setUp()
        write(self.root, "plans/wikimap-0.6.0-plan.md",
              "# wikimap 0.6.0 plan\n\ndotted filename target")
        write(self.root, "notes/pointer.md",
              "# pointer\n\nsee [[wikimap-0.6.0-plan]] and [[plans/wikimap-0.6.0-plan]]")
        run(self.root, "update")

    def test_wikilink_to_dotted_name_resolves(self):
        out = run(self.root, "links", "plans/wikimap-0.6.0-plan.md")
        self.assertIn("notes/pointer.md", out)
        self.assertIn("(1 hops)", run(self.root, "path", "pointer", "wikimap-0.6.0-plan"))

    def test_dotted_link_not_reported_broken(self):
        data = json.loads(run(self.root, "fix-links", "--json"))
        links = [b["link"] for b in data["broken"]]
        self.assertNotIn("[[wikimap-0.6.0-plan]]", links)
        self.assertNotIn("[[plans/wikimap-0.6.0-plan]]", links)

    def test_mv_updates_inbound_links_to_dotted_name(self):
        run(self.root, "mv", "plans/wikimap-0.6.0-plan.md",
            "archive/plans/wikimap-0.6.0-plan.md", "--apply")
        txt = (self.root / "notes/pointer.md").read_text(encoding="utf-8")
        self.assertIn("[[archive/plans/wikimap-0.6.0-plan]]", txt)
        self.assertNotIn("[[plans/wikimap-0.6.0-plan]]", txt)

    def test_explicit_md_extension_in_wikilink_still_resolves(self):
        write(self.root, "notes/ext-link.md", "# ext\n\nsee [[wikimap-0.6.0-plan.md]]")
        run(self.root, "update")
        out = run(self.root, "links", "plans/wikimap-0.6.0-plan.md")
        self.assertIn("notes/ext-link.md", out)


class TestInstallHook(VaultTest):
    def test_appends_to_existing_hook(self):
        run(self.root, "update")
        hooks = self.root / ".git" / "hooks"
        hooks.mkdir(parents=True)
        custom = "#!/bin/sh\necho my-existing-hook\n"
        (hooks / "post-commit").write_text(custom, encoding="utf-8")
        run(self.root, "install", "--hook")
        text = (hooks / "post-commit").read_text(encoding="utf-8")
        self.assertIn("echo my-existing-hook", text, "existing hook must be preserved")
        self.assertIn("wikimap", text)
        out = run(self.root, "install", "--hook")
        self.assertIn("already installed", out)
        self.assertEqual(text, (hooks / "post-commit").read_text(encoding="utf-8"))

    def test_fresh_hook_and_non_git_vault(self):
        (self.root / ".git").mkdir()
        run(self.root, "install", "--hook")
        hook = self.root / ".git" / "hooks" / "post-commit"
        self.assertIn("update", hook.read_text(encoding="utf-8"))
        with self.assertRaises(AssertionError):
            run(self.tmp, "install", "--hook")


class TestInstallPreservesSkill(unittest.TestCase):
    def test_existing_skill_untouched(self):
        tmp = Path(tempfile.mkdtemp(prefix="wikimap-home-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        skill_dir = tmp / ".claude" / "skills" / "wikimap"
        skill_dir.mkdir(parents=True)
        custom = "# my customized SKILL.md — do not touch\n"
        (skill_dir / "SKILL.md").write_text(custom, encoding="utf-8")
        env = dict(os.environ, HOME=str(tmp), USERPROFILE=str(tmp))
        r = subprocess.run(
            [sys.executable, WIKIMAP, "install"],
            capture_output=True, text=True, encoding="utf-8", env=env,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("kept existing", r.stdout)
        self.assertEqual((skill_dir / "SKILL.md").read_text(encoding="utf-8"), custom)
        self.assertTrue((skill_dir / "wikimap.py").exists())

    def test_fresh_install_writes_skill(self):
        tmp = Path(tempfile.mkdtemp(prefix="wikimap-home-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        env = dict(os.environ, HOME=str(tmp), USERPROFILE=str(tmp))
        r = subprocess.run(
            [sys.executable, WIKIMAP, "install"],
            capture_output=True, text=True, encoding="utf-8", env=env,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        skill = tmp / ".claude" / "skills" / "wikimap" / "SKILL.md"
        self.assertIn("name: wikimap", skill.read_text(encoding="utf-8"))

    def test_fresh_install_writes_migration_skill(self):
        tmp = Path(tempfile.mkdtemp(prefix="wikimap-home-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        env = dict(os.environ, HOME=str(tmp), USERPROFILE=str(tmp))
        r = subprocess.run(
            [sys.executable, WIKIMAP, "install"],
            capture_output=True, text=True, encoding="utf-8", env=env,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        migrate = tmp / ".claude" / "skills" / "graphify-to-wikimap" / "SKILL.md"
        self.assertIn("name: graphify-to-wikimap", migrate.read_text(encoding="utf-8"))

    def test_existing_migration_skill_untouched(self):
        tmp = Path(tempfile.mkdtemp(prefix="wikimap-home-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        mig_dir = tmp / ".claude" / "skills" / "graphify-to-wikimap"
        mig_dir.mkdir(parents=True)
        custom = "# my customized migration SKILL — do not touch\n"
        (mig_dir / "SKILL.md").write_text(custom, encoding="utf-8")
        env = dict(os.environ, HOME=str(tmp), USERPROFILE=str(tmp))
        r = subprocess.run(
            [sys.executable, WIKIMAP, "install"],
            capture_output=True, text=True, encoding="utf-8", env=env,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((mig_dir / "SKILL.md").read_text(encoding="utf-8"), custom)

    def test_install_from_console_script_like_pipx(self):
        # a pipx/uv install runs `import wikimap; wikimap.main()` with the module
        # living in a venv's site-packages — install must still copy itself and
        # write SKILL.md to the user's ~/.claude, not somewhere venv-relative
        tmp = Path(tempfile.mkdtemp(prefix="wikimap-home-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        site = tmp / "venv" / "lib" / "site-packages"
        site.mkdir(parents=True)
        shutil.copy(WIKIMAP, site / "wikimap.py")
        env = dict(os.environ, HOME=str(tmp), USERPROFILE=str(tmp), PYTHONPATH=str(site))
        r = subprocess.run(
            [sys.executable, "-c",
             "import sys, wikimap; sys.argv = ['wikimap', 'install']; wikimap.main()"],
            capture_output=True, text=True, encoding="utf-8", env=env, cwd=str(tmp),
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        dest = tmp / ".claude" / "skills" / "wikimap"
        self.assertIn("name: wikimap", (dest / "SKILL.md").read_text(encoding="utf-8"))
        copied = (dest / "wikimap.py").read_text(encoding="utf-8")
        self.assertEqual(copied, (site / "wikimap.py").read_text(encoding="utf-8"))


class TestInstallMultiTarget(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wikimap-home-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.env = dict(os.environ, HOME=str(self.tmp), USERPROFILE=str(self.tmp))

    def install(self, *extra):
        r = subprocess.run(
            [sys.executable, WIKIMAP, "install", *extra],
            capture_output=True, text=True, encoding="utf-8", env=self.env, cwd=str(self.tmp),
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def test_default_installs_both_standard_locations(self):
        self.install()
        for base in (".claude", ".agents"):
            d = self.tmp / base / "skills" / "wikimap"
            self.assertTrue((d / "wikimap.py").exists(), base)
            text = (d / "SKILL.md").read_text(encoding="utf-8")
            self.assertIn("name: wikimap", text)
            self.assertNotIn("__WIKIMAP__", text, "command path placeholder must be substituted")
            self.assertIn(str(d / "wikimap.py"), text)

    def test_target_agents_only(self):
        self.install("--target", "agents")
        self.assertTrue((self.tmp / ".agents" / "skills" / "wikimap" / "SKILL.md").exists())
        self.assertFalse((self.tmp / ".claude").exists())

    def test_preservation_is_per_target(self):
        claude_skill = self.tmp / ".claude" / "skills" / "wikimap" / "SKILL.md"
        claude_skill.parent.mkdir(parents=True)
        custom = "# my customized SKILL.md\n"
        claude_skill.write_text(custom, encoding="utf-8")
        out = self.install()
        self.assertIn("kept existing", out)
        self.assertEqual(claude_skill.read_text(encoding="utf-8"), custom)
        agents_skill = self.tmp / ".agents" / "skills" / "wikimap" / "SKILL.md"
        self.assertIn("name: wikimap", agents_skill.read_text(encoding="utf-8"))

    def test_project_installs_under_cwd(self):
        self.install("--project")
        self.assertTrue((self.tmp / ".claude" / "skills" / "wikimap" / "SKILL.md").exists())
        self.assertTrue((self.tmp / ".agents" / "skills" / "wikimap" / "SKILL.md").exists())

    def test_ships_the_migration_skill_alongside(self):
        self.install()
        for base in (".claude", ".agents"):
            p = self.tmp / base / "skills" / "graphify-to-wikimap" / "SKILL.md"
            self.assertTrue(p.exists(), base)
            text = p.read_text(encoding="utf-8")
            self.assertIn("name: graphify-to-wikimap", text)
            # 스킬이 낡은 수동 절차가 아니라 실제 명령어를 안내해야 한다
            self.assertIn("wikimap migrate", text)

    def test_migration_skill_customizations_are_preserved(self):
        p = self.tmp / ".claude" / "skills" / "graphify-to-wikimap" / "SKILL.md"
        p.parent.mkdir(parents=True)
        custom = "# my migration notes\n"
        p.write_text(custom, encoding="utf-8")
        self.install()
        self.assertEqual(p.read_text(encoding="utf-8"), custom)


class TestInstallAgentsMd(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wikimap-agentsmd-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def install_agents_md(self):
        r = subprocess.run(
            [sys.executable, WIKIMAP, "install", "--agents-md"],
            capture_output=True, text=True, encoding="utf-8", cwd=str(self.tmp),
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        return (self.tmp / "AGENTS.md").read_text(encoding="utf-8")

    def test_creates_when_missing(self):
        text = self.install_agents_md()
        self.assertIn("<!-- wikimap:start -->", text)
        self.assertIn("wikimap search", text)

    def test_appends_preserving_existing_content(self):
        (self.tmp / "AGENTS.md").write_text("# my project rules\n\nkeep me\n", encoding="utf-8")
        text = self.install_agents_md()
        self.assertIn("keep me", text)
        self.assertTrue(text.index("keep me") < text.index("<!-- wikimap:start -->"))

    def test_rerun_refreshes_block_without_duplication(self):
        (self.tmp / "AGENTS.md").write_text("before\n", encoding="utf-8")
        first = self.install_agents_md()
        second = self.install_agents_md()
        self.assertEqual(first, second)
        self.assertEqual(second.count("<!-- wikimap:start -->"), 1)
        self.assertIn("before", second)


class TestAliases(VaultTest):
    def setUp(self):
        super().setUp()
        write(self.root, "profile/resume-ja.md", "\n".join([
            "---", "title: 職務経歴書", "aliases: [일본어 경력기술서, japanese resume]", "---",
            "# 職務経歴書", "", "## アプリ起動時間", "起動時間を58%短縮した。",
        ]))
        write(self.root, "notes/block-alias.md", "\n".join([
            "---", "title: block doc", "aliases:", "  - 블록별칭", "  - second name", "---",
            "# block doc", "", "본문 내용.",
        ]))
        run(self.root, "update")

    def test_alias_matches_at_title_weight(self):
        out = run(self.root, "search", "일본어 경력기술서")
        self.assertTrue(out.splitlines()[0].startswith("profile/resume-ja.md"), out)

    def test_block_list_form(self):
        self.assertIn("notes/block-alias.md", run(self.root, "search", "블록별칭"))
        self.assertIn("notes/block-alias.md", run(self.root, "search", "second name"))

    def test_title_field_filter_matches_alias(self):
        self.assertIn("profile/resume-ja.md",
                      run(self.root, "search", 'title:"japanese resume"'))

    def test_alias_wikilink_resolves(self):
        write(self.root, "notes/pointer.md", "# pointer\n\n[[일본어 경력기술서]] 참고.")
        run(self.root, "update")
        self.assertIn("notes/pointer.md", run(self.root, "links", "profile/resume-ja.md"))
        map_md = (self.root / "MAP.md").read_text(encoding="utf-8")
        self.assertNotIn("일본어 경력기술서", map_md.split("Health")[-1])

    def test_real_file_stem_wins_alias_collision(self):
        write(self.root, "notes/impostor.md", "\n".join([
            "---", "aliases: [auth-plan]", "---", "# impostor", "", "본문.",
        ]))
        run(self.root, "update")
        out = run(self.root, "links", "plans/auth-plan.md")
        self.assertIn("specs/auth-spec.md", out)


class TestLinkAdd(VaultTest):
    def setUp(self):
        super().setUp()
        run(self.root, "update")

    def test_dry_run_writes_nothing(self):
        before = (self.root / "notes/orphan-note.md").read_text(encoding="utf-8")
        out = run(self.root, "link", "add", "notes/orphan-note.md", "auth-plan")
        self.assertIn("dry run", out)
        self.assertEqual(before, (self.root / "notes/orphan-note.md").read_text(encoding="utf-8"))

    def test_apply_creates_related_section_then_idempotent(self):
        run(self.root, "link", "add", "notes/orphan-note.md", "auth-plan", "--apply")
        text = (self.root / "notes/orphan-note.md").read_text(encoding="utf-8")
        self.assertTrue(text.endswith("## Related\n- [[auth-plan]]\n"), text)
        self.assertIn("notes/orphan-note.md", run(self.root, "links", "plans/auth-plan.md"))
        out = run(self.root, "link", "add", "notes/orphan-note.md", "auth-plan")
        self.assertIn("already linked", out)
        self.assertIn("nothing to add", out)
        self.assertEqual(text, (self.root / "notes/orphan-note.md").read_text(encoding="utf-8"))

    def test_reuses_existing_english_link_section(self):
        write(self.root, "notes/hub.md", "\n".join([
            "# hub", "", "본문 단락.", "",
            "## See Also", "- [[auth-plan]]", "",
            "## 다른 섹션", "이 내용은 그대로 남아야 한다.",
        ]))
        run(self.root, "update")
        run(self.root, "link", "add", "notes/hub.md", "auth-spec", "--apply")
        lines = (self.root / "notes/hub.md").read_text(encoding="utf-8").splitlines()
        sec = lines.index("## See Also")
        nxt = lines.index("## 다른 섹션")
        self.assertIn("- [[auth-spec]]", lines[sec:nxt])
        self.assertIn("이 내용은 그대로 남아야 한다.", lines[nxt:])

    def test_reuses_named_section_any_language(self):
        # non-English section headings work via explicit --section (no locale baked in)
        write(self.root, "notes/hub2.md", "\n".join([
            "# hub", "", "본문 단락.", "",
            "## 관련 문서", "- [[auth-plan]]", "",
            "## 다른 섹션", "이 내용은 그대로 남아야 한다.",
        ]))
        run(self.root, "update")
        run(self.root, "link", "add", "notes/hub2.md", "auth-spec",
            "--section", "관련 문서", "--apply")
        lines = (self.root / "notes/hub2.md").read_text(encoding="utf-8").splitlines()
        sec = lines.index("## 관련 문서")
        nxt = lines.index("## 다른 섹션")
        self.assertIn("- [[auth-spec]]", lines[sec:nxt])
        self.assertIn("이 내용은 그대로 남아야 한다.", lines[nxt:])

    def test_multiple_targets_path_and_stem(self):
        run(self.root, "link", "add", "notes/orphan-note.md",
            "specs/auth-spec.md", "auth-plan", "--apply")
        text = (self.root / "notes/orphan-note.md").read_text(encoding="utf-8")
        self.assertIn("- [[auth-spec]]", text)
        self.assertIn("- [[auth-plan]]", text)

    def test_alias_target(self):
        write(self.root, "profile/resume-ja.md", "\n".join([
            "---", "aliases: [일본어 경력기술서]", "---", "# 職務経歴書", "", "本文.",
        ]))
        run(self.root, "update")
        run(self.root, "link", "add", "notes/orphan-note.md", "일본어 경력기술서", "--apply")
        self.assertIn("- [[resume-ja]]",
                      (self.root / "notes/orphan-note.md").read_text(encoding="utf-8"))

    def test_unknown_target_fails(self):
        with self.assertRaises(AssertionError):
            run(self.root, "link", "add", "notes/orphan-note.md", "no-such-doc")

    def test_self_link_is_skipped(self):
        out = run(self.root, "link", "add", "notes/orphan-note.md", "orphan-note", "--apply")
        self.assertIn("nothing to add", out)


class TestParserVersionRescan(VaultTest):
    def test_stale_cache_is_fully_reparsed(self):
        write(self.root, "profile/resume-ja.md", "\n".join([
            "---", "aliases: [일본어 경력기술서]", "---", "# 職務経歴書", "", "本文.",
        ]))
        run(self.root, "update")
        db = sqlite3.connect(self.root / ".wikimap" / "index.db")
        db.execute("UPDATE meta SET value='0' WHERE key='parser_version'")
        db.execute("DELETE FROM aliases")  # simulate a cache built by an older parser
        db.commit()
        db.close()
        out = run(self.root, "update")
        self.assertIn("(6 changed, 0 deleted)", out)
        self.assertIn("profile/resume-ja.md", run(self.root, "search", "일본어 경력기술서"))


class TestMigrateFromGraphify(VaultTest):
    def setUp(self):
        super().setUp()
        graph = {
            "nodes": [
                {"id": "n1", "label": "session expiry", "source_file": "specs/auth-spec.md"},
                {"id": "n2", "label": "auth plan", "source_file": "plans/auth-plan.md"},
            ],
            "links": [{"source": "n1", "target": "n2",
                       "confidence": "INFERRED", "relation": "implements"}],
        }
        write(self.root, "graphify-out/graph.json", json.dumps(graph))
        write(self.root, "graphify-out/cache/blob.bin", "cached junk")
        write(self.root, ".graphifyignore", "node_modules\n")
        # 이름에 graphify가 들어가지만 사용자가 쓴 문서 — 절대 지워지면 안 된다
        write(self.root, "notes/why-we-left-graphify.md", "# graphify 회고\n\n내가 쓴 문서.")
        run(self.root, "update")

    def test_dry_run_writes_nothing(self):
        out = run(self.root, "migrate")
        self.assertIn("dry run", out)
        self.assertIn("graphify-out/", out)
        self.assertIn(".graphifyignore", out)
        self.assertTrue((self.root / "graphify-out/graph.json").exists())
        self.assertTrue((self.root / ".graphifyignore").exists())

    def test_apply_imports_edges_before_deleting_the_graph(self):
        out = run(self.root, "migrate", "--apply")
        self.assertIn("imported 1 doc-pair edges", out)
        self.assertFalse((self.root / "graphify-out").exists())
        self.assertFalse((self.root / ".graphifyignore").exists())
        # graph.json 이 지워졌어도 엣지는 살아 있어야 한다 (순서가 뒤집히면 영구 유실)
        edges = run(self.root, "edges")
        self.assertIn("graphify-import", edges)
        self.assertIn("specs/auth-spec.md", edges)
        self.assertIn("plans/auth-plan.md", edges)

    def test_never_deletes_user_authored_files(self):
        run(self.root, "migrate", "--apply")
        keep = self.root / "notes/why-we-left-graphify.md"
        self.assertTrue(keep.exists(), "filename containing 'graphify' is not an artifact")
        self.assertTrue((self.root / "specs/auth-spec.md").exists())

    def test_no_import_discards_edges(self):
        out = run(self.root, "migrate", "--no-import", "--apply")
        self.assertNotIn("imported", out)
        self.assertFalse((self.root / "graphify-out").exists())
        self.assertNotIn("graphify-import", run(self.root, "edges"))

    def test_idempotent_when_nothing_to_migrate(self):
        run(self.root, "migrate", "--apply")
        out = run(self.root, "migrate")
        self.assertIn("no graphify artifacts found", out)


if __name__ == "__main__":
    unittest.main()
