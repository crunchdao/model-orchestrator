"""
Phala TEE Metrics Tracker.

Records build and startup timing from spawntee task responses
into a SQLite database for historical analysis and optimization.
"""

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from ...utils.logging_utils import get_logger

logger = get_logger()

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS phala_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    submission_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    duration_seconds REAL,
    recorded_at TEXT NOT NULL
)
"""

_INSERT = """
INSERT INTO phala_metrics (task_id, submission_id, operation, status, started_at, completed_at, duration_seconds, recorded_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
"""

_SELECT_ALL = """
SELECT task_id, submission_id, operation, status, started_at, completed_at, duration_seconds, recorded_at
FROM phala_metrics
ORDER BY recorded_at DESC
"""

_SELECT_BY_OPERATION = """
SELECT task_id, submission_id, operation, status, started_at, completed_at, duration_seconds, recorded_at
FROM phala_metrics
WHERE operation = ?
ORDER BY recorded_at DESC
"""

_EXISTS = """
SELECT 1 FROM phala_metrics WHERE task_id = ? AND operation = ? AND status = ? LIMIT 1
"""


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        # Handle both Z suffix and +00:00
        ts = ts.replace("Z", "+00:00")
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


def _duration(started: str | None, completed: str | None) -> float | None:
    s = _parse_iso(started)
    c = _parse_iso(completed)
    if s and c:
        return (c - s).total_seconds()
    return None


class PhalaMetrics:
    """
    Records spawntee operation timings to SQLite for historical analysis.

    Thread-safe — uses a lock around all DB writes.
    """

    def __init__(self, db_path: str = "model_orchestrator/data/phala_metrics.db"):
        self._db_path = db_path
        self._lock = threading.Lock()

        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(_CREATE_TABLE)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path)

    def record_from_task(self, task: dict) -> None:
        """
        Extract and record timing for all completed/failed operations in a task.

        Only records each (task_id, operation, status) once — safe to call
        repeatedly during polling.

        Args:
            task: Full task response from spawntee GET /task/{id}
        """
        task_id = task.get("task_id", "")
        submission_id = task.get("submission_id", "")
        operations = task.get("operations", [])

        with self._lock:
            conn = self._connect()
            try:
                for op in operations:
                    status = op.get("status", "")
                    if status not in ("completed", "failed"):
                        continue

                    operation = op.get("operation_type", "")
                    started_at = op.get("started_at")
                    completed_at = op.get("completed_at")

                    # Skip if already recorded
                    row = conn.execute(_EXISTS, (task_id, operation, status)).fetchone()
                    if row:
                        continue

                    duration = _duration(started_at, completed_at)
                    now = datetime.now(timezone.utc).isoformat()

                    conn.execute(_INSERT, (
                        task_id, submission_id, operation, status,
                        started_at, completed_at, duration, now
                    ))

                    if duration is not None:
                        logger.info(
                            "📊 METRIC %s %s: %.1fs (submission=%s, task=%s)",
                            operation, status, duration, submission_id, task_id[:12]
                        )
                    else:
                        logger.info(
                            "📊 METRIC %s %s: no timing (submission=%s, task=%s)",
                            operation, status, submission_id, task_id[:12]
                        )

                conn.commit()
            finally:
                conn.close()

    def get_all(self) -> list[dict]:
        """Return all metrics rows as dicts."""
        conn = self._connect()
        conn.row_factory = sqlite3.Row
        rows = conn.execute(_SELECT_ALL).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def get_by_operation(self, operation: str) -> list[dict]:
        """Return metrics for a specific operation type."""
        conn = self._connect()
        conn.row_factory = sqlite3.Row
        rows = conn.execute(_SELECT_BY_OPERATION, (operation,)).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def summary(self) -> str:
        """
        Generate a human-readable summary of build and start timings.
        """
        lines = []
        lines.append("")
        lines.append("=" * 70)
        lines.append("  Phala TEE Metrics Summary")
        lines.append("=" * 70)

        for op in ("build_model", "start_model", "stop_model"):
            rows = self.get_by_operation(op)
            completed = [r for r in rows if r["status"] == "completed" and r["duration_seconds"] is not None]
            failed = [r for r in rows if r["status"] == "failed"]

            lines.append(f"\n  {op}")
            lines.append(f"  {'─' * 40}")
            lines.append(f"  Total:     {len(rows)}")
            lines.append(f"  Completed: {len(completed)}")
            lines.append(f"  Failed:    {len(failed)}")

            if completed:
                durations = sorted(r["duration_seconds"] for r in completed)
                avg = sum(durations) / len(durations)
                p50 = durations[len(durations) // 2]
                p95_idx = min(int(len(durations) * 0.95), len(durations) - 1)
                p95 = durations[p95_idx]

                lines.append(f"  Min:       {durations[0]:.1f}s")
                lines.append(f"  Max:       {durations[-1]:.1f}s")
                lines.append(f"  Avg:       {avg:.1f}s")
                lines.append(f"  p50:       {p50:.1f}s")
                lines.append(f"  p95:       {p95:.1f}s")

                # Show last 5
                lines.append(f"  Last 5:")
                for r in completed[:5]:
                    lines.append(
                        f"    {r['submission_id']}: {r['duration_seconds']:.1f}s "
                        f"({r['started_at'][:19]})"
                    )

        lines.append("")
        lines.append("=" * 70)
        return "\n".join(lines)
