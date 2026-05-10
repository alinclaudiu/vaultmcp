"""On-disk rendering of wiki pages.

Master keeps the canonical content in Postgres, but also writes each page
to disk under :attr:`MasterConfig.wiki_dir`. This serves three purposes:

1. Humans can browse the wiki with any markdown editor without going
   through the API.
2. The rendered tree is a normal git working tree — Phase 4 will optionally
   commit it on every write, providing an audit trail.
3. If Postgres ever gets corrupted, the on-disk tree is a recoverable
   backup (you can re-ingest it).

The rendering is intentionally trivial — no templating, no transformation.
The DB row's content IS the file content; metadata lives in the YAML
frontmatter at the top of the content already.
"""

from __future__ import annotations

import os
from pathlib import Path


def render_to_disk(wiki_dir: Path, path: str, content: str) -> None:
    """Write a page to ``<wiki_dir>/<path>``, creating directories as needed.

    Path traversal is prevented: the resolved target must stay under
    ``wiki_dir``. Writes are atomic (write-temp + rename).
    """
    wiki_dir = wiki_dir.resolve()
    target = (wiki_dir / path).resolve()

    # Path-traversal defense.
    try:
        target.relative_to(wiki_dir)
    except ValueError as exc:
        raise ValueError(
            f"Path '{path}' escapes wiki_dir '{wiki_dir}'"
        ) from exc

    target.parent.mkdir(parents=True, exist_ok=True)

    # Atomic write: write to a sibling temp file, then rename.
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, target)


def remove_from_disk(wiki_dir: Path, path: str) -> None:
    """Remove a page from disk. No-op if it doesn't exist.

    Used by future delete handling (out of scope for v0.2 but signature is
    stable). Path-traversal defense applies as in :func:`render_to_disk`.
    """
    wiki_dir = wiki_dir.resolve()
    target = (wiki_dir / path).resolve()

    try:
        target.relative_to(wiki_dir)
    except ValueError as exc:
        raise ValueError(
            f"Path '{path}' escapes wiki_dir '{wiki_dir}'"
        ) from exc

    if target.exists():
        target.unlink()
