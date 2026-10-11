"""SQLite store for proposals and decisions. This is what makes re-runs safe."""
import hashlib
import json
import sqlite3
from datetime import datetime, timezone

from config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS proposals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key   TEXT UNIQUE NOT NULL,
    run_id       TEXT NOT NULL,
    type         TEXT NOT NULL,
    subject      TEXT NOT NULL,
    title        TEXT NOT NULL,
    confidence   TEXT,
    actions      TEXT NOT NULL,
    evidence     TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',   -- pending | approved | rejected | failed
    reviewer_note TEXT,
    result       TEXT,
    created_at   TEXT NOT NULL,
    decided_at   TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    started_at  TEXT NOT NULL,
    summary     TEXT
);
"""


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def dedupe_key(p):
    """Same type + same record + same intended change => same key.

    Notes are excluded because they contain today's date; otherwise every daily
    run would look like a "new" proposal.
    """
    stripped = []
    for a in p["actions"]:
        fields = {k: v for k, v in a.get("fields", {}).items() if k != "note"}
        stripped.append({"op": a["op"], "account_id": a.get("account_id"), "fields": fields})
    digest = hashlib.sha256(json.dumps(stripped, sort_keys=True).encode()).hexdigest()[:16]
    return f"{p['type']}|{p['subject']}|{digest}"


def insert_if_new(conn, run_id, p):
    """Returns True if inserted, False if this exact proposal was seen before (any status)."""
    key = dedupe_key(p)
    cur = conn.execute(
        "INSERT OR IGNORE INTO proposals (dedupe_key, run_id, type, subject, title, confidence, actions, evidence, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (key, run_id, p["type"], p["subject"], p["title"], p.get("confidence"),
         json.dumps(p["actions"]), json.dumps(p["evidence"]), now()),
    )
    return cur.rowcount == 1


def has_open_proposal_for(conn, subject):
    """True if this record already has a pending proposal (avoid stacking two changes on one account)."""
    return conn.execute("SELECT 1 FROM proposals WHERE subject=? AND status='pending'", (subject,)).fetchone() is not None
