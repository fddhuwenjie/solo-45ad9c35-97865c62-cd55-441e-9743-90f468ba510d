"""SQLite persistence for scores, analyses, revisions and confirmations."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS score (
    id           INTEGER PRIMARY KEY,
    title        TEXT NOT NULL,
    musicxml     TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    digest       TEXT NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS analysis (
    id         INTEGER PRIMARY KEY,
    score_id   INTEGER NOT NULL REFERENCES score(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    report_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS revision (
    id          INTEGER PRIMARY KEY,
    score_id    INTEGER NOT NULL REFERENCES score(id),
    analysis_id INTEGER REFERENCES analysis(id),
    note        TEXT NOT NULL DEFAULT '',
    pagination_json TEXT NOT NULL,         -- {partId: [measure numbers ending each page]}
    report_json TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS confirmation (
    id          INTEGER PRIMARY KEY,
    score_id    INTEGER NOT NULL REFERENCES score(id),
    revision_id INTEGER NOT NULL REFERENCES revision(id),
    digest      TEXT NOT NULL,
    route_json  TEXT NOT NULL,
    pagination_json TEXT NOT NULL,
    report_json TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def connect(path: str = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def tx(conn: sqlite3.Connection):
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# --- scores -------------------------------------------------------------------

def create_score(conn: sqlite3.Connection, title: str, musicxml: str,
                 metadata: dict, digest: str) -> int:
    with tx(conn):
        cur = conn.execute(
            "INSERT INTO score(title, musicxml, metadata_json, digest) VALUES (?,?,?,?)",
            (title, musicxml, json.dumps(metadata, ensure_ascii=False), digest),
        )
        return int(cur.lastrowid)


def get_score(conn: sqlite3.Connection, score_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM score WHERE id=?", (score_id,)).fetchone()


def list_scores(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT id,title,digest,created_at FROM score ORDER BY id"))


def latest_analysis(conn: sqlite3.Connection, score_id: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM analysis WHERE score_id=? ORDER BY id DESC LIMIT 1", (score_id,)
    ).fetchone()


def create_analysis(conn: sqlite3.Connection, score_id: int, report: dict) -> int:
    with tx(conn):
        cur = conn.execute(
            "INSERT INTO analysis(score_id, report_json) VALUES (?,?)",
            (score_id, json.dumps(report, ensure_ascii=False)),
        )
        return int(cur.lastrowid)


def get_analysis(conn: sqlite3.Connection, analysis_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM analysis WHERE id=?", (analysis_id,)).fetchone()


# --- revisions ----------------------------------------------------------------

def create_revision(conn: sqlite3.Connection, score_id: int,
                    pagination: dict, note: str,
                    analysis_id: Optional[int], report: Optional[dict]) -> int:
    with tx(conn):
        cur = conn.execute(
            "INSERT INTO revision(score_id, analysis_id, note, pagination_json, report_json)"
            " VALUES (?,?,?,?,?)",
            (score_id, analysis_id, note,
             json.dumps(pagination, ensure_ascii=False),
             json.dumps(report, ensure_ascii=False) if report is not None else None),
        )
        return int(cur.lastrowid)


def list_revisions(conn: sqlite3.Connection, score_id: int) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT id,score_id,analysis_id,note,created_at FROM revision "
        "WHERE score_id=? ORDER BY id", (score_id,)))


def get_revision(conn: sqlite3.Connection, revision_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM revision WHERE id=?", (revision_id,)).fetchone()


def latest_revision(conn: sqlite3.Connection, score_id: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM revision WHERE score_id=? ORDER BY id DESC LIMIT 1", (score_id,)
    ).fetchone()


# --- confirmations ------------------------------------------------------------

def create_confirmation(conn: sqlite3.Connection, score_id: int, revision_id: int,
                        digest: str, route: dict, pagination: dict, report: dict) -> int:
    with tx(conn):
        cur = conn.execute(
            "INSERT INTO confirmation(score_id, revision_id, digest, route_json,"
            " pagination_json, report_json) VALUES (?,?,?,?,?,?)",
            (score_id, revision_id, digest,
             json.dumps(route, ensure_ascii=False),
             json.dumps(pagination, ensure_ascii=False),
             json.dumps(report, ensure_ascii=False)),
        )
        return int(cur.lastrowid)


def get_confirmation(conn: sqlite3.Connection, confirmation_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM confirmation WHERE id=?",
                        (confirmation_id,)).fetchone()


def confirmation_for_revision(conn: sqlite3.Connection, revision_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM confirmation WHERE revision_id=? ORDER BY id DESC LIMIT 1",
                        (revision_id,)).fetchone()
