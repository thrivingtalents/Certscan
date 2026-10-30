"""SQLite storage. One file, no server needed."""
import sqlite3
from datetime import datetime, timezone

from flask import current_app, g

PARTICIPANTS_COLUMNS = ("id, event_id, full_name, email, cert_code, token, pdf_file, ip, created_at, "
                        "email_status, email_attempts, email_error, email_sent_at, next_attempt_at")

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
    email_subject   TEXT,
    email_body      TEXT,
    done_heading    TEXT,                              -- thank-you page shown after registering;
    done_title      TEXT,                              --   empty fields fall back to the defaults
    done_message    TEXT,
    done_button_label TEXT,                            -- optional extra button, e.g. a feedback form
    done_button_url TEXT,
    done_show_download INTEGER NOT NULL DEFAULT 1,
    done_show_view  INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS participants (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id        INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    full_name       TEXT NOT NULL,
    email           TEXT,                              -- empty for names imported without an email
    cert_code       TEXT NOT NULL UNIQUE,              -- internal key used in the private download link
    token           TEXT NOT NULL,                     -- secret for the download link
    pdf_file        TEXT,                              -- empty until the certificate has been made
    pdf_claimed_at  TEXT,                              -- when a worker started making the PDF
    ip              TEXT,
    created_at      TEXT NOT NULL,
    email_status    TEXT NOT NULL DEFAULT 'queued',    -- queued | sending | sent | failed | none
    email_attempts  INTEGER NOT NULL DEFAULT 0,
    email_error     TEXT,
    email_sent_at   TEXT,
    next_attempt_at TEXT                               -- one email may register any number of times
);

-- fonts uploaded in the admin panel; the file is <key>.ttf in DATA_DIR/fonts
CREATE TABLE IF NOT EXISTS fonts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    key             TEXT NOT NULL UNIQUE,
    name            TEXT NOT NULL,
    created_at      TEXT NOT NULL
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


# columns added to events after the first release
EVENT_COLUMNS = {
    "done_heading": "TEXT", "done_title": "TEXT", "done_message": "TEXT", "done_button_label": "TEXT",
    "done_button_url": "TEXT", "done_show_download": "INTEGER NOT NULL DEFAULT 1",
    "done_show_view": "INTEGER NOT NULL DEFAULT 1",
}


def migrate(conn):
    """Bring a database made by an older version up to date."""
    have = {r["name"] for r in conn.execute("PRAGMA table_info(events)")}
    for name, kind in EVENT_COLUMNS.items():
        if name not in have:
            conn.execute(f"ALTER TABLE events ADD COLUMN {name} {kind}")
    cols = {r["name"]: r for r in conn.execute("PRAGMA table_info(participants)")}
    table_sql = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='participants'").fetchone()[0]
    if cols["email"]["notnull"] or "UNIQUE (event_id, email)" in table_sql:
        # email used to be required and once per session; SQLite can only drop those by rebuilding the table
        create = SCHEMA[SCHEMA.index("CREATE TABLE IF NOT EXISTS participants"):]
        create = create[:create.index(");") + 2]
        conn.executescript(
            "PRAGMA foreign_keys=OFF; BEGIN;"
            "ALTER TABLE participants RENAME TO participants_old;"
            f"{create}"
            f"INSERT INTO participants ({PARTICIPANTS_COLUMNS}) SELECT {PARTICIPANTS_COLUMNS} FROM participants_old;"
            "DROP TABLE participants_old;"
            "COMMIT; PRAGMA foreign_keys=ON;")
        conn.executescript(SCHEMA)  # recreate the indexes that went with the old table
    elif "pdf_claimed_at" not in cols:
        conn.execute("ALTER TABLE participants ADD COLUMN pdf_claimed_at TEXT")


def init_db(app):
    conn = connect(db_path(app))
    conn.executescript(SCHEMA)
    migrate(conn)
    conn.commit()
    conn.close()
    app.teardown_appcontext(close_db)
