#!/usr/bin/env python3
"""wikimap test suite — stdlib only, Python 3.8+.

Run: python3 tests.py -v
Every test drives the real CLI via subprocess against a synthetic vault,
so what passes here is exactly what a user gets.
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

WIKIMAP = str(Path(__file__).parent / "wikimap.py")


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

    def test_fix_links_suggests_close_match(self):
        write(self.root, "notes/typo.md", "# typo\n\nsee [[auth-spce]] for details")
        run(self.root, "update")
        out = run(self.root, "fix-links")
        self.assertIn("[[auth-spce]]", out)
        self.assertIn("specs/auth-spec.md", out)
        data = json.loads(run(self.root, "fix-links", "--json"))
        broken = [b for b in data["broken"] if b["link"] == "[[auth-spce]]"]
        self.assertEqual(broken[0]["candidates"][0], "specs/auth-spec.md")


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


if __name__ == "__main__":
    unittest.main()
