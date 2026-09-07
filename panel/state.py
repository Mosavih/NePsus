"""Panel-owned state. Small SQLite DB inside panel/ — never touches live paths."""
from __future__ import annotations

import sqlite3
from pathlib import Path

DB = Path(__file__).resolve().parent / "state.db"


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE IF NOT EXISTS action_log (
               id INTEGER PRIMARY KEY, at TEXT NOT NULL DEFAULT (datetime('now')),
               action TEXT NOT NULL, target TEXT, detail TEXT)""")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS cancel_flag (
               job TEXT PRIMARY KEY, requested_at TEXT NOT NULL DEFAULT (datetime('now')))""")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS route_status (
               tier TEXT PRIMARY KEY, model TEXT, ok INTEGER, latency_ms INTEGER,
               error TEXT, checked_at TEXT NOT NULL DEFAULT (datetime('now')))""")
    conn.commit()
    return conn


def log_action(action: str, target: str = "", detail: str = "") -> None:
    conn = connect()
    try:
        conn.execute("INSERT INTO action_log (action, target, detail) VALUES (?,?,?)",
                     (action, target, detail))
        conn.commit()
    finally:
        conn.close()


def record_decision(pid: int, decision: str, note: str = "") -> None:
    conn = connect()
    try:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS decision (
                   pid INTEGER PRIMARY KEY, decision TEXT NOT NULL, note TEXT,
                   at TEXT NOT NULL DEFAULT (datetime('now')))""")
        conn.execute(
            "INSERT OR REPLACE INTO decision (pid, decision, note) VALUES (?,?,?)",
            (pid, decision, note))
        conn.commit()
    finally:
        conn.close()


def get_decisions() -> dict:
    conn = connect()
    try:
        try:
            rows = conn.execute("SELECT pid, decision, note, at FROM decision")
        except Exception:
            return {}
        return {r["pid"]: dict(r) for r in rows}
    finally:
        conn.close()


def recent_actions(limit: int = 20) -> list[dict]:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT at, action, target, detail FROM action_log ORDER BY id DESC LIMIT ?",
            (limit,))
        return [dict(r) for r in rows]
    finally:
        conn.close()


def save_route_status(tier: str, model: str, res: dict) -> None:
    conn = connect()
    try:
        conn.execute(
            """INSERT OR REPLACE INTO route_status
               (tier, model, ok, latency_ms, error, checked_at)
               VALUES (?,?,?,?,?,datetime('now'))""",
            (tier, model, 1 if res.get("ok") else 0,
             res.get("latency_ms"), (res.get("error") or "")[:300]))
        conn.commit()
    finally:
        conn.close()


def get_route_statuses() -> dict:
    conn = connect()
    try:
        try:
            rows = conn.execute("SELECT * FROM route_status")
        except Exception:
            return {}
        return {r["tier"]: dict(r) for r in rows}
    finally:
        conn.close()
