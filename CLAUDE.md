# CLAUDE.md — Telegram Content Bot (KinkyBeatrice)

> **Cross-system context**: see [../CLAUDE.md](../CLAUDE.md) for the master promo architecture (Google Sheet → OFGG / OFMessaging / TelegramApp / kinkyfreefux).

## Workflow Rules
- **Verify deploy target before deploying**: Before running any deploy command, confirm which Netlify site ID / project it will deploy to. Deploying to the wrong site is a silent failure.
- **Update CLAUDE.md after every push**: After every git push, update the project's CLAUDE.md.
- **⛔ Before EVERY new build, assume a bug exists and find it — whether or not one has been reported.** This is a default-on rule, not a conditional. Re-scan the entire pipeline end-to-end — front-end (UI / IPC / renderer) AND back-end (main process, business logic, pipeline, bundling) — actively hunting for the bug you've assumed is there. If you find one, fix it and re-scan. If after a thorough double-check you're 100% sure no bug exists, build a standalone repro of the **full production chain** (every post-processing step — watermarks, composites, encoders, format conversions, IPC envelope) and prove the path produces the expected output. Only at that point do you tag / commit / trigger the build. Burned 2026-05-12 on PerfectStudio v1.2.2: standalone test omitted the trailing watermark composite, missing that sharp's `.composite()` overwrites prior overlays when chained — v1.2.3 was the real fix. Cost of skipping: 2 wasted ~20-min CI builds + user-visible repeat failure + credibility hit.
- **⛔ Windows Electron installers: always hide the in-window menu bar before shipping.** Electron defaults to showing a `File / Edit / View / Help` strip across the top of every BrowserWindow on Windows (and Linux), which looks unprofessional for a focused desktop app. Required fix: call `win.setMenuBarVisibility(false)` once per window right after `win.loadFile(...)`, with NO platform guard. macOS treats it as a no-op (menu lives in the system menu bar at the top); Windows hides the strip. The menu can stay registered via `Menu.setApplicationMenu(...)` — visibility and registration are independent, so keyboard accelerators still fire and Alt brings the bar back when needed. Always re-verify on a fresh Windows install before shipping a new desktop-app version. Burned 2026-05-12 on PerfectStudio v1.2.3: `main.js` had `if (process.platform === 'darwin') win.setMenuBarVisibility(false)` — macOS-only guard, so Windows users saw the strip; fixed in v1.2.4 by removing the guard.


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
| :10 | Free post | `posted_archive.json` file_path (own content, non-GG) | Archive caption + VIP suffix |
| :25 | GG promo | `/Volumes/All/GG/gifs/{slug}/**` (recursive pool, per-slug rotation) | Archive matched by slug |
| :40 | PPV video | `/Volumes/All/Videos/0-1 min/` + `1-5 min/` | Grok-4-fast rephrase of filthy template |
| :55 | GG promo | same as :25, rotation continues | same as :25 |

**Wall-clock anchors** (changed 2026-05-14, was `:00/:15/:30/:45` and drifting): each slot is independently anchored to a wall-clock minute via `_seconds_until_minute()` at [bot.py:91](bot.py:91), so restarts no longer slide the schedule. The 4 minutes `:10/:25/:40/:55` were chosen to keep TG entirely off the heavily-clustered `:00` and `:30` minutes (where the OF stack and VIP stack already pile up) and fill the previously-idle `:40` and `:55` minutes. Only applies when `cycle == 3600` (production); test cycles (`SLOT_GAP_SECONDS=15` → 60 s cycle) fall back to relative offsets so fast-cycle local testing still works. Both `cmd_autostart` and the `main()` restore block use `_slot_first_offsets(gap, cycle)` to compute `first=` values.

**GG promo media pool** (since 2026-05-13): `_scan_gg_pool(slug)` at [bot.py:102](bot.py:102) recursively walks `/Volumes/All/GG/gifs/{slug}/` for every `.gif/.jpg/.jpeg/.png/.mp4` (excluding `._*` macOS metadata), sorted. Scan runs inside a hang-protected 10s thread executor — same pattern OFGG uses — because `/Volumes/All` is a network/external mount that can stall the asyncio loop. Per-slug rotation index stored as config key `gg_media_index_{slug}` in `bot.db`, advanced one position per fire (wraps mod pool size). On empty pool the creator_index is rolled back so the same girl is retried on the next slot. R2 key is hash-based (`promo-gifs/{slug}/{md5(relpath)[:12]}{ext}`) so subfolder paths with `@`/spaces never break public URLs, and repeat picks of the same file hit the R2 cache. Pre-2026-05-13 the bot read exactly one canonical file `{slug}.gif`; that file is just one entry of many now.

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

## File Overwrite Policy

- **⛔ Never silently overwrite a local file.** Before any operation that would replace an existing file on the local machine (image/format conversion, codegen, downloads, copies, moves to an occupied path, save-as targets, batch processing), check whether a same-named file already exists at the destination. If it does, **stop and ask the user**: overwrite, or pick a new name? Do not decide on your own based on file size, content similarity, timestamps, single-frame-vs-animated checks, or any other heuristic. The user has to choose. This applies even when the existing file looks redundant, auto-generated, or "obviously" derived from the same source. **Burned 2026-05-12**: silently overwrote 33 single-frame `.gif` files in `/Volumes/All/Gifs/1-25 MB/` during a batch JPG→GIF conversion after self-deciding it was safe.
