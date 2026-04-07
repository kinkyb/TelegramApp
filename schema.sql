-- schema.sql — Database schema for Telegram Content Bot
-- Enable WAL mode for concurrent reads/writes (set in db.py at connection time)

-- Own content (photos, videos, GIFs)
CREATE TABLE IF NOT EXISTS content (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  file_url        TEXT    NOT NULL,              -- full/unlocked media URL
  teaser_url      TEXT,                          -- free preview posted to channel (PPV only)
  file_type       TEXT,                          -- image, video, gif
  caption         TEXT,
  is_ppv          BOOLEAN DEFAULT 0,
  ppv_price_stars INTEGER,
  posted          BOOLEAN DEFAULT 0,
  posted_at       DATETIME,
  uploaded_at     DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- Promoted creators
CREATE TABLE IF NOT EXISTS creators (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  name          TEXT    NOT NULL,
  onlyfans_url  TEXT    NOT NULL,
  gif_url       TEXT,                            -- R2 URL of promo GIF
  bio           TEXT,
  active        BOOLEAN DEFAULT 1
);

-- PPV purchase records
CREATE TABLE IF NOT EXISTS purchases (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  telegram_user_id INTEGER NOT NULL,
  content_id       INTEGER NOT NULL,
  stars_paid       INTEGER NOT NULL,
  purchased_at     DATETIME DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (content_id) REFERENCES content(id)
);

-- Promo post log — used by rate limiter to count /promo posts alongside content posts
CREATE TABLE IF NOT EXISTS promo_posts (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  posted_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- Index for fast purchase lookup (user + content)
CREATE UNIQUE INDEX IF NOT EXISTS idx_purchases_user_content
  ON purchases (telegram_user_id, content_id);
