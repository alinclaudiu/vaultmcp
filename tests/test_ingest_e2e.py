"""End-to-end test for ``vaultmcp-master ingest``.

Builds a small VaultMesh-style markdown tree on disk, runs the
ingest command against the test DB, and verifies the imported
pages appear with the right session attribution.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import asyncpg
import pytest

pytestmark = pytest.mark.e2e


def _page(title: str, body: str = "Body.\n") -> str:
    return (
        "---\n"
        f"title: {title}\n"
        "type: app\n"
        "owners: [test]\n"
        "updated: 2026-05-10\n"
        "---\n\n"
        f"{body}"
    )


def _make_tree(root: Path) -> None:
    """Create a small wiki tree similar to what VaultMesh would hold."""
    files = {
        "apps/webstore/README.md": _page("Webstore README"),
        "apps/webstore/discounts.md": _page("Discount handling"),
        "apps/admin/README.md": _page("Admin"),
        "decisions/0001-rate-limits.md": _page("Rate limits", body="ADR body.\n"),
        "shared/glossary.md": _page("Glossary"),
        # Intentionally invalid — missing required keys.
        "broken/missing-frontmatter.md": "# Just a body\n\nNo frontmatter here.\n",
    }
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


def _resolve_master_cli() -> str:
    cli = (
        shutil.which("vaultmcp-master", path=str(Path(sys.executable).parent))
        or shutil.which("vaultmcp-master")
    )
    if cli is None:
        raise RuntimeError("vaultmcp-master CLI not found")
    return cli


def _run_cli(args: list[str], database_url: str, wiki_dir: Path) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["VAULTMCP_DATABASE_URL"] = database_url
    env["VAULTMCP_WIKI_DIR"] = str(wiki_dir)
    return subprocess.run(
        [_resolve_master_cli(), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.mark.asyncio
async def test_ingest_imports_valid_pages_and_skips_invalid(
    clean_database: str, tmp_path: Path
) -> None:
    src = tmp_path / "vaultmesh"
    src.mkdir()
    _make_tree(src)
    wiki = tmp_path / "wiki"
    wiki.mkdir()

    # Apply schema first via migrate — ingest assumes the schema exists.
    proc = _run_cli(["migrate"], clean_database, wiki)
    assert proc.returncode == 0, proc.stderr

    proc = _run_cli(
        ["ingest", "--from", str(src), "--server-id", "import-from-fooserver"],
        clean_database,
        wiki,
    )
    assert proc.returncode == 0, proc.stderr + "\n" + proc.stdout
    out = proc.stdout
    # 5 valid + 1 broken under our tree.
    assert "Imported   : 5" in out
    assert "Skipped existing: 0" in out
    assert "Skipped invalid : 1" in out
    assert "broken/missing-frontmatter.md" in out

    # Verify rows landed in DB with the right session.
    conn = await asyncpg.connect(clean_database)
    try:
        rows = await conn.fetch(
            "SELECT path, last_writer_server, last_writer_app FROM pages "
            "ORDER BY path"
        )
        rows_by_path = {r["path"]: dict(r) for r in rows}
        assert set(rows_by_path) == {
            "apps/webstore/README.md",
            "apps/webstore/discounts.md",
            "apps/admin/README.md",
            "decisions/0001-rate-limits.md",
            "shared/glossary.md",
        }
        # server_id is the --server-id we passed.
        for r in rows_by_path.values():
            assert r["last_writer_server"] == "import-from-fooserver"
        # app is auto-derived from the first segment.
        assert rows_by_path["apps/webstore/README.md"]["last_writer_app"] == "webstore"
        assert rows_by_path["apps/admin/README.md"]["last_writer_app"] == "admin"
        assert rows_by_path["decisions/0001-rate-limits.md"]["last_writer_app"] == "decisions"
        assert rows_by_path["shared/glossary.md"]["last_writer_app"] == "shared"
    finally:
        await conn.close()

    # And the wiki/ projection got rendered.
    for rel in (
        "apps/webstore/README.md",
        "apps/webstore/discounts.md",
        "decisions/0001-rate-limits.md",
    ):
        assert (wiki / rel).exists(), rel


@pytest.mark.asyncio
async def test_ingest_skip_existing_and_overwrite(
    clean_database: str, tmp_path: Path
) -> None:
    src = tmp_path / "vaultmesh"
    src.mkdir()
    (src / "apps").mkdir()
    (src / "apps/test").mkdir()
    (src / "apps/test/r.md").write_text(_page("Original"), encoding="utf-8")
    wiki = tmp_path / "wiki"
    wiki.mkdir()

    proc = _run_cli(["migrate"], clean_database, wiki)
    assert proc.returncode == 0

    # First import.
    proc = _run_cli(["ingest", "--from", str(src)], clean_database, wiki)
    assert "Imported   : 1" in proc.stdout

    # Mutate source.
    (src / "apps/test/r.md").write_text(_page("Updated"), encoding="utf-8")

    # Second import without --overwrite skips.
    proc = _run_cli(["ingest", "--from", str(src)], clean_database, wiki)
    assert "Imported   : 0" in proc.stdout
    assert "Skipped existing: 1" in proc.stdout

    # With --overwrite it bumps version.
    proc = _run_cli(
        ["ingest", "--from", str(src), "--overwrite"],
        clean_database,
        wiki,
    )
    assert "Imported   : 1" in proc.stdout

    conn = await asyncpg.connect(clean_database)
    try:
        row = await conn.fetchrow(
            "SELECT version, content FROM pages WHERE path = 'apps/test/r.md'"
        )
        assert row["version"] == 2
        assert "title: Updated" in row["content"]
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_ingest_dry_run_does_not_touch_db(
    clean_database: str, tmp_path: Path
) -> None:
    src = tmp_path / "vaultmesh"
    src.mkdir()
    (src / "shared").mkdir()
    (src / "shared/x.md").write_text(_page("X"), encoding="utf-8")
    wiki = tmp_path / "wiki"
    wiki.mkdir()

    proc = _run_cli(["migrate"], clean_database, wiki)
    assert proc.returncode == 0

    proc = _run_cli(
        ["ingest", "--from", str(src), "--dry-run"], clean_database, wiki
    )
    assert proc.returncode == 0
    assert "(dry-run)" in proc.stdout

    conn = await asyncpg.connect(clean_database)
    try:
        n = await conn.fetchval("SELECT count(*) FROM pages")
        assert n == 0
    finally:
        await conn.close()
