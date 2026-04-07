# CLAUDE.md — Telegram Content Bot + Mini App

## Project Overview

A Telegram bot and mini app for a solo content creator to:
- Auto-post content (images, video, GIFs) from a local library to a Telegram channel
- Promote ~50 other creators with GIF cards linking to their OnlyFans pages
- Charge for selected posts via Telegram Stars (PPV, occasional)
- Serve a scrollable mini app feed inside Telegram

---

## Architecture

```
local-library/
├── my-content/        # Creator's own photos/videos
└── promo-gifs/        # GIFs for promoted creators

sync-script (Python)   # Watches local folders, uploads to R2
         ↓
Cloudflare R2          # Cloud media storage (CDN URLs)
         ↓
Flask backend          # Bot logic, DB, payment handling
         ↓
Telegram Bot API       # Channel posts, PPV invoices, mini app
         ↓
Mini App (React)       # In-app feed UI
```

---

## Tech Stack

| Layer | Choice | Reason |
|---|---|---|
| Bot framework | python-telegram-bot v20 | Async, well-maintained |
| Backend | Flask | Already familiar |
| Database | SQLite (MVP) → Postgres | Simple to start |
| Media storage | Cloudflare R2 | Free tier, S3-compatible |
| Local sync | Watchdog (Python) | Folder watcher |
| Payments | Telegram Stars | No approval needed, instant |
| Mini app frontend | React + Vite | Fast dev, TG SDK support |

---

## Core Components

### 1. Local Library Watcher (`watcher.py`)

- Monitors `local-library/my-content/` and `local-library/promo-gifs/`
- On new file detected:
  - Uploads to Cloudflare R2
  - Inserts record into SQLite DB with metadata:
    - `file_url`, `file_type`, `creator_id` (null for own content), `is_ppv` (default false), `ppv_price_stars`, `uploaded_at`
- Runs as a background daemon

### 2. Database Schema (`schema.sql`)

```sql
-- Own content
CREATE TABLE content (
  id INTEGER PRIMARY KEY,
  file_url TEXT NOT NULL,
  file_type TEXT,           -- image, video, gif
  caption TEXT,
  is_ppv BOOLEAN DEFAULT 0,
  ppv_price_stars INTEGER,
  posted BOOLEAN DEFAULT 0,
  posted_at DATETIME,
  uploaded_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- Promoted creators
CREATE TABLE creators (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  onlyfans_url TEXT NOT NULL,
  gif_url TEXT,             -- R2 URL of promo GIF
  bio TEXT,
  active BOOLEAN DEFAULT 1
);

-- PPV purchase records
CREATE TABLE purchases (
  id INTEGER PRIMARY KEY,
  telegram_user_id INTEGER,
  content_id INTEGER,
  stars_paid INTEGER,
  purchased_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

### 3. Telegram Bot (`bot.py`)

**Commands:**
- `/post [content_id]` — manually post a specific item to channel
- `/ppv [content_id] [stars]` — mark content as PPV and post
- `/promo [creator_id]` — post a creator promo GIF to channel
- `/schedule` — show posting queue

**Channel posting logic:**
- Free content: sends media directly to channel with caption
- PPV content: sends blurred preview + Stars invoice button
- Promo post: sends creator GIF with OF link button

**PPV Flow:**
1. Bot posts preview (low-res or blurred) to channel
2. User clicks "Unlock for X Stars"
3. Bot sends Telegram Stars invoice via `send_invoice`
4. On `pre_checkout_query`: validate and answer ok
5. On `successful_payment`: record in `purchases` table, send full content to user via DM

### 4. Mini App Backend (Flask routes)

```
GET  /api/feed              → paginated content list (free items)
GET  /api/creators          → all active promoted creators
GET  /api/content/:id       → single item (checks purchase if PPV)
POST /api/purchase/verify   → verify Stars payment, return content URL
```

### 5. Mini App Frontend (React)

**Pages:**
- `Feed` — scrollable grid/list of own content + interleaved creator promos
- `CreatorCard` — GIF, name, bio, "Visit OnlyFans" button
- `PPVModal` — Stars payment prompt for locked content

**Telegram SDK integration:**
```javascript
const tg = window.Telegram.WebApp;
tg.ready();
tg.expand();
// Use tg.initDataUnsafe.user for user identity
// Use tg.MainButton for primary actions
```

---

## Environment Variables

```env
BOT_TOKEN=your_botfather_token
R2_ACCOUNT_ID=
R2_ACCESS_KEY_ID=
R2_SECRET_ACCESS_KEY=
R2_BUCKET_NAME=
R2_PUBLIC_URL=https://your-bucket.r2.dev
CHANNEL_ID=@yourchannel
DATABASE_URL=sqlite:///bot.db
FLASK_SECRET_KEY=
MINI_APP_URL=https://your-mini-app-domain.com
```

---

## File Structure

```
project/
├── CLAUDE.md
├── .env
├── requirements.txt
├── schema.sql
├── watcher.py           # Local folder watcher + R2 uploader
├── bot.py               # Telegram bot (python-telegram-bot)
├── app.py               # Flask API server
├── db.py                # DB helpers
├── r2.py                # Cloudflare R2 upload helpers
├── mini-app/
│   ├── index.html
│   ├── src/
│   │   ├── main.jsx
│   │   ├── App.jsx
│   │   ├── components/
│   │   │   ├── Feed.jsx
│   │   │   ├── CreatorCard.jsx
│   │   │   └── PPVModal.jsx
│   │   └── api.js
│   └── vite.config.js
└── local-library/       # Gitignored — local media only
    ├── my-content/
    └── promo-gifs/
```

---

## Build Order for Claude Code

1. `schema.sql` + `db.py` — database foundation
2. `r2.py` — R2 upload/URL helpers
3. `watcher.py` — local folder watcher
4. `bot.py` — core bot with channel posting + PPV Stars flow
5. `app.py` — Flask API for mini app
6. `mini-app/` — React frontend

---

## Key Constraints

- All media served from R2 public URLs — bot never serves from local disk
- Bot must answer `pre_checkout_query` within 10 seconds (Telegram requirement)
- Mini app must call `tg.ready()` immediately on load
- SQLite is fine for MVP; migrate to Postgres before scaling
- `local-library/` must be in `.gitignore` — never commit media
- Stars conversion: 1 Star ≈ $0.013 USD; Telegram keeps 30%

---

## Claude Code Instructions

You are building a Telegram content bot and mini app from this CLAUDE.md spec.

**Rules:**
- Follow the file structure exactly
- Write async Python throughout (python-telegram-bot v20 uses asyncio)
- Use `boto3` with custom endpoint for Cloudflare R2 (not AWS S3)
- Never hardcode credentials — always read from `.env` via `python-dotenv`
- Add docstrings to every function
- SQLite WAL mode on for concurrent read/write
- All Flask routes return JSON
- React components use functional style with hooks only
- Telegram Stars invoices use `currency="XTR"` and `prices=[LabeledPrice("Unlock", stars_amount)]`
- Test PPV flow end-to-end before moving to mini app

**Start with:** `schema.sql` → `db.py` → `r2.py` → `watcher.py` → `bot.py`
