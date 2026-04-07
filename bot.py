"""bot.py — Telegram bot for the content channel.

Commands (owner-only):
  /post [content_id]                    — post a free content item to the channel
  /ppv  [content_id] [stars] [teaser_id]— mark as PPV and post teaser + unlock button
                                          teaser_id is optional: another content row
                                          whose file_url is used as the free preview.
                                          If omitted, file_url itself is the teaser.
  /promo [creator_id]                   — post a creator promo GIF to the channel
  /schedule                             — list unposted content items

Rate limiting:
  A minimum of MIN_POST_GAP_MINUTES must pass between any channel posts
  (free, PPV, and promo combined). Owner is warned if they post too soon.

PPV / teaser flow:
  1. Bot posts teaser_url (real free media) to channel with "Unlock for X Stars" button.
  2. User taps → bot sends Stars invoice via DM.
  3. pre_checkout_query → validate & approve (within 10 s).
  4. successful_payment → record purchase, DM full file_url to user.

Run:  python bot.py
"""

import logging
import os
from datetime import datetime, timedelta, timezone

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
    ConversationHandler,
    MessageHandler,
    PreCheckoutQueryHandler,
    filters,
)

# ConversationHandler states
WAITING_CAPTION = 1

import db

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

BOT_TOKEN  = os.environ["BOT_TOKEN"]
CHANNEL_ID = os.environ["CHANNEL_ID"]      # e.g. "@yourchannel" or "-100123456"

# Minimum gap between any channel post (free, PPV, promo)
MIN_POST_GAP_MINUTES = 15

_OWNER_IDS_RAW = os.getenv("OWNER_TELEGRAM_IDS", "")
OWNER_IDS: set[int] = {
    int(uid.strip()) for uid in _OWNER_IDS_RAW.split(",") if uid.strip().isdigit()
}


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

async def _send_media(context: ContextTypes.DEFAULT_TYPE, chat_id, file_url: str,
                      file_type: str, caption: str = "",
                      reply_markup=None) -> None:
    """Send a single media item to any chat.

    Args:
        context: PTB context.
        chat_id: Destination chat or channel id.
        file_url: Public R2 URL of the media.
        file_type: 'image', 'video', or 'gif'.
        caption: Optional caption text.
        reply_markup: Optional InlineKeyboardMarkup.
    """
    kwargs = dict(caption=caption, reply_markup=reply_markup)
    if file_type == "image":
        await context.bot.send_photo(chat_id=chat_id, photo=file_url, **kwargs)
    elif file_type == "video":
        await context.bot.send_video(chat_id=chat_id, video=file_url, **kwargs)
    elif file_type == "gif":
        await context.bot.send_animation(chat_id=chat_id, animation=file_url, **kwargs)
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
    teaser_url  = row["teaser_url"] or row["file_url"]
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

    await _send_media(
        context, CHANNEL_ID,
        file_url=teaser_url,
        file_type=file_type,
        caption=teaser_caption,
        reply_markup=keyboard,
    )


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

async def cmd_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle /post <content_id> [caption] — post a free content item to the channel.

    If caption text is included in the command it is used immediately.
    If omitted, the bot asks for a caption (or /skip to post without one).

    Args:
        update: Incoming Telegram update.
        context: PTB context with args.
    """
    if not _is_owner(update):
        await update.message.reply_text("Not authorised.")
        return ConversationHandler.END

    if not context.args:
        await update.message.reply_text("Usage: /post <content_id> [caption]")
        return ConversationHandler.END

    try:
        content_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("content_id must be an integer.")
        return ConversationHandler.END

    rate_err = _check_rate_limit()
    if rate_err:
        await update.message.reply_text(rate_err)
        return ConversationHandler.END

    row = db.get_content(content_id)
    if row is None:
        await update.message.reply_text(f"Content {content_id} not found.")
        return ConversationHandler.END

    # If caption provided inline, post immediately
    if len(context.args) > 1:
        caption = " ".join(context.args[1:])
        await _do_post(context, row, content_id, caption)
        await update.message.reply_text(f"✅ Posted content {content_id}.")
        return ConversationHandler.END

    # Otherwise ask for a caption
    context.user_data["pending_post_id"] = content_id
    context.user_data["pending_post_type"] = "free"
    await update.message.reply_text(
        f"📝 Add a caption for content {content_id}?\n\n"
        f"Type it now, or send /skip to post without one."
    )
    return WAITING_CAPTION


async def received_caption(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive caption text and post the pending content item.

    Args:
        update: Message containing the caption.
        context: PTB context with user_data.
    """
    caption = update.message.text.strip()
    content_id = context.user_data.get("pending_post_id")
    post_type  = context.user_data.get("pending_post_type", "free")

    if content_id is None:
        await update.message.reply_text("No pending post. Use /post <id> first.")
        return ConversationHandler.END

    row = db.get_content(content_id)
    if row is None:
        await update.message.reply_text(f"Content {content_id} not found.")
        return ConversationHandler.END

    if post_type == "ppv":
        stars = context.user_data.get("pending_ppv_stars", 50)
        db.set_ppv(content_id, stars)
        row = db.get_content(content_id)   # refresh after update
        await _post_ppv_teaser(context, row, caption_override=caption)
    else:
        await _do_post(context, row, content_id, caption)

    db.mark_posted(content_id)
    context.user_data.clear()
    await update.message.reply_text(f"✅ Posted content {content_id}.")
    return ConversationHandler.END


async def skip_caption(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Post the pending content item with no caption.

    Args:
        update: /skip command.
        context: PTB context with user_data.
    """
    content_id = context.user_data.get("pending_post_id")
    post_type  = context.user_data.get("pending_post_type", "free")

    if content_id is None:
        await update.message.reply_text("No pending post.")
        return ConversationHandler.END

    row = db.get_content(content_id)
    if row is None:
        await update.message.reply_text(f"Content {content_id} not found.")
        return ConversationHandler.END

    if post_type == "ppv":
        stars = context.user_data.get("pending_ppv_stars", 50)
        db.set_ppv(content_id, stars)
        row = db.get_content(content_id)
        await _post_ppv_teaser(context, row, caption_override="")
    else:
        await _do_post(context, row, content_id, "")

    db.mark_posted(content_id)
    context.user_data.clear()
    await update.message.reply_text(f"✅ Posted content {content_id} (no caption).")
    return ConversationHandler.END


async def _do_post(context, row: dict, content_id: int, caption: str) -> None:
    """Post a free content item to the channel with the given caption.

    Args:
        context: PTB context.
        row: Content DB row dict.
        content_id: ID for logging.
        caption: Caption string (may be empty).
    """
    if row["is_ppv"]:
        await _post_ppv_teaser(context, row, caption_override=caption)
    else:
        await _post_free(context, row, caption_override=caption)


async def cmd_ppv(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
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
        return ConversationHandler.END

    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage: /ppv <content_id> <stars> [teaser_content_id]\n\n"
            "teaser_content_id is optional — it's another content item whose "
            "URL is shown free in the channel as a preview."
        )
        return ConversationHandler.END

    try:
        content_id  = int(context.args[0])
        price_stars = int(context.args[1])
        teaser_id   = int(context.args[2]) if len(context.args) >= 3 else None
    except ValueError:
        await update.message.reply_text("Arguments must be integers.")
        return ConversationHandler.END

    if price_stars < 1:
        await update.message.reply_text("Stars price must be at least 1.")
        return ConversationHandler.END

    rate_err = _check_rate_limit()
    if rate_err:
        await update.message.reply_text(rate_err)
        return ConversationHandler.END

    row = db.get_content(content_id)
    if row is None:
        await update.message.reply_text(f"Content {content_id} not found.")
        return ConversationHandler.END

    # Resolve teaser URL
    teaser_url = ""
    if teaser_id is not None:
        teaser_row = db.get_content(teaser_id)
        if teaser_row is None:
            await update.message.reply_text(f"Teaser content {teaser_id} not found.")
            return ConversationHandler.END
        teaser_url = teaser_row["file_url"]

    db.set_ppv(content_id, price_stars, teaser_url)

    # Store pending PPV details and ask for caption
    context.user_data["pending_post_id"]    = content_id
    context.user_data["pending_post_type"]  = "ppv"
    context.user_data["pending_ppv_stars"]  = price_stars
    await update.message.reply_text(
        f"📝 Add a caption for PPV content {content_id} ({price_stars} Stars)?\n\n"
        f"Type it now, or send /skip to post without one."
    )
    return WAITING_CAPTION


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


# ---------------------------------------------------------------------------
# Application setup
# ---------------------------------------------------------------------------

def build_app() -> Application:
    """Create and configure the PTB Application.

    Returns:
        Configured Application instance ready to run.
    """
    app = Application.builder().token(BOT_TOKEN).build()

    # /post and /ppv use a conversation to optionally collect a caption
    caption_conv = ConversationHandler(
        entry_points=[
            CommandHandler("post", cmd_post),
            CommandHandler("ppv",  cmd_ppv),
        ],
        states={
            WAITING_CAPTION: [
                CommandHandler("skip", skip_caption),
                MessageHandler(filters.TEXT & ~filters.COMMAND, received_caption),
            ],
        },
        fallbacks=[CommandHandler("skip", skip_caption)],
        per_user=True,
        per_chat=True,
    )
    app.add_handler(caption_conv)

    app.add_handler(CommandHandler("promo",    cmd_promo))
    app.add_handler(CommandHandler("schedule", cmd_schedule))

    app.add_handler(CallbackQueryHandler(handle_ppv_button, pattern=r"^ppv:\d+$"))

    app.add_handler(PreCheckoutQueryHandler(pre_checkout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))

    return app


def main() -> None:
    """Initialise the DB and start the bot in polling mode."""
    db.init_db()
    logger.info("Database initialised.")
    app = build_app()
    logger.info("Bot starting (polling)…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
