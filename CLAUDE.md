# CLAUDE.md — Telegram Content Bot (KinkyBeatrice)

## Workflow Rules
- **Verify deploy target before deploying**: Before running any deploy command, confirm which Netlify site ID / project it will deploy to. Check `.netlify/state.json` or use `--site` flag explicitly. Deploying to the wrong site is a silent failure — the correct site gets nothing.
- **Update CLAUDE.md after every push**: After every git push, update the project's CLAUDE.md to reflect any changes made — new features, changed behaviour, updated stack details, new rules. Commit and push the CLAUDE.md update immediately after.

## Project Overview

A fully automated Telegram posting bot for @kinkybeatricelounge running locally on Mac.
Posts every 15 minutes in a 4-slot repeating cycle — no manual intervention needed once started.

**Channel:** @kinkybeatricelounge
**Bot runs:** locally via `start_bot.command` (not on Render)
**Render:** used only for Acaption bot — TelegramApp Render blueprints have been deleted

---

## Process Management — launchd

**Label**: `com.adam.telegram-bot`
**Plist**: `~/Library/LaunchAgents/com.adam.telegram-bot.plist`
**Mode**: `KeepAlive: true`, `RunAtLoad: true` — restarts on crash, starts on login after reboot

Both `watcher.py` and `bot.py` are managed by a single plist using a `bash -c` wrapper. When `bot.py` exits, `watcher.py` is killed too, so launchd restarts both cleanly together. Independent — no Chromium dependency.

The `/autostart` scheduler command auto-resumes on bot restart — no manual intervention needed.

```bash
# Check running
launchctl list | grep telegram-bot

# Restart
launchctl bootout gui/$(id -u)/com.adam.telegram-bot
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.adam.telegram-bot.plist

# Logs
tail -f ~/Desktop/TelegramApp/bot.log
tail -f ~/Desktop/TelegramApp/bot_err.log
```

**Do NOT use `start_bot.command`** while the launchd agent is active — it would create a duplicate instance. Use launchctl commands above instead.

## How to Start

```bash
# Double-click:
start_bot.command        # starts watcher.py + bot.py

# Then DM the bot:
/autostart               # kicks off the 4-slot scheduler
                         # (auto-resumes on restart — no need to /autostart again)
```

---

## 4-Slot Auto-Scheduler (1-hour cycle, 15-min slots)

| Slot | What fires | Media source | Caption source |
|---|---|---|---|
| :00 | Free post | `posted_archive.json` file_path (own content, non-GG) | Archive caption + VIP suffix |
| :15 | GG promo | `/Volumes/All/GG/gifs/{slug}/{slug}.gif` | Archive matched by slug in file_path |
| :30 | PPV video | `/Volumes/All/Videos/0-1 min/` + `1-5 min/` | Grok-4-fast rephrase of filthy template |
| :45 | GG promo | same as :15, rotation continues | same as :15 |

**Caption cache:** `~/Desktop/XAutoPosting/posted_archive.json` — shared with XAutoPosting.
3 of 4 slots reuse archived captions (zero Grok cost). Only PPV calls Grok.

**Free post suffix (appended to every free post caption):**
```
For paid fucks without other girls sub to Kinky Beatrice No Promo Lounge at https://onlyfans.com/kinkybeatricevip/c15
```

**PPV caption format:**
```
💋 [Grok-4-fast filthy rephrase] 💋
```
No VIP lounge line on PPV posts.

**Scheduler commands:**
- `/autostart` — start scheduler (also auto-resumes on bot restart)
- `/autostop` — stop scheduler
- `/autostatus` — show status, pool sizes, next video/creator
- `/setppvprice <stars>` — change default PPV price (currently 1000 ⭐ = ~$9.10 net)

---

## Architecture

```
/Volumes/All/                     # Local media library (Mac external drive)
├── Images/                       # KB's own photos
├── Gifs/                         # KB's own GIFs
├── Videos/0-1 min/               # PPV video pool
├── Videos/1-5 min/               # PPV video pool
└── GG/gifs/{slug}/{slug}.gif     # GG creator promo GIFs

XAutoPosting/posted_archive.json  # Shared caption cache (2600+ entries)
         ↓
bot.py (local)                    # Reads archive, uploads to R2, posts to Telegram
         ↓
Cloudflare R2                     # Media storage (CDN)
         ↓
Telegram Bot API                  # Channel + DM delivery
```

---

## Tech Stack

| Layer | Choice |
|---|---|
| Bot framework | python-telegram-bot v21 + APScheduler (job-queue extra) |
| Database | SQLite (`bot.db`) locally |
| Media storage | Cloudflare R2 (S3-compatible, free tier) |
| Image compression | Pillow (auto resize/compress to fit Telegram 10MB/10000px limits) |
| Video compression | ffmpeg (compress >45MB, GIF→MP4 for >50MB GIFs) |
| Orientation fix | ffprobe (detect) + ffmpeg transpose / Pillow exif_transpose |
| Caption AI | Grok-4-fast (xai API, temp=0.9, PPV only) |
| Payments | Telegram Stars (XTR, no provider setup needed) |

---

## Manual Commands (owner-only)

- `/post <id> [caption]` — post free content from DB
- `/ppv <id> <stars> [caption]` — mark as PPV and post teaser
- `/promo <creator_id>` — post a creator GIF manually
- `/schedule` — show unposted queue

---

## Database Schema (SQLite `bot.db`)

```sql
content     — id, file_url, teaser_url, file_type, caption, is_ppv,
              ppv_price_stars, posted, posted_at, uploaded_at, source_path
creators    — id, name, onlyfans_url, gif_url, bio, active, last_promoted_at
purchases   — id, telegram_user_id, content_id, stars_paid, purchased_at
promo_posts — id, posted_at
config      — key, value  (scheduler state: auto_enabled, gg_creator_index,
                            ppv_video_index, free_archive_index, ppv_price_stars)
```

---

## Media Handling Rules

- **Images >10 MB or width+height >10,000 px:** Pillow auto-compress + resize to portrait
- **Videos >45 MB:** ffmpeg compress (720p CRF28 → 480p CRF30 fallback, 1800s timeout)
- **GIFs >50 MB:** ffmpeg convert to MP4 (sendAnimation handles MP4 fine)
- **Landscape orientation:** auto-rotated to portrait (EXIF for images, ffprobe+transpose for video)
- **Files ≤50 MB:** downloaded as bytes and sent directly (avoids R2 CDN Cloudflare block)
- **Files >50 MB:** sent via R2 URL (Telegram fetches directly)

---

## GG Creators List

53 creators in `GG_CREATORS` in `bot.py`. Each entry: `(slug, trial_link)`.
GIF path convention: `/Volumes/All/GG/gifs/{slug}/{slug}.gif`

**To add a new creator:** add to `GG_CREATORS` list in `bot.py`, matching the slug
used in XAutoPosting and the folder name in `/Volumes/All/GG/gifs/`.

**Current creators include:** goodgirlhana, monica-de-mistress, adalyn-diary,
ambersplayland, sya, quincysin1, luna-dray + 46 others (see bot.py for full list).

---

## PPV Flow

1. Bot posts text-only teaser with Grok caption + "Unlock for X Stars" button to channel
2. User taps → bot sends Telegram Stars invoice via DM
3. `pre_checkout_query` → validate & approve (within 10 s)
4. `successful_payment` → record in `purchases`, DM full video to user

Default price: **1,000 Stars (~$9.10 net)**. Change with `/setppvprice`.

---

## R2 Cleanup

Daily job at 03:00 UTC deletes R2 objects for content posted >7 days ago,
**skipping** items that have been purchased (buyers need continued access).

---

## Environment Variables

```env
BOT_TOKEN=
CHANNEL_ID=@kinkybeatricelounge
OWNER_TELEGRAM_IDS=7677422869
R2_ACCOUNT_ID=
R2_ACCESS_KEY_ID=
R2_SECRET_ACCESS_KEY=
R2_BUCKET_NAME=kinkybeatriceloungebot
R2_PUBLIC_URL=https://pub-cbe3a6abe8564a649cb1c88bfda6a420.r2.dev
DATABASE_URL=sqlite:///bot.db
FLASK_SECRET_KEY=
MINI_APP_URL=https://classy-chaja-7120c1.netlify.app
GROK_API_KEY=                     # xai- key (shared with XAutoPosting)
DEFAULT_PPV_PRICE=1000
# SLOT_GAP_SECONDS=15             # uncomment for fast test cycle (60s total)
```

---

## Key Notes

- **Bot runs locally** — Mac must stay awake. No Render for this workflow.
- **Render is for Acaption only** — TelegramApp Render blueprints deleted.
- **Archive grows daily** — XAutoPosting appends to `posted_archive.json` automatically,
  growing the free post and promo caption pool without any manual action.
- **Duplicate process protection** — if `start_bot.command` is double-clicked,
  kill duplicate PIDs with `pgrep -f "bot.py|watcher.py" | xargs kill`
- **PTB version:** v21 — requires `pip install "python-telegram-bot[job-queue]"`
- **Stars conversion:** 1 Star = $0.013 gross, Telegram keeps 30%, net ≈ $0.0091/star
