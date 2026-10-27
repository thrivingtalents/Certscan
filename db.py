"""SQLite storage. One file, no server needed."""
import sqlite3
from datetime import datetime, timezone

from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    slug            TEXT NOT NULL UNIQUE,
    name            TEXT NOT NULL,
    event_date      TEXT,
    status          TEXT NOT NULL DEFAULT 'open',      -- open | closed
    access_code     TEXT,                              -- optional code shown only at the venue
    template_file   TEXT,                              -- stored upload (png/jpg/pdf)
    template_kind   TEXT,                              -- image | pdf
    page_w          REAL,                              -- certificate page size in PDF points
    page_h          REAL,
    preview_file    TEXT,                              -- PNG preview of the blank template
    name_x          REAL NOT NULL DEFAULT 0.5,         -- centre of name box, fraction of width
    name_y          REAL NOT NULL DEFAULT 0.5,         -- centre of name box, fraction of height
    name_w          REAL NOT NULL DEFAULT 0.6,         -- max width of name, fraction of width
    name_size       REAL NOT NULL DEFAULT 0.07,        -- font size, fraction of page height
    name_font       TEXT NOT NULL DEFAULT 'poppins-bold',
    name_color      TEXT NOT NULL DEFAULT '#1E2A4A',
    name_align      TEXT NOT NULL DEFAULT 'center',    -- left | center | right
    name_case       TEXT NOT NULL DEFAULT 'as_typed',  -- as_typed | upper | title
    show_cert_id    INTEGER NOT NULL DEFAULT 1,
    email_subject   TEXT,
    email_body      TEXT,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS participants (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id        INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    full_name       TEXT NOT NULL,
    email           TEXT NOT NULL,
    cert_code       TEXT NOT NULL UNIQUE,              -- public certificate ID, printed + verifiable
    token           TEXT NOT NULL,                     -- secret for the download link
    pdf_file        TEXT,
    ip              TEXT,
    created_at      TEXT NOT NULL,
    email_status    TEXT NOT NULL DEFAULT 'queued',    -- queued | sending | sent | failed
    email_attempts  INTEGER NOT NULL DEFAULT 0,
    email_error     TEXT,
    email_sent_at   TEXT,
    next_attempt_at TEXT,
    UNIQUE (event_id, email)
);

-- app-wide settings changed in the admin panel (email sender, mail server); override .env
CREATE TABLE IF NOT EXISTS settings (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_participants_event ON participants(event_id);
CREATE INDEX IF NOT EXISTS idx_participants_mail ON participants(email_status, next_attempt_at);
"""


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(path):
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def db_path(app=None):
    app = app or current_app
    return app.config["DATA_DIR"] / "certqr.db"


def get_db():
    if "db" not in g:
        g.db = connect(db_path())
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db(app):
    conn = connect(db_path(app))
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    app.teardown_appcontext(close_db)
