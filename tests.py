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
        out = run(self.root, "update")
        self.assertIn("4 files indexed", out)
        self.assertIn("skipped 1 non-indexed files", out)
        self.assertIn(".png 1", out)
        map_md = (self.root / "MAP.md").read_text(encoding="utf-8")
        self.assertIn("coverage: every file accounted for — 4 indexed, 1 skipped", map_md)

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
        self.assertIn("orphan docs (no links in or out): 2", map_md)
        self.assertIn("`notes/orphan-note.md`", map_md)
        self.assertIn("broken links (target missing): 2", map_md)
        self.assertIn("`[[ghost-doc]]` in specs/auth-spec.md", map_md)

    def test_edge_rescues_orphan_and_goes_stale(self):
        run(self.root, "update")
        run(self.root, "edge", "add", "--src", "notes/orphan-note.md",
            "--dst", "specs/auth-spec.md", "--rationale", "test edge")
        map_md = (self.root / "MAP.md").read_text(encoding="utf-8")
        self.assertIn("orphan docs (no links in or out): 1", map_md)
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
