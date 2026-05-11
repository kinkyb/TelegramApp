# CLAUDE.md — Telegram Content Bot (KinkyBeatrice)

> **Cross-system context**: see [../CLAUDE.md](../CLAUDE.md) for the master promo architecture (Google Sheet → OFGG / OFMessaging / TelegramApp / kinkyfreefux).

## Workflow Rules
- **Verify deploy target before deploying**: Before running any deploy command, confirm which Netlify site ID / project it will deploy to. Deploying to the wrong site is a silent failure.
- **Update CLAUDE.md after every push**: After every git push, update the project's CLAUDE.md.

## Process Management — launchd

**Label**: `com.adam.telegram-bot`
**Plist**: `~/Library/LaunchAgents/com.adam.telegram-bot.plist`
**Mode**: `KeepAlive: true`, `RunAtLoad: true`

Both `watcher.py` and `bot.py` managed by a single plist. Independent — no Chromium dependency.
The `/autostart` scheduler command auto-resumes on bot restart — no manual intervention needed.

```bash
launchctl list | grep telegram-bot
launchctl bootout gui/$(id -u)/com.adam.telegram-bot
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.adam.telegram-bot.plist
tail -f ~/Desktop/TelegramApp/bot.log
tail -f ~/Desktop/TelegramApp/bot_err.log
```

**Do NOT use `start_bot.command`** while the launchd agent is active — creates a duplicate instance.

If double-clicked by accident: `pgrep -f "bot.py|watcher.py" | xargs kill`

## 4-Slot Auto-Scheduler (1-hour cycle, 15-min slots)

| Slot | What fires | Media source | Caption source |
|---|---|---|---|
| :00 | Free post | `posted_archive.json` file_path (own content, non-GG) | Archive caption + VIP suffix |
| :15 | GG promo | `/Volumes/All/GG/gifs/{slug}/{slug}.gif` | Archive matched by slug |
| :30 | PPV video | `/Volumes/All/Videos/0-1 min/` + `1-5 min/` | Grok-4-fast rephrase of filthy template |
| :45 | GG promo | same as :15, rotation continues | same as :15 |

**Caption cache:** `~/Desktop/XAutoPosting/posted_archive.json` — shared with XAutoPosting.
3 of 4 slots reuse archived captions (zero Grok cost). Only PPV calls Grok.

**Free-post selector filter** (fix 2026-05-01, [bot.py:1203](bot.py:1203)) — `_get_own_archive_entries` filters by `post_type != "gg"` (canonical) plus `/GG/` path guard. Previously only checked the path substring, which leaked 466 GG entries into the free-post pool because their `file_path` points at `~/Desktop/kinkyfreefux/videos/{slug}.mp4` (no `/GG/` substring). Symptom: free post arrived with VIP-lounge CTA suffix but a girl's media (e.g., Adalyn).

**VIP Lounge suffix (appended to every post across all 4 slots — free, GG promo ×2, PPV):**
```
💋 To get a 3-month access to my OnlyFans VIP No Promo Lounge https://onlyfans.com/kinkybeatricevip/c15 for FREE go to kinkyfreefux.com and follow instructions 💋
```
Defined as `VIP_LOUNGE_SUFFIX` at [bot.py:92](bot.py:92). Free post: appended after archive caption with `\n\n`. GG promo: appended after `💋 {trial_link} 💋` line with `\n\n` (girl's trial link still primary CTA via inline button). PPV: appended after `💋 {teaser} 💋` with `\n\n` (unlock button still primary CTA).

**Stars conversion rate:** 1 Star = $0.013 gross; Telegram keeps 30%; net ≈ $0.0091/star

## Tech Stack
| Layer | Choice |
|---|---|
| Bot framework | python-telegram-bot v21 + APScheduler (job-queue extra) |
| Database | SQLite (`bot.db`) locally |
| Media storage | Cloudflare R2 (S3-compatible, free tier) |
| Caption AI | Grok-4-fast (PPV only) |
| Payments | Telegram Stars (XTR) |

**PTB version:** v21 — requires `pip install "python-telegram-bot[job-queue]"`

## Media Handling Rules
| Condition | Action |
|---|---|
| Images >10 MB or width+height >10,000 px | Pillow auto-compress + resize to portrait |
| Videos >45 MB | ffmpeg compress (720p CRF28 → 480p CRF30 fallback, 1800s timeout) |
| GIFs >50 MB | ffmpeg convert to MP4 |
| Files ≤50 MB | Downloaded as bytes and sent directly (avoids R2 CDN Cloudflare block) |
| Files >50 MB | Sent via R2 URL |

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
GROK_API_KEY=
DEFAULT_PPV_PRICE=1000
```
