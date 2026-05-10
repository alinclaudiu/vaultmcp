"""Durable on-disk queue for offline writes.

When master is unreachable, the agent stages writes as JSON files under
``<state_dir>/queue/`` and replays them in timestamp order on reconnect.
The queue survives agent crashes (files are fsync'd) and exits cleanly
on conflict (the failed file is moved to ``queue/failed/`` for human
review; the rest of the queue continues).
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass
class QueuedWrite:
    """One pending write, persisted as a JSON file."""

    id: str
    timestamp: str  # ISO-8601
    path: str
    content: str
    base_version: int | None
    session: dict[str, Any]

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, text: str) -> QueuedWrite:
        data = json.loads(text)
        return cls(**data)


class WriteQueue:
    """File-backed FIFO queue of pending writes.

    Files are named ``<ISO timestamp>-<uuid4>.json`` so an alphabetical
    ``sorted()`` listing yields chronological order.
    """

    def __init__(self, queue_dir: Path) -> None:
        self.queue_dir = queue_dir
        self.queue_dir.mkdir(parents=True, exist_ok=True)
        self.failed_dir = queue_dir / "failed"
        self.failed_dir.mkdir(parents=True, exist_ok=True)

    def enqueue(
        self,
        *,
        path: str,
        content: str,
        base_version: int | None,
        session: dict[str, Any],
    ) -> QueuedWrite:
        ts = datetime.now(tz=UTC).strftime("%Y-%m-%dT%H-%M-%S-%fZ")
        item_id = f"{ts}-{uuid.uuid4().hex[:8]}"
        item = QueuedWrite(
            id=item_id,
            timestamp=datetime.now(tz=UTC).isoformat(),
            path=path,
            content=content,
            base_version=base_version,
            session=session,
        )
        self._atomic_write(self._file_for(item_id), item.to_json())
        return item

    def list_pending(self) -> list[QueuedWrite]:
        items: list[QueuedWrite] = []
        for fp in sorted(self.queue_dir.glob("*.json")):
            try:
                items.append(QueuedWrite.from_json(fp.read_text(encoding="utf-8")))
            except (OSError, ValueError, json.JSONDecodeError):
                # Don't crash on a corrupt entry; sideline it.
                self._sideline(fp, reason="corrupt")
        return items

    def remove(self, item: QueuedWrite) -> None:
        target = self._file_for(item.id)
        if target.exists():
            target.unlink()

    def mark_failed(self, item: QueuedWrite, reason: str) -> None:
        src = self._file_for(item.id)
        if src.exists():
            self._sideline(src, reason=reason)

    def depth(self) -> int:
        return len(list(self.queue_dir.glob("*.json")))

    # ----- internals -----

    def _file_for(self, item_id: str) -> Path:
        return self.queue_dir / f"{item_id}.json"

    def _atomic_write(self, target: Path, text: str) -> None:
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, target)

    def _sideline(self, fp: Path, *, reason: str) -> None:
        ts = datetime.now(tz=UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
        dst = self.failed_dir / f"{ts}-{reason}-{fp.name}"
        try:
            fp.rename(dst)
        except OSError:
            pass
