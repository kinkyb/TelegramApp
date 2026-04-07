"""db.py — Database helpers supporting both SQLite (local) and PostgreSQL (Render).

Backend is chosen automatically from DATABASE_URL:
  sqlite:///bot.db        → SQLite  (local dev)
  postgresql://...        → PostgreSQL (Render / production)

All public functions have an identical interface regardless of backend.
"""

import os
import sqlite3
from datetime import datetime
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///bot.db")

# Render sets DATABASE_URL starting with "postgres://" (legacy) — normalise it
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

_USE_POSTGRES = DATABASE_URL.startswith("postgresql://")


# ---------------------------------------------------------------------------
# Connection factory
# ---------------------------------------------------------------------------

class _PGConn:
    """Wrapper around pg8000 that makes rows behave like dicts."""

    def __init__(self):
        import pg8000.dbapi
        import urllib.parse as _up
        r = _up.urlparse(DATABASE_URL)
        self._conn = pg8000.dbapi.connect(
            user=r.username,
            password=r.password,
            host=r.hostname,
            port=r.port or 5432,
            database=r.path.lstrip("/"),
            ssl_context=True,
        )
        self._cur = self._conn.cursor()

    def execute(self, sql: str, params: tuple = ()):
        """Run a query and return self for chaining."""
        self._cur.execute(sql, params)
        return self

    def fetchone(self):
        """Return one row as a dict, or None."""
        row = self._cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in self._cur.description]
        return dict(zip(cols, row))

    def fetchall(self):
        """Return all rows as a list of dicts."""
        rows = self._cur.fetchall()
        if not rows:
            return []
        cols = [d[0] for d in self._cur.description]
        return [dict(zip(cols, r)) for r in rows]

    def commit(self):
        self._conn.commit()

    def close(self):
        self._cur.close()
        self._conn.close()


def get_connection():
    """Return an open database connection for the configured backend.

    SQLite connections have row_factory set so rows behave like dicts.
    PostgreSQL connections use pg8000 (pure Python) with dict-like rows.
    """
    if _USE_POSTGRES:
        return _PGConn()
    else:
        path = DATABASE_URL[len("sqlite:///"):]
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn


def _ph() -> str:
    """Return the correct SQL placeholder for the active backend (? or %s)."""
    return "%s" if _USE_POSTGRES else "?"


def _execute(conn, sql: str, params: tuple = ()):
    """Execute a single statement and return the cursor."""
    if _USE_POSTGRES:
        cur = conn.cursor()
        cur.execute(sql, params)
        return cur
    else:
        return conn.execute(sql, params)


def _fetchone(conn, sql: str, params: tuple = ()):
    """Execute a query and return one row (dict-like) or None."""
    cur = _execute(conn, sql, params)
    return cur.fetchone()


def _fetchall(conn, sql: str, params: tuple = ()):
    """Execute a query and return all rows as a list of dict-like rows."""
    cur = _execute(conn, sql, params)
    return cur.fetchall()


# ---------------------------------------------------------------------------
# Schema initialisation
# ---------------------------------------------------------------------------

_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS content (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  file_url        TEXT    NOT NULL,
  teaser_url      TEXT,
  file_type       TEXT,
  caption         TEXT,
  is_ppv          BOOLEAN DEFAULT 0,
  ppv_price_stars INTEGER,
  posted          BOOLEAN DEFAULT 0,
  posted_at       DATETIME,
  uploaded_at     DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS creators (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  name          TEXT    NOT NULL,
  onlyfans_url  TEXT    NOT NULL,
  gif_url       TEXT,
  bio           TEXT,
  active        BOOLEAN DEFAULT 1
);

CREATE TABLE IF NOT EXISTS purchases (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  telegram_user_id INTEGER NOT NULL,
  content_id       INTEGER NOT NULL,
  stars_paid       INTEGER NOT NULL,
  purchased_at     DATETIME DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (content_id) REFERENCES content(id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_purchases_user_content
  ON purchases (telegram_user_id, content_id);

CREATE TABLE IF NOT EXISTS promo_posts (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  posted_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
"""

_PG_SCHEMA_STATEMENTS = [
    """CREATE TABLE IF NOT EXISTS content (
        id              SERIAL PRIMARY KEY,
        file_url        TEXT    NOT NULL,
        teaser_url      TEXT,
        file_type       TEXT,
        caption         TEXT,
        is_ppv          BOOLEAN DEFAULT FALSE,
        ppv_price_stars INTEGER,
        posted          BOOLEAN DEFAULT FALSE,
        posted_at       TIMESTAMP,
        uploaded_at     TIMESTAMP DEFAULT NOW()
    )""",
    """CREATE TABLE IF NOT EXISTS creators (
        id            SERIAL PRIMARY KEY,
        name          TEXT    NOT NULL,
        onlyfans_url  TEXT    NOT NULL,
        gif_url       TEXT,
        bio           TEXT,
        active        BOOLEAN DEFAULT TRUE
    )""",
    """CREATE TABLE IF NOT EXISTS purchases (
        id               SERIAL PRIMARY KEY,
        telegram_user_id BIGINT  NOT NULL,
        content_id       INTEGER NOT NULL REFERENCES content(id),
        stars_paid       INTEGER NOT NULL,
        purchased_at     TIMESTAMP DEFAULT NOW(),
        UNIQUE (telegram_user_id, content_id)
    )""",
    """CREATE TABLE IF NOT EXISTS promo_posts (
        id        SERIAL PRIMARY KEY,
        posted_at TIMESTAMP DEFAULT NOW()
    )""",
]


def init_db() -> None:
    """Create all tables (idempotent). Runs the correct DDL for the active backend."""
    conn = get_connection()
    try:
        if _USE_POSTGRES:
            for stmt in _PG_SCHEMA_STATEMENTS:
                _execute(conn, stmt)
            conn.commit()
        else:
            conn.executescript(_SQLITE_SCHEMA)
            conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Content helpers
# ---------------------------------------------------------------------------

def insert_content(file_url: str, file_type: str, caption: str = "", teaser_url: str = "") -> int:
    """Insert a new content row and return its id."""
    ph = _ph()
    conn = get_connection()
    try:
        if _USE_POSTGRES:
            cur = _execute(
                conn,
                f"INSERT INTO content (file_url, teaser_url, file_type, caption) VALUES ({ph},{ph},{ph},{ph}) RETURNING id",
                (file_url, teaser_url or None, file_type, caption),
            )
            row_id = cur.fetchone()["id"]
        else:
            cur = _execute(
                conn,
                f"INSERT INTO content (file_url, teaser_url, file_type, caption) VALUES ({ph},{ph},{ph},{ph})",
                (file_url, teaser_url or None, file_type, caption),
            )
            row_id = cur.lastrowid
        conn.commit()
        return row_id
    finally:
        conn.close()


def get_content(content_id: int) -> Optional[dict]:
    """Fetch a single content row by id, or None if not found."""
    conn = get_connection()
    try:
        return _fetchone(conn, f"SELECT * FROM content WHERE id = {_ph()}", (content_id,))
    finally:
        conn.close()


def get_unposted_content(limit: int = 10) -> list:
    """Return up to `limit` content rows that have not been posted yet."""
    conn = get_connection()
    try:
        return _fetchall(
            conn,
            f"SELECT * FROM content WHERE posted = FALSE ORDER BY uploaded_at ASC LIMIT {_ph()}",
            (limit,),
        )
    finally:
        conn.close()


def get_feed_content(page: int = 1, per_page: int = 20) -> list:
    """Return paginated free (non-PPV) content for the mini-app feed."""
    offset = (page - 1) * per_page
    ph = _ph()
    conn = get_connection()
    try:
        return _fetchall(
            conn,
            f"SELECT * FROM content WHERE is_ppv = FALSE ORDER BY uploaded_at DESC LIMIT {ph} OFFSET {ph}",
            (per_page, offset),
        )
    finally:
        conn.close()


def mark_posted(content_id: int) -> None:
    """Mark a content row as posted with the current timestamp."""
    ph = _ph()
    conn = get_connection()
    try:
        _execute(
            conn,
            f"UPDATE content SET posted = TRUE, posted_at = {ph} WHERE id = {ph}",
            (datetime.utcnow().isoformat(), content_id),
        )
        conn.commit()
    finally:
        conn.close()


def set_ppv(content_id: int, price_stars: int, teaser_url: str = "") -> None:
    """Flag a content row as PPV, set its price, and optionally set a teaser URL."""
    ph = _ph()
    conn = get_connection()
    try:
        _execute(
            conn,
            f"UPDATE content SET is_ppv = TRUE, ppv_price_stars = {ph}, teaser_url = {ph} WHERE id = {ph}",
            (price_stars, teaser_url or None, content_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_last_posted_at() -> Optional[str]:
    """Return the most recent posted_at across content and promo posts, or None."""
    conn = get_connection()
    try:
        content_row = _fetchone(
            conn, "SELECT posted_at FROM content WHERE posted = TRUE ORDER BY posted_at DESC LIMIT 1"
        )
        promo_row = _fetchone(
            conn, "SELECT posted_at FROM promo_posts ORDER BY posted_at DESC LIMIT 1"
        )
        timestamps = []
        if content_row and content_row["posted_at"]:
            timestamps.append(str(content_row["posted_at"]))
        if promo_row and promo_row["posted_at"]:
            timestamps.append(str(promo_row["posted_at"]))
        return max(timestamps) if timestamps else None
    finally:
        conn.close()


def record_promo_post() -> None:
    """Log that a promo post was sent, so the rate limiter can track it."""
    conn = get_connection()
    try:
        _execute(conn, f"INSERT INTO promo_posts (posted_at) VALUES ({_ph()})", (datetime.utcnow().isoformat(),))
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Creator helpers
# ---------------------------------------------------------------------------

def get_creator(creator_id: int) -> Optional[dict]:
    """Fetch a single creator row by id, or None if not found."""
    conn = get_connection()
    try:
        return _fetchone(conn, f"SELECT * FROM creators WHERE id = {_ph()}", (creator_id,))
    finally:
        conn.close()


def get_active_creators() -> list:
    """Return all active promoted creators."""
    conn = get_connection()
    try:
        return _fetchall(conn, "SELECT * FROM creators WHERE active = TRUE ORDER BY name ASC")
    finally:
        conn.close()


def insert_creator(name: str, onlyfans_url: str, gif_url: str = "", bio: str = "") -> int:
    """Insert a new creator and return its id."""
    ph = _ph()
    conn = get_connection()
    try:
        if _USE_POSTGRES:
            cur = _execute(
                conn,
                f"INSERT INTO creators (name, onlyfans_url, gif_url, bio) VALUES ({ph},{ph},{ph},{ph}) RETURNING id",
                (name, onlyfans_url, gif_url or None, bio or None),
            )
            row_id = cur.fetchone()["id"]
        else:
            cur = _execute(
                conn,
                f"INSERT INTO creators (name, onlyfans_url, gif_url, bio) VALUES ({ph},{ph},{ph},{ph})",
                (name, onlyfans_url, gif_url or None, bio or None),
            )
            row_id = cur.lastrowid
        conn.commit()
        return row_id
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Purchase helpers
# ---------------------------------------------------------------------------

def record_purchase(telegram_user_id: int, content_id: int, stars_paid: int) -> None:
    """Record a completed Stars purchase, ignoring duplicates."""
    ph = _ph()
    conn = get_connection()
    try:
        if _USE_POSTGRES:
            _execute(
                conn,
                f"INSERT INTO purchases (telegram_user_id, content_id, stars_paid) VALUES ({ph},{ph},{ph}) ON CONFLICT DO NOTHING",
                (telegram_user_id, content_id, stars_paid),
            )
        else:
            _execute(
                conn,
                f"INSERT OR IGNORE INTO purchases (telegram_user_id, content_id, stars_paid) VALUES ({ph},{ph},{ph})",
                (telegram_user_id, content_id, stars_paid),
            )
        conn.commit()
    finally:
        conn.close()


def has_purchased(telegram_user_id: int, content_id: int) -> bool:
    """Return True if the user has already purchased the given content."""
    ph = _ph()
    conn = get_connection()
    try:
        row = _fetchone(
            conn,
            f"SELECT 1 FROM purchases WHERE telegram_user_id = {ph} AND content_id = {ph}",
            (telegram_user_id, content_id),
        )
        return row is not None
    finally:
        conn.close()
