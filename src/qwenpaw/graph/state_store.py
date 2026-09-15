# -*- coding: utf-8 -*-
"""SQLite checkpoint store for graph runs (EP-2-17).

Two tables:
    graph_runs      — one row per execution (status, merged state)
    graph_node_runs — per-node checkpoints (outputs + chosen route)

Resume semantics: a restarted executor reloads completed nodes into the
graph state and skips them, so an interrupted run continues from the
last finished node (the DoD for EP-2-17).
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_SCHEMA = """
CREATE TABLE IF NOT EXISTS graph_runs (
    run_id      TEXT PRIMARY KEY,
    graph_id    TEXT NOT NULL,
    status      TEXT NOT NULL,
    state_json  TEXT NOT NULL DEFAULT '{}',
    error       TEXT,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS graph_node_runs (
    run_id      TEXT NOT NULL,
    node_id     TEXT NOT NULL,
    status      TEXT NOT NULL,
    outputs_json TEXT NOT NULL DEFAULT '{}',
    route       TEXT,
    started_at  REAL,
    finished_at REAL,
    PRIMARY KEY (run_id, node_id)
);
"""


class GraphStateStore:
    """Run + node checkpoints on one SQLite database (WAL)."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = Path(database_path)
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._database_path,
            timeout=30,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    # ------------------------------------------------------------- runs

    def create_run(self, run_id: str, graph_id: str) -> None:
        now = time.time()
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO graph_runs "
                "(run_id, graph_id, status, created_at, updated_at) "
                "VALUES (?, ?, 'running', ?, ?)",
                (run_id, graph_id, now, now),
            )

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM graph_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "run_id": row["run_id"],
            "graph_id": row["graph_id"],
            "status": row["status"],
            "state": json.loads(row["state_json"] or "{}"),
            "error": row["error"],
        }

    def set_run_status(
        self,
        run_id: str,
        status: str,
        *,
        state: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
    ) -> None:
        with self._connect() as connection:
            if state is not None:
                connection.execute(
                    "UPDATE graph_runs SET status = ?, state_json = ?, "
                    "error = ?, updated_at = ? WHERE run_id = ?",
                    (
                        status,
                        json.dumps(state, ensure_ascii=False),
                        error,
                        time.time(),
                        run_id,
                    ),
                )
            else:
                connection.execute(
                    "UPDATE graph_runs SET status = ?, error = ?, "
                    "updated_at = ? WHERE run_id = ?",
                    (status, error, time.time(), run_id),
                )

    # ------------------------------------------------------------ nodes

    def mark_node_started(self, run_id: str, node_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO graph_node_runs "
                "(run_id, node_id, status, started_at) "
                "VALUES (?, ?, 'running', ?)",
                (run_id, node_id, time.time()),
            )

    def mark_node_completed(
        self,
        run_id: str,
        node_id: str,
        outputs: Dict[str, Any],
        route: str = "default",
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO graph_node_runs "
                "(run_id, node_id, status, outputs_json, route, "
                "started_at, finished_at) VALUES (?, ?, 'completed', ?, ?, "
                "?, ?)",
                (
                    run_id,
                    node_id,
                    json.dumps(outputs, ensure_ascii=False),
                    route,
                    time.time(),
                    time.time(),
                ),
            )

    def mark_node_suspended(self, run_id: str, node_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO graph_node_runs "
                "(run_id, node_id, status, started_at) "
                "VALUES (?, ?, 'suspended', ?)",
                (run_id, node_id, time.time()),
            )

    def completed_nodes(
        self,
        run_id: str,
    ) -> Dict[str, Tuple[Dict[str, Any], str]]:
        """node_id -> (outputs, route) for checkpointed completions."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT node_id, outputs_json, route FROM graph_node_runs "
                "WHERE run_id = ? AND status = 'completed'",
                (run_id,),
            ).fetchall()
        return {
            row["node_id"]: (
                json.loads(row["outputs_json"] or "{}"),
                row["route"] or "default",
            )
            for row in rows
        }

    def list_node_runs(self, run_id: str) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM graph_node_runs WHERE run_id = ? "
                "ORDER BY started_at",
                (run_id,),
            ).fetchall()
        return [
            {
                "node_id": row["node_id"],
                "status": row["status"],
                "outputs": json.loads(row["outputs_json"] or "{}"),
                "route": row["route"],
            }
            for row in rows
        ]


__all__ = ["GraphStateStore"]
