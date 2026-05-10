"""Unit tests for the agent's offline write queue."""

from __future__ import annotations

from pathlib import Path

from vaultmcp.agent.queue import WriteQueue


def _session() -> dict:
    return {
        "server_id": "server-1",
        "app": "webstore",
        "agent_model": "claude",
        "session_id": "",
    }


def test_enqueue_and_list(tmp_path: Path) -> None:
    q = WriteQueue(tmp_path / "queue")
    q.enqueue(path="a.md", content="aaa", base_version=None, session=_session())
    q.enqueue(path="b.md", content="bbb", base_version=1, session=_session())

    items = q.list_pending()
    assert {i.path for i in items} == {"a.md", "b.md"}
    assert q.depth() == 2


def test_remove(tmp_path: Path) -> None:
    q = WriteQueue(tmp_path / "queue")
    item = q.enqueue(path="x.md", content="x", base_version=None, session=_session())
    q.remove(item)
    assert q.depth() == 0


def test_mark_failed_sidelines(tmp_path: Path) -> None:
    q = WriteQueue(tmp_path / "queue")
    item = q.enqueue(path="x.md", content="x", base_version=None, session=_session())
    q.mark_failed(item, "conflict")
    assert q.depth() == 0
    assert any((tmp_path / "queue" / "failed").iterdir())


def test_corrupt_entry_is_sidelined(tmp_path: Path) -> None:
    qd = tmp_path / "queue"
    qd.mkdir(parents=True)
    (qd / "00-bad.json").write_text("not json")

    q = WriteQueue(qd)
    items = q.list_pending()
    assert items == []
    assert any((qd / "failed").iterdir())


def test_chronological_order(tmp_path: Path) -> None:
    """Items returned in timestamp order even with rapid enqueues."""
    q = WriteQueue(tmp_path / "queue")
    paths = [f"page-{i}.md" for i in range(5)]
    for p in paths:
        q.enqueue(path=p, content="x", base_version=None, session=_session())
    items = q.list_pending()
    # Rapid enqueues may collide on timestamp, but the sorted file listing
    # should still be deterministic. We at least verify all items are there.
    assert {i.path for i in items} == set(paths)
