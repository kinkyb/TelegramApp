"""bot.py — Telegram bot for the content channel.

Manual commands (owner-only):
  /post [content_id]                    — post a free content item to the channel
  /ppv  [content_id] [stars] [caption]  — mark as PPV and post teaser + unlock button
  /promo [creator_id]                   — post a creator promo GIF to the channel
  /schedule                             — list unposted content queue

Auto-scheduler commands (owner-only):
  /autostart    — start the 4-slot repeating scheduler
  /autostop     — stop the scheduler
  /autostatus   — show scheduler status and config
  /setppvprice  — set default PPV price for auto-posted videos

Scheduler slots (each cycle = 4 × SLOT_GAP_SECONDS, default 1 h):
  :00  free post  — next unposted image/GIF from DB
  :15  promo girl — next creator from GG_CREATORS rotation (GIF + trial link)
  :30  PPV video  — next video from /Volumes/All/Videos pool (text-only teaser)
  :45  promo girl — rotation continues

Captions (cheapest first):
  1. XAutoPosting posted_archive.json  (free reuse)
  2. Grok API                          (billed — requires GROK_API_KEY in .env)
  3. Static fallback

Run:  python bot.py
"""

import asyncio
import base64
import json
import logging
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PreCheckoutQueryHandler,
    filters,
)

import db
import r2

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

BOT_TOKEN  = os.environ["BOT_TOKEN"]
CHANNEL_ID = os.environ["CHANNEL_ID"]      # e.g. "@yourchannel" or "-100123456"

# Minimum gap between manual channel posts (free, PPV, promo)
MIN_POST_GAP_MINUTES = 1   # set back to 15 for production once auto-scheduler is running

_OWNER_IDS_RAW = os.getenv("OWNER_TELEGRAM_IDS", "")
OWNER_IDS: set[int] = {
    int(uid.strip()) for uid in _OWNER_IDS_RAW.split(",") if uid.strip().isdigit()
}

# ---------------------------------------------------------------------------
# Auto-scheduler constants
# ---------------------------------------------------------------------------

# Gap between each of the 4 posting slots.  Full cycle = 4 × gap.
# Set SLOT_GAP_SECONDS=15 in .env for fast test cycles (60 s total).
# Production default: 900 s = 15 min  →  1-hour cycle.
SLOT_GAP_SECONDS  = int(os.getenv("SLOT_GAP_SECONDS", "900"))
AUTO_JOB_NAMES    = ["auto_free", "auto_promo_1", "auto_ppv", "auto_promo_2"]

GROK_API_KEY      = os.getenv("GROK_API_KEY", "")
GROK_API_URL      = "https://api.x.ai/v1/chat/completions"
VIP_LOUNGE_URL    = "https://onlyfans.com/kinkybeatricevip"
FREE_POST_SUFFIX  = "For paid fucks without other girls sub to Kinky Beatrice No Promo Lounge at https://onlyfans.com/kinkybeatricevip/c15"
DEFAULT_PPV_PRICE = int(os.getenv("DEFAULT_PPV_PRICE", "1000"))

XAUTOPOST_ARCHIVE = Path.home() / "Desktop" / "XAutoPosting" / "posted_archive.json"
GG_GIFS_BASE      = Path("/Volumes/All/GG/gifs")
PPV_VIDEO_DIRS    = [
    Path("/Volumes/All/Videos/0-1 min"),
    Path("/Volumes/All/Videos/1-5 min"),
]
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}

# GG creators mirrored from XAutoPosting (slug, trial_link)
GG_CREATORS = [
    ("goodgirlhana",       "https://onlyfans.com/goodgirlhana/trial/98x1xaq09yo46pubcbtvd1urmaftc2cr"),
    ("monica-de-mistress", "https://onlyfans.com/monica_de_mistress/trial/iqq6lgdsoa1qaietvreotxhduhgvnqzx"),
    ("elizabeth-baby",     "https://onlyfans.com/elizabeth_baby/c28"),
    ("sunny-bunny-xx",     "https://onlyfans.com/sunny.bunny.xx/trial/7ylsomutjw1nxfywiiupgi7nzjinwfip"),
    ("slimkary",           "https://onlyfans.com/slimkary/c145"),
    ("panteritaaa",        "https://onlyfans.com/panteritaaa/trial/zo6w1whexceytrfwfw6h1ooozyjkidrb"),
    ("only-mei",           "https://onlyfans.com/only_mei/trial/txs37zalkqjk4rssautccfafngkidrp5"),
    ("azaleahijabi",       "https://onlyfans.com/azaleahijabi/trial/5qd6oeiyrwzyls8l7uuc2hz85byglibu"),
    ("emmilee-xx",         "https://onlyfans.com/emmilee.xx/trial/9t1i2mntfpftkuchct94jg5wkfxzbeb0"),
    ("matchakeira",        "https://onlyfans.com/matchakeira/trial/1yciuersvwym7qi8c9jbkpqfhswpyr79"),
    ("avemylissaa",        "https://onlyfans.com/avemylissaa/trial/t8vuxpmveycvjmkq3owca5hshqb7v0nh"),
    ("wondereva",          "https://onlyfans.com/wondereva/trial/5d4zgknosnk0lmbfqbfpwy4xt4urunaa"),
    ("tiffanybei",         "https://onlyfans.com/tiffanybei/trial/ufzlmaxsumnlvpoxxdlqio1v4q3taqzp"),
    ("cutenbusty",         "https://onlyfans.com/cutenbusty/trial/s4ngoljde0skwykyseq8ltbctekgdooi"),
    ("bunnymya",           "https://onlyfans.com/bunnymya/trial/aloggn408jeg85d8ise4sioyjuufcdk7"),
    ("cindybrella",        "https://onlyfans.com/cindybrella/trial/2pe7pbtnkr1p2v8epr6ly1ic9kftm0jw"),
    ("sasha-leee",         "https://onlyfans.com/sasha.leee/trial/m36ffxu9wfgkzazuuzyb5ag9fzqlkesm"),
    ("desiiidesiresxoxo",  "https://onlyfans.com/desiiidesiresxoxo/trial/imaahqh7bj5v2jlny6cj1gcbq057yu1s"),
    ("adalyn-diary",       "https://onlyfans.com/adalyn.diary/trial/eruzckdvatp83qabccwo3ucmgz0nipv4"),
    ("curvy-kate-lv",      "https://onlyfans.com/curvy_kate_lv/trial/dqeiu2f2uiv144l8udvoxwie8lu7vwsk"),
    ("miraclevoyage",      "https://onlyfans.com/miraclevoyage/trial/ejdvrbbzhmymuvyz9ytjyu3caak6rq8q"),
    ("yukixbunz",          "https://onlyfans.com/yukixbunz/trial/fplzvseccs21cztl6anlpzf0k0ric6nf"),
    ("bbwdalia",           "https://onlyfans.com/bbwdalia/trial/rxwzymeezcl7vfqkaroxqcojkyowfiv4"),
    ("nathasly",           "https://onlyfans.com/nathasly/c142"),
    ("itsemmawilsonn",     "https://onlyfans.com/itsemmawilsonn/c207"),
    ("nayashka",           "https://onlyfans.com/nayashka/c29"),
    ("nerdyyumi",          "https://onlyfans.com/nerdyyumi/trial/5tnikbkcrun4ti0k6y5b63mcpj4tls4g"),
    ("lizzy-vixxen",       "https://onlyfans.com/lizzy_vixxen/trial/dziftnnd7mipdmhmrmtu6f0tgx5xp3tq"),
    ("hijabisofiya",       "https://onlyfans.com/hijabisofiya/trial/xsu2etgswsfyefkjbpolp7vtcey3crxm"),
    ("lenadrains",         "https://onlyfans.com/lenadrains/c162"),
    ("leonayla",           "https://onlyfans.com/leonayla/trial/xofiuyrxc8qtcybjdhscz1i7iwiiaaqd"),
    ("aryastark2024",      "https://onlyfans.com/aryastark2024/trial/klmvadpjkgaaqejcqjdqh2sorjn1absy"),
    ("lris-adamsone",      "https://onlyfans.com/lris_adamsone/trial/o1gnpth1skdws5ilywo5fplj5jtyopmx"),
    ("callysto-nymph",     "https://onlyfans.com/callysto_nymph/trial/nvm5nblltdjificlx5loq3mh3zza8wad"),
    ("sweetteonly",        "https://onlyfans.com/sweetteonly/trial/fczpzmn7wtfdmdkik86ueq65aupggxbw"),
    ("bunnyholez",         "https://onlyfans.com/bunnyholez/trial/xcqew99hpbxncx4v42mpeeinu41idunk"),
    ("gina-a",             "https://onlyfans.com/gina_a/trial/coq6pzex8l0txort8h08azddjilurzvs"),
    ("yumi-neko",          "https://onlyfans.com/yumi_neko/trial/d3oyijd1ealmio035dig3gfoulo5pxaa"),
    ("peeeachypie",        "https://onlyfans.com/peeeachypie/trial/eqit8wsyy5xafsw9nhhel5ffwdl2vuts"),
    ("tureinafire",        "https://onlyfans.com/tureinafire/trial/qui5kcyamxxcaenegrynpqkcepawsvbv"),
    ("esteladior",         "https://onlyfans.com/esteladior/c664"),
    ("rosedomi",           "https://onlyfans.com/rosedomi/trial/lwv21q2luc9qla8seawyxlpxdlv2x4dh"),
    ("xprincessnx",        "https://onlyfans.com/xprincessnx/trial/2d60smotandzadtgbzo7kektch8it9x1"),
    ("aurora-shadow",      "https://onlyfans.com/aurora_shadow/c26"),
    ("lilthiccckk",        "https://onlyfans.com/lilthiccckk/trial/x5no7rhwclmyprbln7pwn845rqzor79u"),
    ("your-tasha",         "https://onlyfans.com/your.tasha/trial/ct1o1g1smcqyngk6ul9ikd34ce8ocpew"),
    ("ambersplayland",     "https://onlyfans.com/ambersplayland/trial/of98c3bocr1grtkaglibm2kgwfgpn9yg"),
    ("emillia3",           "https://onlyfans.com/emillia3/c73"),
    ("lachicarocio",       "https://onlyfans.com/lachicarocio/c31"),
    ("softyasmin",         "https://onlyfans.com/softyasmin/trial/yn1na8f4ldec2lbjykqicpomovlxn17x"),
    ("sya",                "https://onlyfans.com/action/trial/h0habqtcqxyjgdmej2ivfakgtbanrzhk"),
    ("quincysin1",         "https://onlyfans.com/quincysin1/trial/kqpa6zi6x27tire7ov5sokjdv82k2bmw"),
    ("luna-dray",          "https://onlyfans.com/luna_dray/trial/5goefsddh7xwavm9yzilwkgzxdnihkk2"),
    ("nikkita-ts",         "https://onlyfans.com/nikkita.ts/c52"),
    ("littlefoxira",       "https://onlyfans.com/littlefoxira/trial/kgnqmez8dgqmb3tzowmnw5bbzle6azya"),
]


# ---------------------------------------------------------------------------
# Auth guard
# ---------------------------------------------------------------------------

def _is_owner(update: Update) -> bool:
    """Return True if the message sender is an authorised owner."""
    user = update.effective_user
    if user is None:
        return False
    if not OWNER_IDS:
        return True   # dev mode: no owners configured
    return user.id in OWNER_IDS


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

def _check_rate_limit() -> str:
    """Return an error string if posting too soon, or empty string if clear.

    Reads the last posted_at timestamp from the DB and compares to now.
    """
    last = db.get_last_posted_at()
    if last is None:
        return ""

    try:
        last_dt = datetime.fromisoformat(last).replace(tzinfo=timezone.utc)
    except ValueError:
        return ""

    now       = datetime.now(tz=timezone.utc)
    min_gap   = timedelta(minutes=MIN_POST_GAP_MINUTES)
    elapsed   = now - last_dt

    if elapsed < min_gap:
        remaining = int((min_gap - elapsed).total_seconds() // 60) + 1
        return f"Too soon — last post was {int(elapsed.total_seconds() // 60)} min ago. Wait ~{remaining} more min."

    return ""


# ---------------------------------------------------------------------------
# Media send helpers
# ---------------------------------------------------------------------------

TELEGRAM_UPLOAD_LIMIT = 50 * 1024 * 1024   # 50 MB — Bot API multipart limit
TELEGRAM_PHOTO_LIMIT  = 10 * 1024 * 1024   # 10 MB — send_photo hard limit


def _get_file_size(url: str) -> int:
    """Return remote file size in bytes via HEAD request, or 0 on failure."""
    import requests as _requests
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
    try:
        resp = _requests.head(url, headers=headers, timeout=10, allow_redirects=True)
        return int(resp.headers.get("Content-Length", 0))
    except Exception:
        return 0


def _get_video_orientation(video_path: Path) -> tuple[int, int, int]:
    """Return (width, height, rotation_degrees) for a video file using ffprobe.

    Reads both the stream dimensions and the 'rotate' metadata tag so we can
    tell whether a video is actually portrait even if its raw dimensions say
    landscape (common with iOS .MOV files recorded in portrait).

    Args:
        video_path: Path to the video file.

    Returns:
        Tuple of (width, height, rotation) where rotation is 0, 90, 180, or 270.
    """
    import json as _json
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json",
         "-show_streams", "-show_entries",
         "stream=width,height:stream_tags=rotate:format_tags=rotate",
         str(video_path)],
        capture_output=True, timeout=15,
    )
    if result.returncode != 0:
        return 0, 0, 0
    try:
        data = _json.loads(result.stdout)
        streams = [s for s in data.get("streams", []) if s.get("width")]
        if not streams:
            return 0, 0, 0
        s   = streams[0]
        w   = s.get("width", 0)
        h   = s.get("height", 0)
        rot = int(s.get("tags", {}).get("rotate", 0))
        return w, h, rot
    except Exception:
        return 0, 0, 0


def _compress_image_bytes(data: bytes, max_bytes: int = TELEGRAM_PHOTO_LIMIT) -> bytes:
    """Compress and resize JPEG image bytes to satisfy Telegram's photo limits.

    Telegram requires:
      - File size ≤ 10 MB
      - width + height ≤ 10,000 px

    Progressively reduces quality then resolution until both constraints are met.

    Args:
        data: Raw image bytes.
        max_bytes: Target maximum file size in bytes.

    Returns:
        Compressed JPEG bytes, or original bytes on failure.
    """
    try:
        import io as _io
        from PIL import Image

        img = Image.open(_io.BytesIO(data)).convert("RGB")

        # Respect EXIF orientation first
        try:
            from PIL import ImageOps
            img = ImageOps.exif_transpose(img)
        except Exception:
            pass

        # Rotate landscape to portrait
        w, h = img.size
        if w > h:
            img = img.rotate(90, expand=True)
            logger.info("Image rotated 90° to portrait (%dx%d → %dx%d)", w, h, img.width, img.height)

        # Enforce Telegram's dimension constraint (w + h ≤ 10,000)
        w, h = img.size
        if w + h > 10000:
            scale = 9900 / (w + h)   # target sum ~9900 with a small margin
            new_w, new_h = int(w * scale), int(h * scale)
            img = img.resize((new_w, new_h), Image.LANCZOS)
            logger.info("Image resized from %dx%d to %dx%d (Telegram dimension limit)",
                        w, h, new_w, new_h)

        # If already small enough after resize, save at high quality and return
        buf = _io.BytesIO()
        img.save(buf, format="JPEG", quality=90, optimize=True)
        if buf.tell() <= max_bytes:
            return buf.getvalue()

        # Otherwise reduce quality until it fits
        for quality in [85, 75, 60, 45]:
            buf = _io.BytesIO()
            img.save(buf, format="JPEG", quality=quality, optimize=True)
            if buf.tell() <= max_bytes:
                logger.info("Image compressed to %.1f MB (quality=%d)",
                            buf.tell() / 1024 / 1024, quality)
                return buf.getvalue()

        # Last resort: halve resolution again
        w2, h2 = img.size
        img = img.resize((w2 // 2, h2 // 2), Image.LANCZOS)
        buf = _io.BytesIO()
        img.save(buf, format="JPEG", quality=75, optimize=True)
        logger.info("Image halved to %dx%d → %.1f MB", w2 // 2, h2 // 2,
                    buf.tell() / 1024 / 1024)
        return buf.getvalue()

    except Exception as exc:
        logger.warning("Image compression failed: %s — sending original", exc)
        return data


def _download_file(url: str) -> tuple[bytes, str]:
    """Download a file from R2 and return (bytes, filename).

    Uses requests with a browser User-Agent to avoid Cloudflare 403s.

    Args:
        url: Public R2 URL.

    Returns:
        Tuple of (raw bytes, filename string).

    Raises:
        RuntimeError: If the download fails.
    """
    import requests as _requests
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
    try:
        resp = _requests.get(url, headers=headers, timeout=120)
        resp.raise_for_status()
        filename = url.split("/")[-1]
        return resp.content, filename
    except Exception as exc:
        raise RuntimeError(f"Failed to download {url}: {exc}") from exc


async def _send_media(context: ContextTypes.DEFAULT_TYPE, chat_id, file_url: str,
                      file_type: str, caption: str = "",
                      reply_markup=None) -> None:
    """Send a single media item to any chat.

    Files ≤ 50 MB: downloaded locally and sent as bytes (avoids R2 CDN issues).
    Files  > 50 MB: passed as URL directly (Telegram fetches from R2).

    Args:
        context: PTB context.
        chat_id: Destination chat or channel id.
        file_url: Public R2 URL of the media.
        file_type: 'image', 'video', or 'gif'.
        caption: Optional caption text.
        reply_markup: Optional InlineKeyboardMarkup.
    """
    import io
    from telegram import InputFile

    kwargs = dict(caption=caption, reply_markup=reply_markup)

    file_size = _get_file_size(file_url)
    use_url   = file_size > TELEGRAM_UPLOAD_LIMIT   # too large to upload as bytes

    if use_url:
        logger.info("Large file (%.1f MB) — sending via URL: %s", file_size / 1024 / 1024, file_url)
        media = file_url
    else:
        data, filename = _download_file(file_url)
        if file_type == "image":
            data = _compress_image_bytes(data)
        media = InputFile(io.BytesIO(data), filename=filename)

    if file_type == "image":
        await context.bot.send_photo(chat_id=chat_id, photo=media, **kwargs)
    elif file_type == "video":
        await context.bot.send_video(chat_id=chat_id, video=media, **kwargs)
    elif file_type == "gif":
        await context.bot.send_animation(chat_id=chat_id, animation=media, **kwargs)
    else:
        text = f"{caption}\n{file_url}" if caption else file_url
        await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)


async def _post_free(
    context: ContextTypes.DEFAULT_TYPE,
    row,
    caption_override: str | None = None,
) -> None:
    """Post free content directly to the channel.

    Args:
        context: PTB context.
        row: content DB row.
        caption_override: If provided, use this instead of row['caption'].
    """
    caption = caption_override if caption_override is not None else (row["caption"] or "")
    await _send_media(
        context, CHANNEL_ID,
        file_url=row["file_url"],
        file_type=row["file_type"],
        caption=caption,
    )


async def _post_ppv_teaser(
    context: ContextTypes.DEFAULT_TYPE,
    row,
    caption_override: str | None = None,
) -> None:
    """Post the teaser media to the channel with an Unlock button.

    The teaser is row['teaser_url'] if set, otherwise row['file_url'].
    This gives subscribers real free content while incentivising the unlock
    for the full/extended version.

    Args:
        context: PTB context.
        row: content DB row (must have is_ppv=1).
        caption_override: If provided, use this instead of row['caption'].
    """
    content_id  = row["id"]
    price_stars = row["ppv_price_stars"]
    caption     = caption_override if caption_override is not None else (row["caption"] or "")
    teaser_url  = row["teaser_url"]   # explicit thumbnail only — never expose full content
    file_type   = row["file_type"]

    teaser_caption = (
        f"{caption}\n\n🔓 Unlock the full version for {price_stars} Stars"
        if caption
        else f"🔓 Unlock the full version for {price_stars} Stars"
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"🔓 Unlock for {price_stars} Stars",
            callback_data=f"ppv:{content_id}",
        )]
    ])

    if teaser_url:
        # Has a proper thumbnail — send it as the preview
        await _send_media(
            context, CHANNEL_ID,
            file_url=teaser_url,
            file_type="image",   # thumbnails are always JPEG
            caption=teaser_caption,
            reply_markup=keyboard,
        )
    elif file_type == "image":
        # Images act as their own teaser (no separate thumb needed)
        await _send_media(
            context, CHANNEL_ID,
            file_url=row["file_url"],
            file_type="image",
            caption=teaser_caption,
            reply_markup=keyboard,
        )
    else:
        # Video/GIF with no thumbnail — text-only post to protect the content
        await context.bot.send_message(
            chat_id=CHANNEL_ID,
            text=teaser_caption,
            reply_markup=keyboard,
        )


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

async def cmd_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /post <content_id> [caption text].

    Usage:
      /post 12              — post content ID 12 with no caption
      /post 12 My caption   — post with caption

    Args:
        update: Incoming Telegram update.
        context: PTB context with args.
    """
    if not _is_owner(update):
        await update.message.reply_text("Not authorised.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /post <content_id> [caption]\nExample: /post 12 Come join me 🔥")
        return

    try:
        content_id = int("".join(filter(str.isdigit, context.args[0])))
        if not content_id:
            raise ValueError
    except (ValueError, IndexError):
        await update.message.reply_text("Usage: /post <content_id> [caption]\nExample: /post 12 Come join me 🔥")
        return

    rate_err = _check_rate_limit()
    if rate_err:
        await update.message.reply_text(rate_err)
        return

    row = db.get_content(content_id)
    if row is None:
        await update.message.reply_text(f"Content {content_id} not found.")
        return

    caption = " ".join(context.args[1:]) if len(context.args) > 1 else ""

    if row["is_ppv"]:
        await _post_ppv_teaser(context, row, caption_override=caption)
    else:
        await _post_free(context, row, caption_override=caption)

    db.mark_posted(content_id)
    await update.message.reply_text(f"✅ Posted content {content_id}.")


async def cmd_ppv(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /ppv <content_id> <stars> [teaser_content_id].

    Marks content as PPV and posts a teaser to the channel.

    teaser_content_id (optional): id of another content row whose file_url
    is used as the free channel preview. If omitted, file_url of the main
    content is used as the teaser (so subscribers see a real sample).

    Args:
        update: Incoming Telegram update.
        context: PTB context with args.
    """
    if not _is_owner(update):
        await update.message.reply_text("Not authorised.")
        return

    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage: /ppv <content_id> <stars> [caption]\n"
            "Example: /ppv 12 1000 Exclusive content 🔥"
        )
        return

    try:
        content_id  = int("".join(filter(str.isdigit, context.args[0])))
        price_stars = int("".join(filter(str.isdigit, context.args[1])))
        if not content_id or not price_stars:
            raise ValueError
    except (ValueError, IndexError):
        await update.message.reply_text(
            "Usage: /ppv <content_id> <stars> [caption]\n"
            "Example: /ppv 12 1000 Exclusive content 🔥"
        )
        return

    if price_stars < 1:
        await update.message.reply_text("Stars price must be at least 1.")
        return

    rate_err = _check_rate_limit()
    if rate_err:
        await update.message.reply_text(rate_err)
        return

    row = db.get_content(content_id)
    if row is None:
        await update.message.reply_text(f"Content {content_id} not found.")
        return

    # arg[2] is teaser_id only if it's a pure number, otherwise it starts the caption
    teaser_url = ""
    caption_start = 2
    if len(context.args) >= 3 and context.args[2].isdigit():
        teaser_id  = int(context.args[2])
        teaser_row = db.get_content(teaser_id)
        if teaser_row is None:
            await update.message.reply_text(f"Teaser content {teaser_id} not found.")
            return
        teaser_url   = teaser_row["file_url"]
        caption_start = 3

    caption = " ".join(context.args[caption_start:]) if len(context.args) > caption_start else ""

    db.set_ppv(content_id, price_stars, teaser_url)
    row = db.get_content(content_id)   # re-fetch with updated values
    await _post_ppv_teaser(context, row, caption_override=caption)
    db.mark_posted(content_id)
    await update.message.reply_text(f"✅ PPV posted for content {content_id} ({price_stars} Stars).")


async def cmd_promo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /promo <creator_id> — post a creator promo GIF to the channel.

    Args:
        update: Incoming Telegram update.
        context: PTB context with args.
    """
    if not _is_owner(update):
        await update.message.reply_text("Not authorised.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /promo <creator_id>")
        return

    try:
        creator_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("creator_id must be an integer.")
        return

    rate_err = _check_rate_limit()
    if rate_err:
        await update.message.reply_text(rate_err)
        return

    creator = db.get_creator(creator_id)
    if creator is None:
        await update.message.reply_text(f"Creator {creator_id} not found.")
        return

    name         = creator["name"]
    onlyfans_url = creator["onlyfans_url"]
    gif_url      = creator["gif_url"]
    bio          = creator["bio"] or ""
    caption      = f"✨ {name}\n{bio}" if bio else f"✨ {name}"

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Visit OnlyFans", url=onlyfans_url)]
    ])

    if gif_url:
        await context.bot.send_animation(
            chat_id=CHANNEL_ID,
            animation=gif_url,
            caption=caption,
            reply_markup=keyboard,
        )
    else:
        await context.bot.send_message(
            chat_id=CHANNEL_ID,
            text=caption,
            reply_markup=keyboard,
        )

    # Record a synthetic posted_at so the rate limiter counts this post
    db.record_promo_post()
    await update.message.reply_text(f"Promo posted for {name}.")


async def cmd_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /schedule — show unposted content queue and next allowed post time.

    Args:
        update: Incoming Telegram update.
        context: PTB context.
    """
    if not _is_owner(update):
        await update.message.reply_text("Not authorised.")
        return

    rows = db.get_unposted_content(limit=20)

    last = db.get_last_posted_at()
    if last:
        last_dt   = datetime.fromisoformat(last).replace(tzinfo=timezone.utc)
        next_ok   = last_dt + timedelta(minutes=MIN_POST_GAP_MINUTES)
        now       = datetime.now(tz=timezone.utc)
        wait_secs = max(0, int((next_ok - now).total_seconds()))
        if wait_secs > 0:
            wait_str = f"{wait_secs // 60} min {wait_secs % 60} s"
            timing = f"Next post allowed in: {wait_str}"
        else:
            timing = "Next post: ready now"
    else:
        timing = "Next post: ready now"

    if not rows:
        await update.message.reply_text(f"Queue empty.\n{timing}")
        return

    lines = [f"📋 Unposted queue ({len(rows)} items):", timing, ""]
    for row in rows:
        ppv_tag = f" 🔒 {row['ppv_price_stars']}⭐" if row["is_ppv"] else ""
        teaser  = " [has teaser]" if row["teaser_url"] else ""
        lines.append(f"  ID {row['id']}  [{row['file_type']}]{ppv_tag}{teaser}  {row['uploaded_at']}")

    await update.message.reply_text("\n".join(lines))


# ---------------------------------------------------------------------------
# Inline button — PPV unlock
# ---------------------------------------------------------------------------

async def handle_ppv_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle "Unlock for X Stars" button taps — send a Stars invoice via DM.

    Args:
        update: Incoming callback query update.
        context: PTB context.
    """
    query = update.callback_query
    await query.answer()

    content_id = int(query.data.split(":")[1])
    user       = query.from_user

    if db.has_purchased(user.id, content_id):
        await context.bot.send_message(
            chat_id=user.id,
            text="You have already unlocked this — check your earlier DM from me.",
        )
        return

    row = db.get_content(content_id)
    if row is None or not row["is_ppv"]:
        await query.answer("Content not available.", show_alert=True)
        return

    price_stars = row["ppv_price_stars"]
    caption     = row["caption"] or "Exclusive content"

    await context.bot.send_invoice(
        chat_id=user.id,
        title=f"Unlock: {caption[:32]}",
        description=(
            f"Pay {price_stars} Stars to unlock the full version. "
            "Delivered instantly to this chat."
        ),
        payload=f"ppv:{content_id}",
        currency="XTR",
        prices=[LabeledPrice("Unlock full content", price_stars)],
    )


# ---------------------------------------------------------------------------
# Payment handlers
# ---------------------------------------------------------------------------

async def pre_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Validate a Stars payment — must respond within 10 seconds.

    Args:
        update: Incoming pre-checkout query.
        context: PTB context.
    """
    query = update.pre_checkout_query

    if not query.invoice_payload.startswith("ppv:"):
        await query.answer(ok=False, error_message="Invalid payment.")
        return

    content_id = int(query.invoice_payload.split(":")[1])
    row = db.get_content(content_id)

    if row is None or not row["is_ppv"]:
        await query.answer(ok=False, error_message="Content no longer available.")
        return

    await query.answer(ok=True)


async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Record a completed purchase and DM the full content to the buyer.

    Args:
        update: Message containing successful_payment data.
        context: PTB context.
    """
    payment    = update.message.successful_payment
    user       = update.effective_user
    content_id = int(payment.invoice_payload.split(":")[1])
    stars_paid = payment.total_amount

    db.record_purchase(user.id, content_id, stars_paid)

    row = db.get_content(content_id)
    if row is None:
        await update.message.reply_text(
            "Payment received! Content not found — please contact support."
        )
        return

    await update.message.reply_text(
        "Payment received! Here is your full content:"
    )

    await _send_media(
        context,
        chat_id=user.id,
        file_url=row["file_url"],
        file_type=row["file_type"],
        caption=row["caption"] or "",
    )

    logger.info(
        "Delivered content %d to user %d (%d Stars)", content_id, user.id, stars_paid
    )


# =============================================================================
# AUTO-SCHEDULER — caption cache, Grok helpers, job functions, commands
# =============================================================================

# ---------------------------------------------------------------------------
# Caption cache (shared read/write with XAutoPosting's posted_archive.json)
# ---------------------------------------------------------------------------

_archive_data: list | None = None   # in-process cache; reloaded on each bot start


def _load_archive() -> list:
    """Load (and cache in-process) the XAutoPosting posted_archive.json.

    Returns an empty list if the file doesn't exist or can't be parsed.
    """
    global _archive_data
    if _archive_data is not None:
        return _archive_data
    if XAUTOPOST_ARCHIVE.exists():
        try:
            with XAUTOPOST_ARCHIVE.open(encoding="utf-8") as fh:
                _archive_data = json.load(fh)
        except Exception as exc:
            logger.warning("Could not load XAutoPosting archive: %s", exc)
            _archive_data = []
    else:
        _archive_data = []
    return _archive_data


def _clean_x_caption(caption: str) -> str:
    """Strip X/Twitter-specific call-to-action trailers from an archived caption.

    XAutoPosting captions often end with phrases like "more free girls 👉" or
    "more girls 👉" which were paired with a link appended separately at post time.
    Those trailers look broken on Telegram where the link isn't appended.

    Args:
        caption: Raw caption from posted_archive.json.

    Returns:
        Cleaned caption with the X-specific trailer removed.
    """
    import re
    # Remove trailing "more free girls 👉", "more girls 👉", "more 👉" etc.
    caption = re.sub(r'\s*more\s+(free\s+)?girls?\s*👉\s*$', '', caption, flags=re.IGNORECASE).strip()
    # Remove trailing lone arrow emoji
    caption = re.sub(r'\s*👉\s*$', '', caption).strip()
    return caption


def _cached_caption_by_path(file_path: str) -> str:
    """Return the cached caption for a local file_path, or empty string.

    Matches against the 'file_path' field in posted_archive.json entries.

    Args:
        file_path: Absolute local path used in XAutoPosting.
    """
    for entry in _load_archive():
        if entry.get("file_path") == file_path and entry.get("caption"):
            return entry["caption"]
    return ""


def _cached_caption_by_link(link: str) -> str:
    """Return the cached caption for a creator trial link, or empty string.

    Matches against the 'link' field in posted_archive.json entries.

    Args:
        link: OnlyFans trial link used in XAutoPosting.
    """
    for entry in _load_archive():
        if entry.get("link") == link and entry.get("caption"):
            return entry["caption"]
    return ""


def _cached_caption_by_slug(slug: str) -> str:
    """Return a cached caption for a GG creator by matching slug in file_path.

    GG entries in posted_archive.json have an empty link field but their
    file_path contains the creator slug (e.g. /GG/gifs/goodgirlhana/...).

    Args:
        slug: Creator slug, e.g. 'goodgirlhana'.
    """
    for entry in _load_archive():
        fp = entry.get("file_path", "")
        if slug in fp and "/GG/" in fp and entry.get("caption"):
            return entry["caption"]
    return ""


def _save_to_archive(file_path: str, caption: str, link: str = "") -> None:
    """Append a new caption to the archive so future runs can reuse it.

    Args:
        file_path: Local file path (may be empty for promo entries).
        caption: The generated caption text.
        link: Creator link (for promo entries).
    """
    archive = _load_archive()
    archive.append({
        "caption": caption,
        "fixed_suffix": None,
        "link": link,
        "file_path": file_path,
        "scheduled": datetime.utcnow().isoformat(),
        "account": 0,
        "posted": True,
        "tweet_id": "",
        "posted_at": datetime.utcnow().isoformat(),
        "posted_by": "TelegramBot",
    })
    try:
        with XAUTOPOST_ARCHIVE.open("w", encoding="utf-8") as fh:
            json.dump(archive, fh, ensure_ascii=False)
    except Exception as exc:
        logger.warning("Could not write to XAutoPosting archive: %s", exc)


# ---------------------------------------------------------------------------
# Grok API helpers (blocking → wrap with asyncio.to_thread)
# ---------------------------------------------------------------------------

def _grok_vision_sync(image_path: Path, prompt: str, temperature: float = 0.1) -> str:
    """Call the Grok vision API with a local JPEG image (blocking).

    Args:
        image_path: Path to the local JPEG file.
        prompt: Instruction text sent alongside the image.
        temperature: Sampling temperature (0.1 for deterministic tags).

    Returns:
        Response text, or empty string on any error.
    """
    import requests as _req
    if not GROK_API_KEY:
        return ""
    with image_path.open("rb") as fh:
        b64 = base64.b64encode(fh.read()).decode()
    payload = {
        "model": "grok-2-vision-latest",
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            {"type": "text", "text": prompt},
        ]}],
        "temperature": temperature,
        "max_tokens": 300,
    }
    resp = _req.post(
        GROK_API_URL, json=payload, timeout=60,
        headers={"Authorization": f"Bearer {GROK_API_KEY}", "Content-Type": "application/json"},
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


def _grok_text_sync(prompt: str, temperature: float = 0.9) -> str:
    """Call the Grok text API (blocking).

    Args:
        prompt: User message text.
        temperature: Sampling temperature.

    Returns:
        Response text, or empty string on any error.
    """
    import requests as _req
    if not GROK_API_KEY:
        return ""
    payload = {
        "model": "grok-4-fast",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": 200,
    }
    resp = _req.post(
        GROK_API_URL, json=payload, timeout=60,
        headers={"Authorization": f"Bearer {GROK_API_KEY}", "Content-Type": "application/json"},
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


async def _tag_frame(image_path: Path) -> str:
    """Tag an adult video frame using Grok vision — runs in thread.

    Args:
        image_path: Path to the extracted JPEG frame.

    Returns:
        Comma-separated explicit tags, or empty string.
    """
    prompt = (
        "You are tagging adult content for a private creator platform. "
        "List 5-8 short, explicit, graphic tags describing exactly what you see. "
        "Be direct and specific. Output only the tags, comma-separated, no other text."
    )
    return await asyncio.to_thread(_grok_vision_sync, image_path, prompt, 0.1)


async def _generate_ppv_teaser() -> str:
    """Generate a unique filthy PPV teaser by rephrasing a fixed expression.

    No vision or frame analysis — Grok simply rewrites the base expression in
    the most vulgar and explicit way it can each time.  Uses grok-4-fast at
    temperature 0.9 (same settings as Acaption).

    Returns:
        Two-line teaser string (teaser sentence + unlock line), or static
        fallback on API failure.
    """
    prompt = (
        "Rewrite the following adult content teaser in the most filthy, vulgar, and explicit "
        "way possible. Keep it to 1-2 sentences maximum. Output only the rewritten text — "
        "no quotes, no intro, no explanation.\n\n"
        "Original: \"Exclusive content — this one is absolutely filthy 🔥 unlock & enjoy\""
    )
    result = await asyncio.to_thread(_grok_text_sync, prompt, 0.9)
    return result if result else "Exclusive content — this one is absolutely filthy 🔥 unlock & enjoy"


async def _generate_free_caption(context_hint: str = "") -> str:
    """Generate a short teasing caption for a free post.

    Args:
        context_hint: Optional hint (e.g. filename) to guide the model.

    Returns:
        1-2 sentence caption, or empty string.
    """
    prompt = (
        "Write a short, teasing, explicit social media caption (1-2 sentences max) "
        "for an adult content creator sharing a photo or video. "
        "Be playful and enticing. No hashtags. No quotes. Output only the caption."
    )
    if context_hint:
        prompt += f" Context hint: {context_hint}"
    return await asyncio.to_thread(_grok_text_sync, prompt, 0.9)


async def _generate_promo_caption(creator_display: str) -> str:
    """Generate a short promo caption for a GG creator.

    Args:
        creator_display: Human-readable creator name/slug.

    Returns:
        1-2 sentence promo caption, or empty string.
    """
    prompt = (
        f"Write a short, sexy social media caption (1-2 sentences) promoting "
        f"'{creator_display}' on OnlyFans. Be suggestive but not explicit. "
        f"No hashtags. No quotes. Output only the caption."
    )
    return await asyncio.to_thread(_grok_text_sync, prompt, 0.9)


# ---------------------------------------------------------------------------
# Local frame extraction (for PPV tagging)
# ---------------------------------------------------------------------------

def _extract_frame_local(video_path: Path) -> Path | None:
    """Extract a single JPEG frame at 3 s from a local video using ffmpeg.

    Args:
        video_path: Path to the local video file.

    Returns:
        Path to a temp JPEG file (caller must delete), or None on failure.
    """
    if not shutil.which("ffmpeg"):
        return None
    tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
    tmp.close()
    result = subprocess.run(
        ["ffmpeg", "-y", "-ss", "3", "-i", str(video_path),
         "-frames:v", "1", "-q:v", "3", tmp.name],
        capture_output=True, timeout=30,
    )
    if result.returncode != 0 or not Path(tmp.name).stat().st_size:
        Path(tmp.name).unlink(missing_ok=True)
        return None
    return Path(tmp.name)


# ---------------------------------------------------------------------------
# Video compression (for oversized PPV uploads — blocking)
# ---------------------------------------------------------------------------

def _compress_video_for_upload(video_path: Path) -> Path | None:
    """Compress a video to fit within Telegram's 45 MB upload budget (blocking).

    Also corrects landscape orientation to portrait using ffprobe metadata.
    Tries 720p CRF 28 first, then 480p CRF 30 if still too large.

    Args:
        video_path: Path to the source video.

    Returns:
        Path to a temp compressed MP4 (caller must delete), or None on failure.
    """
    tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
    tmp.close()

    # Detect orientation — apply transpose filter if truly landscape
    vw, vh, rot = _get_video_orientation(video_path)
    # After applying rotation metadata, effective dimensions are:
    if rot in (90, 270):
        vw, vh = vh, vw   # metadata will rotate it, so effective is swapped
    needs_rotate = vw > 0 and vh > 0 and vw > vh

    for scale, crf in [("1280:720", "28"), ("854:480", "30")]:
        short_side = scale.split(":")[1]  # e.g. "720"
        if needs_rotate:
            # Rotate 90° CW then scale so the long side fits
            vf = f"transpose=1,scale='-2:min(ih,{short_side})'"
        else:
            vf = f"scale='min(iw,{scale.split(':')[0]})':'-2'"

        result = subprocess.run(
            [
                "ffmpeg", "-y", "-i", str(video_path),
                "-vf", vf,
                "-c:v", "libx264", "-crf", crf, "-preset", "fast",
                "-c:a", "aac", "-b:a", "128k", "-movflags", "faststart",
                tmp.name,
            ],
            capture_output=True, timeout=1800,
        )
        if result.returncode != 0:
            logger.error("Video compression failed at %s CRF%s: %s",
                         scale, crf, result.stderr.decode()[:200])
            Path(tmp.name).unlink(missing_ok=True)
            return None
        size = Path(tmp.name).stat().st_size
        logger.info("Compressed at %s CRF%s%s → %.1f MB",
                    scale, crf, " [rotated]" if needs_rotate else "", size / 1024 / 1024)
        if size <= 45 * 1024 * 1024:
            return Path(tmp.name)
        logger.info("Still %.1f MB — retrying at lower quality…", size / 1024 / 1024)
    logger.warning("Could not compress under 45 MB — uploading anyway")
    return Path(tmp.name)


# ---------------------------------------------------------------------------
# PPV video pool helpers
# ---------------------------------------------------------------------------

def _scan_ppv_pool() -> list[Path]:
    """Return a sorted list of all video files in the PPV video directories.

    Covers /Volumes/All/Videos/0-1 min and /Volumes/All/Videos/1-5 min.
    """
    vids: list[Path] = []
    for d in PPV_VIDEO_DIRS:
        if d.exists():
            for f in sorted(d.iterdir()):
                if f.is_file() and f.suffix.lower() in VIDEO_EXTS:
                    vids.append(f)
    return vids


# ---------------------------------------------------------------------------
# Auto-scheduler enabled check
# ---------------------------------------------------------------------------

def _auto_enabled() -> bool:
    """Return True if the auto-scheduler is currently active."""
    return db.get_config("auto_enabled", "0") == "1"


# ---------------------------------------------------------------------------
# Own-content archive pool (XAutoPosting entries that are NOT GG girls)
# ---------------------------------------------------------------------------

def _get_own_archive_entries() -> list[dict]:
    """Return archive entries for KB's own content (non-GG, caption present, file exists).

    Excludes any entry whose file_path contains '/GG/' so GG promo posts are
    never double-counted here.  Also excludes entries whose local file no longer
    exists on disk.
    """
    return [
        e for e in _load_archive()
        if e.get("file_path")
        and "/GG/" not in e["file_path"]
        and e.get("caption")
        and Path(e["file_path"]).exists()
    ]


# ---------------------------------------------------------------------------
# Job: free post (:00)
# ---------------------------------------------------------------------------

async def job_free_post(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Post the next own-content entry from the XAutoPosting archive.

    Media + caption are both sourced from posted_archive.json — zero Grok cost.
    The file is uploaded to R2 on first use and tracked in the DB by source_path
    so it is never uploaded twice.

    Falls back to the DB unposted queue if the archive has no more entries.
    """
    if not _auto_enabled():
        return

    entries = _get_own_archive_entries()
    if not entries:
        logger.warning("Auto free: no own-content entries found in archive")
        return

    idx = int(db.get_config("free_archive_index", "0")) % len(entries)
    entry = entries[idx]
    db.set_config("free_archive_index", str((idx + 1) % len(entries)))

    file_path = Path(entry["file_path"])
    caption   = _clean_x_caption(entry["caption"])
    if caption:
        caption = f"{caption}\n\n{FREE_POST_SUFFIX}"
    else:
        caption = FREE_POST_SUFFIX
    source    = str(file_path)

    # Determine file type
    ext       = file_path.suffix.lower()
    file_type = r2.detect_file_type(file_path.name)
    if file_type == "unknown":
        logger.info("Auto free: unsupported extension %s — skipping %s", ext, file_path.name)
        return

    # Check if already in DB / R2
    existing = db.get_content_by_source_path(source)
    if existing:
        content_id = existing["id"]
        file_url   = existing["file_url"]
        logger.info("Auto free: reusing existing content_id=%d", content_id)
    else:
        # Compress/convert oversized files before uploading
        upload_path    = file_path
        tmp_compressed: Path | None = None
        size_bytes     = file_path.stat().st_size

        if file_type == "video" and size_bytes > 45 * 1024 * 1024:
            logger.info("Auto free: compressing video %s (%.1f MB)",
                        file_path.name, size_bytes / 1024 / 1024)
            tmp_compressed = await asyncio.to_thread(_compress_video_for_upload, file_path)
            if tmp_compressed:
                upload_path = tmp_compressed

        elif file_type == "gif" and size_bytes > TELEGRAM_UPLOAD_LIMIT:
            logger.info("Auto free: converting GIF %s (%.1f MB) to MP4",
                        file_path.name, size_bytes / 1024 / 1024)
            def _gif_to_mp4_free(src: Path) -> Path | None:
                tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
                tmp.close()
                res = subprocess.run(
                    ["ffmpeg", "-y", "-i", str(src),
                     "-movflags", "faststart", "-pix_fmt", "yuv420p",
                     "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
                     tmp.name],
                    capture_output=True, timeout=300,
                )
                if res.returncode != 0:
                    Path(tmp.name).unlink(missing_ok=True)
                    return None
                return Path(tmp.name)
            tmp_compressed = await asyncio.to_thread(_gif_to_mp4_free, file_path)
            if tmp_compressed:
                upload_path = tmp_compressed
                file_type   = "gif"   # sendAnimation handles MP4

        folder      = "my-content"
        safe_name   = file_path.name.replace(" ", "_")
        safe_stem   = file_path.stem.replace(" ", "_")
        object_key  = f"{folder}/{safe_stem}.mp4" if tmp_compressed else f"{folder}/{safe_name}"
        r2_ok = False
        try:
            if r2.object_exists(object_key):
                file_url = r2.public_url(object_key)
                r2_ok = True
            else:
                logger.info("Auto free: uploading %s (%.1f MB)",
                            upload_path.name, upload_path.stat().st_size / 1024 / 1024)
                file_url = await asyncio.to_thread(r2.upload_file, str(upload_path), object_key)
                r2_ok = True
        except Exception as exc:
            logger.warning("Auto free: R2 upload failed for %s: %s — will send directly", file_path.name, exc)
            file_url = ""   # will send from local bytes below
        finally:
            if tmp_compressed and r2_ok:
                tmp_compressed.unlink(missing_ok=True)

        if r2_ok:
            content_id = db.insert_content(file_url=file_url, file_type=file_type, caption=caption)
            db.set_content_source_path(content_id, source)
        else:
            # R2 down — send local file directly as bytes (no DB record, will retry upload next time)
            content_id = None

    try:
        if content_id is not None:
            # Normal path — send from R2 URL
            fake_row = {"id": content_id, "file_url": file_url, "file_type": file_type,
                        "caption": caption, "is_ppv": False, "teaser_url": ""}
            await _post_free(context, fake_row, caption_override=caption)
            db.mark_posted(content_id)
            logger.info("Auto free: posted content_id=%d  file=%s", content_id, file_path.name)
        else:
            # R2 down — send local bytes directly, don't mark as posted (retry upload next cycle)
            import io as _io
            send_path = tmp_compressed if (tmp_compressed and tmp_compressed.exists()) else upload_path
            with send_path.open("rb") as fh:
                raw = fh.read()
            if file_type == "image":
                raw = _compress_image_bytes(raw)
            from telegram import InputFile as _IF
            media = _IF(_io.BytesIO(raw), filename=send_path.name)
            kwargs = dict(caption=caption)
            if file_type == "image":
                await context.bot.send_photo(chat_id=CHANNEL_ID, photo=media, **kwargs)
            elif file_type == "video":
                await context.bot.send_video(chat_id=CHANNEL_ID, video=media, **kwargs)
            else:
                await context.bot.send_animation(chat_id=CHANNEL_ID, animation=media, **kwargs)
            if tmp_compressed and tmp_compressed.exists():
                tmp_compressed.unlink(missing_ok=True)
            logger.info("Auto free: posted %s directly (R2 bypassed)", file_path.name)
            # Advance the archive index so next cycle picks a different file
    except Exception as exc:
        logger.error("Auto free: post failed for %s: %s", file_path.name, exc)
        if tmp_compressed and tmp_compressed.exists():
            tmp_compressed.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Job: promo girl (:15 and :45)
# ---------------------------------------------------------------------------

async def job_promo_girl(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Post the next GG creator promo to the channel (rotates through GG_CREATORS).

    Caption source priority: XAutoPosting archive (matched by trial link) →
    Grok → static fallback.  GIF is uploaded to R2 on first use.
    """
    if not _auto_enabled():
        return

    if not GG_CREATORS:
        logger.warning("Auto promo: GG_CREATORS list is empty")
        return

    idx = int(db.get_config("gg_creator_index", "0")) % len(GG_CREATORS)
    slug, trial_link = GG_CREATORS[idx]
    db.set_config("gg_creator_index", str((idx + 1) % len(GG_CREATORS)))

    display_name = slug.replace("-", " ").title()

    # Caption: archive by slug (GG entries have empty link) → archive by link → Grok → default
    caption = _clean_x_caption(
        _cached_caption_by_slug(slug) or _cached_caption_by_link(trial_link)
    )
    if not caption and GROK_API_KEY:
        try:
            caption = await _generate_promo_caption(display_name)
            if caption:
                _save_to_archive("", caption, link=trial_link)
        except Exception as exc:
            logger.warning("Auto promo: Grok caption failed for %s: %s", slug, exc)
    if not caption:
        caption = f"✨ {display_name} is waiting for you on OnlyFans!"

    full_caption = f"{caption}\n💋 {trial_link} 💋"

    # Find and (if needed) upload the creator's GIF to R2
    # GIFs > 50 MB are converted to MP4 first so Telegram can receive them as bytes
    gif_url  = ""
    gif_type = "gif"
    gif_local = GG_GIFS_BASE / slug / f"{slug}.gif"
    if gif_local.exists():
        upload_path = gif_local
        r2_key      = f"promo-gifs/{slug}/{slug}.gif"
        tmp_mp4: Path | None = None

        if gif_local.stat().st_size > TELEGRAM_UPLOAD_LIMIT:
            logger.info("Auto promo: GIF for %s is %.1f MB — converting to MP4",
                        slug, gif_local.stat().st_size / 1024 / 1024)
            def _gif_to_mp4(src: Path) -> Path | None:
                tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
                tmp.close()
                res = subprocess.run(
                    ["ffmpeg", "-y", "-i", str(src),
                     "-movflags", "faststart", "-pix_fmt", "yuv420p",
                     "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
                     tmp.name],
                    capture_output=True, timeout=300,
                )
                if res.returncode != 0:
                    Path(tmp.name).unlink(missing_ok=True)
                    return None
                return Path(tmp.name)

            tmp_mp4 = await asyncio.to_thread(_gif_to_mp4, gif_local)
            if tmp_mp4:
                upload_path = tmp_mp4
                r2_key      = f"promo-gifs/{slug}/{slug}.mp4"
                gif_type    = "gif"   # sendAnimation handles MP4 fine

        local_send_path: Path | None = None
        try:
            if r2.object_exists(r2_key):
                gif_url = r2.public_url(r2_key)
            else:
                gif_url = await asyncio.to_thread(r2.upload_file, str(upload_path), r2_key)
                logger.info("Auto promo: uploaded %s for %s to R2 (%.1f MB)",
                            upload_path.suffix, slug, upload_path.stat().st_size / 1024 / 1024)
        except Exception as exc:
            logger.warning("Auto promo: R2 upload failed for %s: %s — sending directly", slug, exc)
            local_send_path = upload_path  # bypass R2, send local bytes
        finally:
            if tmp_mp4 and gif_url:
                tmp_mp4.unlink(missing_ok=True)
    else:
        logger.info("Auto promo: no GIF found for %s at %s", slug, gif_local)

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("💋 Visit OnlyFans (free trial)", url=trial_link)
    ]])

    if not gif_url and not local_send_path:
        logger.warning("Auto promo: skipping %s — no GIF available", slug)
        db.set_config("gg_creator_index", str(idx))
        return

    try:
        if gif_url:
            await _send_media(context, CHANNEL_ID, gif_url, "gif",
                              caption=full_caption, reply_markup=keyboard)
        else:
            import io as _io
            with local_send_path.open("rb") as fh:
                raw = fh.read()
            from telegram import InputFile as _IF
            media = _IF(_io.BytesIO(raw), filename=local_send_path.name)
            await context.bot.send_animation(
                chat_id=CHANNEL_ID, animation=media,
                caption=full_caption, reply_markup=keyboard,
            )
            if tmp_mp4 and tmp_mp4.exists():
                tmp_mp4.unlink(missing_ok=True)
            logger.info("Auto promo: sent %s directly (R2 bypassed)", slug)
        db.record_promo_post()
        logger.info("Auto promo: posted %s (idx=%d)", slug, idx)
    except Exception as exc:
        logger.error("Auto promo: post failed for %s: %s", slug, exc)
        if tmp_mp4 and tmp_mp4.exists():
            tmp_mp4.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Job: PPV video (:30)
# ---------------------------------------------------------------------------

async def job_ppv_post(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Pick the next video from the PPV pool, upload to R2 if needed, post teaser.

    Teaser is always text-only (never exposes the video before purchase).
    Caption generated via Grok frame-tagging; falls back to static template.
    """
    if not _auto_enabled():
        return

    pool = _scan_ppv_pool()
    if not pool:
        logger.warning("Auto PPV: no videos found in PPV pool directories")
        return

    # Rotate through pool
    idx = int(db.get_config("ppv_video_index", "0")) % len(pool)
    video_path = pool[idx]
    db.set_config("ppv_video_index", str((idx + 1) % len(pool)))

    price_stars = int(db.get_config("ppv_price_stars", str(DEFAULT_PPV_PRICE)))

    # Ensure video is in R2 and DB
    existing = db.get_content_by_source_path(str(video_path))
    if existing is None:
        logger.info("Auto PPV: uploading %s (%.1f MB)",
                    video_path.name, video_path.stat().st_size / 1024 / 1024)

        upload_path = video_path
        tmp_compressed: Path | None = None
        if video_path.stat().st_size > 45 * 1024 * 1024:
            logger.info("Auto PPV: compressing %s before upload", video_path.name)
            tmp_compressed = await asyncio.to_thread(
                _compress_video_for_upload, video_path
            )
            if tmp_compressed:
                upload_path = tmp_compressed

        object_key = f"my-content/{video_path.stem}.mp4"
        try:
            if r2.object_exists(object_key):
                file_url = r2.public_url(object_key)
            else:
                file_url = await asyncio.to_thread(
                    r2.upload_file, str(upload_path), object_key
                )
            content_id = db.insert_content(
                file_url=file_url,
                file_type="video",
                caption="",
                teaser_url="",
            )
            db.set_content_source_path(content_id, str(video_path))
            db.set_ppv(content_id, price_stars)
            logger.info("Auto PPV: content_id=%d uploaded to R2", content_id)
        except Exception as exc:
            logger.error("Auto PPV: upload failed for %s: %s", video_path.name, exc)
            return
        finally:
            if tmp_compressed:
                tmp_compressed.unlink(missing_ok=True)
    else:
        content_id = existing["id"]
        if not existing["is_ppv"]:
            db.set_ppv(content_id, price_stars)
        logger.info("Auto PPV: reusing existing content_id=%d", content_id)

    # Generate unique filthy teaser via Grok (no vision — pure text rephrasing)
    teaser_text = ""
    if GROK_API_KEY:
        try:
            teaser_text = await _generate_ppv_teaser()
        except Exception as exc:
            logger.warning("Auto PPV: Grok teaser failed: %s", exc)

    if not teaser_text:
        teaser_text = "Exclusive content — this one is absolutely filthy 🔥 unlock & enjoy"

    full_caption = f"💋 {teaser_text} 💋"

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            f"🔓 Unlock for {price_stars} Stars",
            callback_data=f"ppv:{content_id}",
        )
    ]])

    try:
        await context.bot.send_message(
            chat_id=CHANNEL_ID, text=full_caption, reply_markup=keyboard,
        )
        db.mark_posted(content_id)
        logger.info("Auto PPV: posted content_id=%d  video=%s", content_id, video_path.name)
    except Exception as exc:
        logger.error("Auto PPV: send_message failed: %s", exc)


# ---------------------------------------------------------------------------
# Job: daily R2 cleanup (runs once a day)
# ---------------------------------------------------------------------------

async def job_r2_cleanup(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Delete R2 objects for content that was posted >7 days ago with no purchases.

    Thumbnails stored under my-content/thumbs/ are also deleted when the parent
    content row is cleaned up.
    """
    stale = db.get_stale_content_for_cleanup(days=7)
    deleted = 0
    for row in stale:
        url = row["file_url"] or ""
        if not url:
            continue
        # Derive R2 object key from the public URL: last two path segments (folder/file)
        parts = [p for p in url.split("/") if p]
        if len(parts) >= 2:
            object_key = f"{parts[-2]}/{parts[-1]}"
        else:
            object_key = parts[-1] if parts else ""
        if not object_key:
            continue
        try:
            await asyncio.to_thread(r2.delete_file, object_key)
            logger.info("R2 cleanup: deleted %s", object_key)
            deleted += 1
        except Exception as exc:
            logger.warning("R2 cleanup: could not delete %s: %s", object_key, exc)

        # Delete thumbnail if present
        teaser_url = row.get("teaser_url") or ""
        if teaser_url:
            t_parts = [p for p in teaser_url.split("/") if p]
            if len(t_parts) >= 3:
                thumb_key = f"{t_parts[-3]}/{t_parts[-2]}/{t_parts[-1]}"
            elif len(t_parts) >= 2:
                thumb_key = f"{t_parts[-2]}/{t_parts[-1]}"
            else:
                thumb_key = ""
            if thumb_key:
                try:
                    await asyncio.to_thread(r2.delete_file, thumb_key)
                except Exception:
                    pass

    logger.info("R2 cleanup complete: %d objects deleted", deleted)


# ---------------------------------------------------------------------------
# Auto-scheduler commands
# ---------------------------------------------------------------------------

async def cmd_autostart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /autostart — start the 4-slot repeating scheduler.

    Removes any existing auto jobs before registering fresh ones so calling
    /autostart twice is safe.

    Args:
        update: Incoming Telegram update.
        context: PTB context with job_queue.
    """
    if not _is_owner(update):
        await update.message.reply_text("Not authorised.")
        return

    # Cancel any running auto jobs
    for name in AUTO_JOB_NAMES:
        for job in context.job_queue.get_jobs_by_name(name):
            job.schedule_removal()

    gap   = SLOT_GAP_SECONDS
    cycle = gap * 4

    context.job_queue.run_repeating(
        job_free_post,  interval=cycle, first=1,       name="auto_free",    chat_id=0,
    )
    context.job_queue.run_repeating(
        job_promo_girl, interval=cycle, first=gap,     name="auto_promo_1", chat_id=0,
    )
    context.job_queue.run_repeating(
        job_ppv_post,   interval=cycle, first=gap * 2, name="auto_ppv",     chat_id=0,
    )
    context.job_queue.run_repeating(
        job_promo_girl, interval=cycle, first=gap * 3, name="auto_promo_2", chat_id=0,
    )

    db.set_config("auto_enabled", "1")

    cycle_desc = f"{cycle // 60} min" if cycle >= 60 else f"{cycle} s"
    gap_desc   = f"{gap // 60} min"   if gap   >= 60 else f"{gap} s"
    price      = db.get_config("ppv_price_stars", str(DEFAULT_PPV_PRICE))
    gg_idx     = db.get_config("gg_creator_index", "0")
    ppv_idx    = db.get_config("ppv_video_index", "0")

    await update.message.reply_text(
        f"✅ Auto-scheduler started.\n"
        f"Cycle: {cycle_desc}  |  Slot gap: {gap_desc}\n"
        f"PPV price: {price} ⭐\n"
        f"GG creator index: {gg_idx} / {len(GG_CREATORS)}\n"
        f"PPV video index: {ppv_idx} / {len(_scan_ppv_pool())}\n\n"
        f"Slots:\n"
        f"  +0 s   → free post\n"
        f"  +{gap_desc} → promo girl\n"
        f"  +{gap_desc}×2 → PPV video\n"
        f"  +{gap_desc}×3 → promo girl"
    )


async def cmd_autostop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /autostop — stop all running auto-scheduler jobs.

    Args:
        update: Incoming Telegram update.
        context: PTB context with job_queue.
    """
    if not _is_owner(update):
        await update.message.reply_text("Not authorised.")
        return

    removed = 0
    for name in AUTO_JOB_NAMES:
        for job in context.job_queue.get_jobs_by_name(name):
            job.schedule_removal()
            removed += 1

    db.set_config("auto_enabled", "0")
    await update.message.reply_text(
        f"⏹ Auto-scheduler stopped ({removed} job(s) removed)."
    )


async def cmd_autostatus(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /autostatus — show scheduler status, config, and pool sizes.

    Args:
        update: Incoming Telegram update.
        context: PTB context with job_queue.
    """
    if not _is_owner(update):
        await update.message.reply_text("Not authorised.")
        return

    enabled   = db.get_config("auto_enabled", "0") == "1"
    price     = db.get_config("ppv_price_stars", str(DEFAULT_PPV_PRICE))
    gg_idx    = int(db.get_config("gg_creator_index", "0")) % len(GG_CREATORS)
    ppv_pool  = _scan_ppv_pool()
    ppv_idx   = int(db.get_config("ppv_video_index", "0")) % max(len(ppv_pool), 1)
    free_rows = db.get_unposted_content(limit=50)
    free_count = sum(1 for r in free_rows if not r["is_ppv"])

    running_jobs = [
        name for name in AUTO_JOB_NAMES
        if context.job_queue.get_jobs_by_name(name)
    ]

    gap        = SLOT_GAP_SECONDS
    cycle      = gap * 4
    cycle_desc = f"{cycle // 60} min" if cycle >= 60 else f"{cycle} s"
    gap_desc   = f"{gap // 60} min"   if gap   >= 60 else f"{gap} s"

    next_slug = GG_CREATORS[gg_idx][0] if GG_CREATORS else "—"
    next_vid  = ppv_pool[ppv_idx].name if ppv_pool else "—"

    status = "🟢 RUNNING" if enabled and running_jobs else "🔴 STOPPED"

    await update.message.reply_text(
        f"📊 Auto-scheduler status: {status}\n\n"
        f"Cycle: {cycle_desc}  |  Slot gap: {gap_desc}\n"
        f"PPV price: {price} ⭐  |  Grok: {'✅' if GROK_API_KEY else '❌ no key'}\n\n"
        f"Free content in queue: {free_count}\n"
        f"PPV video pool: {len(ppv_pool)} files\n"
        f"  Next PPV: {next_vid} (idx {ppv_idx})\n\n"
        f"GG creators: {len(GG_CREATORS)} total\n"
        f"  Next promo: {next_slug} (idx {gg_idx})\n\n"
        f"Active jobs: {', '.join(running_jobs) or 'none'}"
    )


async def cmd_setppvprice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /setppvprice <stars> — set the default PPV price for auto-posted videos.

    Args:
        update: Incoming Telegram update.
        context: PTB context with args.
    """
    if not _is_owner(update):
        await update.message.reply_text("Not authorised.")
        return

    if not context.args or not context.args[0].isdigit():
        current = db.get_config("ppv_price_stars", str(DEFAULT_PPV_PRICE))
        await update.message.reply_text(
            f"Usage: /setppvprice <stars>\n"
            f"Current price: {current} ⭐\n"
            f"Example: /setppvprice 1000"
        )
        return

    price = int(context.args[0])
    if price < 1:
        await update.message.reply_text("Price must be at least 1 Star.")
        return

    db.set_config("ppv_price_stars", str(price))
    await update.message.reply_text(f"✅ Default PPV price set to {price} ⭐.")


# ---------------------------------------------------------------------------
# Application setup
# ---------------------------------------------------------------------------

def build_app() -> Application:
    """Create and configure the PTB Application.

    Returns:
        Configured Application instance ready to run.
    """
    app = Application.builder().token(BOT_TOKEN).build()

    # Manual posting commands
    app.add_handler(CommandHandler("post",     cmd_post))
    app.add_handler(CommandHandler("ppv",      cmd_ppv))
    app.add_handler(CommandHandler("promo",    cmd_promo))
    app.add_handler(CommandHandler("schedule", cmd_schedule))

    # Auto-scheduler commands
    app.add_handler(CommandHandler("autostart",   cmd_autostart))
    app.add_handler(CommandHandler("autostop",    cmd_autostop))
    app.add_handler(CommandHandler("autostatus",  cmd_autostatus))
    app.add_handler(CommandHandler("setppvprice", cmd_setppvprice))

    # Inline button & payments
    app.add_handler(CallbackQueryHandler(handle_ppv_button, pattern=r"^ppv:\d+$"))
    app.add_handler(PreCheckoutQueryHandler(pre_checkout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))

    return app


def main() -> None:
    """Initialise the DB, register the daily R2 cleanup job, and start polling."""
    db.init_db()
    logger.info("Database initialised.")

    app = build_app()

    # Daily R2 cleanup at 03:00 UTC — runs every 24 h starting from next 3 AM
    now_utc = datetime.now(tz=timezone.utc)
    next_3am = now_utc.replace(hour=3, minute=0, second=0, microsecond=0)
    if next_3am <= now_utc:
        next_3am += timedelta(days=1)
    seconds_until_3am = (next_3am - now_utc).total_seconds()
    app.job_queue.run_repeating(
        job_r2_cleanup,
        interval=86400,            # 24 hours
        first=seconds_until_3am,
        name="r2_daily_cleanup",
        chat_id=0,
    )
    logger.info(
        "R2 cleanup job scheduled: first run in %.0f h at 03:00 UTC",
        seconds_until_3am / 3600,
    )

    # Restore auto-scheduler if it was running before restart
    if db.get_config("auto_enabled", "0") == "1":
        gap   = SLOT_GAP_SECONDS
        cycle = gap * 4
        app.job_queue.run_repeating(job_free_post,  interval=cycle, first=1,       name="auto_free",    chat_id=0)
        app.job_queue.run_repeating(job_promo_girl, interval=cycle, first=gap,     name="auto_promo_1", chat_id=0)
        app.job_queue.run_repeating(job_ppv_post,   interval=cycle, first=gap * 2, name="auto_ppv",     chat_id=0)
        app.job_queue.run_repeating(job_promo_girl, interval=cycle, first=gap * 3, name="auto_promo_2", chat_id=0)
        logger.info(
            "Auto-scheduler restored from config (gap=%ds, cycle=%ds).", gap, cycle
        )

    logger.info("Bot starting (polling)…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
