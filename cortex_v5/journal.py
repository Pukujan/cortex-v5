"""Durable, expiring storage for the independent Cortex V5 runtime."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

_SENSITIVE = re.compile(r"authorization|cookie|api[_-]?key|token|secret|password|credential", re.I)


def _safe(value: Any, key: str | None = None) -> Any:
    if key and _SENSITIVE.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): _safe(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_safe(item) for item in value]
    if isinstance(value, str):
        return re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1[REDACTED]", value)
    return value


class Journal:
    """A small SQLite journal whose rows expire strictly after ``ttl_seconds``."""

    def __init__(self, data_dir: str | Path, ttl_seconds: float = 86_400) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.data_dir / "journal.sqlite3"
        self.ttl_seconds = float(ttl_seconds)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        with self._lock, self._connection:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY, payload TEXT NOT NULL,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL, expires_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
                    kind TEXT NOT NULL, payload TEXT NOT NULL, created_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS records_task_kind ON records(task_id, kind, id);
                CREATE INDEX IF NOT EXISTS records_expiry ON records(expires_at);
                CREATE TABLE IF NOT EXISTS model_state (
                    task_id TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                );
                """
            )
        self.purge_expired()

    @staticmethod
    def _dump(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)

    @staticmethod
    def _load(value: str) -> Any:
        return json.loads(value)

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> Journal:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def purge_expired(self, now: float | None = None) -> int:
        cutoff = time.time() if now is None else now
        with self._lock, self._connection:
            total = 0
            for table in ("tasks", "records", "model_state"):
                cursor = self._connection.execute(
                    f"DELETE FROM {table} WHERE expires_at <= ?", (cutoff,)
                )
                total += cursor.rowcount
            return total

    purge = purge_expired

    def put(self, task_id: str, task: Any) -> Any:
        now = time.time()
        encoded = self._dump(task)
        with self._lock, self._connection:
            self._connection.execute(
                """INSERT INTO tasks(task_id,payload,created_at,updated_at,expires_at)
                VALUES(?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET
                payload=excluded.payload, updated_at=excluded.updated_at""",
                (str(task_id), encoded, now, now, now + self.ttl_seconds),
            )
        return task

    def get(self, task_id: str, default: Any = None) -> Any:
        now = time.time()
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT payload FROM tasks WHERE task_id=? AND expires_at>?", (str(task_id), now)
            ).fetchone()
            if row is None:
                self._connection.execute("DELETE FROM tasks WHERE task_id=?", (str(task_id),))
                return default
            return self._load(row["payload"])

    def update(
        self, task_id: str, changes: dict[str, Any] | Callable[[Any], Any], **kwargs: Any
    ) -> Any:
        with self._lock:
            current = self.get(task_id)
            if current is None:
                raise KeyError(task_id)
            if callable(changes):
                updated = changes(current)
            else:
                if not isinstance(current, dict):
                    raise TypeError("mapping updates require a mapping task")
                updated = {**current, **changes, **kwargs}
            return self.put(task_id, updated)

    def _append(self, task_id: str, kind: str, payload: Any) -> int:
        now = time.time()
        with self._lock, self._connection:
            task = self._connection.execute(
                "SELECT expires_at FROM tasks WHERE task_id=? AND expires_at>?",
                (str(task_id), now),
            ).fetchone()
            expires_at = min(
                now + self.ttl_seconds,
                float(task["expires_at"]) if task is not None else now + self.ttl_seconds,
            )
            cursor = self._connection.execute(
                "INSERT INTO records(task_id,kind,payload,created_at,expires_at) VALUES(?,?,?,?,?)",
                (str(task_id), kind, self._dump(payload), now, expires_at),
            )
            return int(cursor.lastrowid)

    def append_event(self, task_id: str, event: Any) -> int:
        return self._append(task_id, "event", _safe(event))

    def append_receipt(self, task_id: str, receipt: Any) -> int:
        return self._append(task_id, "receipt", _safe(receipt))

    def append_outcome(self, task_id: str, outcome: Any) -> int:
        return self._append(task_id, "outcome", _safe(outcome))

    def _records(self, task_id: str, kind: str) -> list[Any]:
        now = time.time()
        with self._lock, self._connection:
            rows = self._connection.execute(
                "SELECT payload FROM records WHERE task_id=? AND kind=? "
                "AND expires_at>? ORDER BY id",
                (str(task_id), kind, now),
            ).fetchall()
            self._connection.execute("DELETE FROM records WHERE expires_at<=?", (now,))
            return [self._load(row["payload"]) for row in rows]

    def events(self, task_id: str) -> list[Any]:
        return self._records(task_id, "event")

    def receipts(self, task_id: str) -> list[Any]:
        return self._records(task_id, "receipt")

    def outcomes(self, task_id: str) -> list[Any]:
        return self._records(task_id, "outcome")

    get_events = events
    get_receipts = receipts
    get_outcomes = outcomes

    def set_model_state(self, task_id: str, state: Any) -> Any:
        now = time.time()
        with self._lock, self._connection:
            existing = self._connection.execute(
                "SELECT expires_at FROM model_state WHERE task_id=? AND expires_at>?",
                (str(task_id), now),
            ).fetchone()
            task = self._connection.execute(
                "SELECT expires_at FROM tasks WHERE task_id=? AND expires_at>?",
                (str(task_id), now),
            ).fetchone()
            expires_at = (
                float(existing["expires_at"])
                if existing is not None
                else min(
                    now + self.ttl_seconds,
                    float(task["expires_at"]) if task is not None else now + self.ttl_seconds,
                )
            )
            self._connection.execute(
                """INSERT INTO model_state(task_id,payload,updated_at,expires_at) VALUES(?,?,?,?)
                ON CONFLICT(task_id) DO UPDATE SET payload=excluded.payload,
                updated_at=excluded.updated_at, expires_at=excluded.expires_at""",
                (str(task_id), self._dump(state), now, expires_at),
            )
        return state

    def get_model_state(self, task_id: str, default: Any = None) -> Any:
        now = time.time()
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT payload FROM model_state WHERE task_id=? AND expires_at>?",
                (str(task_id), now),
            ).fetchone()
            return default if row is None else self._load(row["payload"])

    save_model_state = set_model_state
    record_model_outcome = append_outcome
    get_model_outcomes = outcomes
