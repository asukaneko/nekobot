import json
import os
import sqlite3
from datetime import datetime
from typing import Any, Dict, Optional


def _db_path(data_dir: str) -> str:
    return os.path.join(data_dir, "sessions.db")


def _connect(data_dir: str) -> sqlite3.Connection:
    os.makedirs(data_dir, exist_ok=True)
    conn = sqlite3.connect(_db_path(data_dir))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            data_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    return conn


def load_sessions(data_dir: str) -> Dict[str, Dict[str, Any]]:
    sessions: Dict[str, Dict[str, Any]] = {}
    with _connect(data_dir) as conn:
        rows = conn.execute("SELECT session_id, data_json FROM sessions").fetchall()
        for session_id, data_json in rows:
            try:
                sessions[session_id] = json.loads(data_json)
            except Exception:
                continue
    return sessions


def get_session(data_dir: str, session_id: str) -> Optional[Dict[str, Any]]:
    with _connect(data_dir) as conn:
        row = conn.execute(
            "SELECT data_json FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if not row:
            return None
        try:
            return json.loads(row[0])
        except Exception:
            return None


def save_sessions(data_dir: str, sessions: Dict[str, Dict[str, Any]]) -> None:
    now = datetime.now().isoformat()
    with _connect(data_dir) as conn:
        conn.execute("DELETE FROM sessions")
        payload = [
            (session_id, json.dumps(session, ensure_ascii=False), now)
            for session_id, session in sessions.items()
        ]
        if payload:
            conn.executemany(
                "INSERT INTO sessions (session_id, data_json, updated_at) VALUES (?, ?, ?)",
                payload,
            )


def upsert_session(data_dir: str, session_id: str, session: Dict[str, Any]) -> None:
    now = datetime.now().isoformat()
    with _connect(data_dir) as conn:
        conn.execute(
            """
            INSERT INTO sessions (session_id, data_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                data_json = excluded.data_json,
                updated_at = excluded.updated_at
            """,
            (session_id, json.dumps(session, ensure_ascii=False), now),
        )
