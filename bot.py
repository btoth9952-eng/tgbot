python
import asyncio
import logging
import os
import signal
import sys
import traceback
from logging.handlers import RotatingFileHandler
from aiohttp import web

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, Forbidden, NetworkError, TimedOut, RetryAfter
from telegram.ext import Application, CommandHandler, ChatMemberHandler, ContextTypes

from database import (
    init_db,
    get_user,
    register_user,
    verify_channel_member,
    unverify_channel_member,  # <--- JAVÍTVA: Beimportálva a hiányzó függvény
    get_invite_count,
    milestone_already_notified,
    mark_milestone_notified,
    get_all_user_ids,
    get_all_users_with_counts,
    get_unverified_users,
)

# ─── Logging ────────────────────────────────────────────────────────────────

LOG_FILE = os.path.join(os.path.dirname(__file__), "bot.log")
_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
_console = logging.StreamHandler(sys.stdout)
_console.setFormatter(_fmt)
_file = RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
_file.setFormatter(_fmt)
logging.basicConfig(level=logging.INFO, handlers=[_console, _file])
logger = logging.getLogger(__name__)

# ─── Config ─────────────────────────────────────────────────────────────────

BOT_TOKEN   = os.environ["TELEGRAM_BOT_TOKEN"]
ADMIN_ID    = int(os.environ["TELEGRAM_ADMIN_ID"])
CHANNEL     = os.environ["TELEGRAM_CHANNEL"]            # e.g. "@mychannel"
CHANNEL_ID  = int(os.environ.get("TELEGRAM_GROUP_ID", 0))  # numeric ID fallback
HEALTH_PORT = int(os.environ.get("PORT", 8000))
MILESTONE   = 20

_shutdown = asyncio.Event()

ACTIVE_MEMBER_STATUSES = {"member", "administrator", "creator", "restricted"}
INACTIVE_MEMBER_STATUSES = {"left", "kicked", "banned"}


# ─── Helpers ────────────────────────────────────────────────────────────────

def channel_button() -> InlineKeyboardMarkup:
    url = f"https://t.me/{CHANNEL.lstrip('@')}"
    return InlineKeyboardMarkup([[InlineKeyboardButton("📢 Join Channel", url=url)]])


def _is_our_channel(chat) -> bool:
    """Check if a chat object refers to our configured channel."""
    if chat.username and chat.username.lower() == CHANNEL.lstrip("@").lower():
        return True
    if CHANNEL_ID and chat.id == CHANNEL_ID:
        return True
    return False


async def is_channel_member(bot, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=CHANNEL, user_id=user_id)
        return member.status not in INACTIVE_MEMBER_STATUSES
    except (BadRequest, Forbidden):
        return False
    except Exception as e:
        logger.warning(f"is_channel_member error for {user_id}: {e}")
        return False


async def _credit_referral(bot, user_id: int):
    """
    Mark user as channel-verified and notify their referrer.
    Called both from /start (when already in channel) and from the
    ChatMemberHandler (when they join the channel automatically).
    """
    newly_verified = await verify_channel_member(user_id)
    if not newly_verified:
        return  # already counted before

    user_row = await get_user(user_id)
    if not user_row:
        return

    referrer_id = user_row["invited_by"]
    if referrer_id and await get_user(referrer_id):
        # Notify referrer
        count = await get_invite_count(referrer_id)
        try:
            await bot.send_message(
                chat_id=referrer_id,
                text=(
                    f"🎉 Someone joined the channel using your invite link!\n"
                    f"You now have <b>{count}</b> verified invite(s)."
                ),
                parse_mode="HTML",
            )
        except (BadRequest, Forbidden):
            pass

        # Milestone check
        if count >= MILESTONE and not await milestone_already_notified(referrer_id, MILESTONE):
            await mark_milestone_notified(referrer_id, MILESTONE)
            try:
                await bot.send_message(
                    chat_id=referrer_id,
                    text=f"🏆 Congratulations! You reached <b>{MILESTONE} invites</b>!",
                    parse_mode="HTML",
                )
            except (BadRequest, Forbidden):
                pass

        # Notify new user that their referral was recorded
        try:
            invite_link = f"https://t.me/{(await bot.get_me()).username}?start={user_id}"
            await bot.send_message(
                chat_id=user_id,
                text=(
                    "✅ You've joined the channel — your referral has been counted!\n\n"
                    f"🔗 Your own invite link:\n<code>{invite_link}</code>\n\n"
                    f"Share it to earn invites toward the goal of <b>{MILESTONE}</b>."
                ),
                parse_mode="HTML",
            )
        except (BadRequest, Forbidden):
            pass


# ─── Channel join handler (auto-fires when user joins the channel) ───────────

async def on_channel_member_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Telegram calls this whenever a user's status changes in any chat.
    We filter for joins specifically in our channel.
    """
    cm = update.chat_member
    if cm is None:
        return

    if not _is_our_channel(cm.chat):
        return  # not our channel

    old_status = cm.old_chat_member.status
    new_status = cm.new_chat_member.status

    was_outside = old_status in INACTIVE_MEMBER_STATUSES
    is_now_inside = new_status in ACTIVE_MEMBER_STATUSES

    if not (was_outside and is_now_inside):
        return  # not a join event

    user = cm.new_chat_member.user
    logger.info(f"User {user.id} (@{user.username}) joined channel — checking referral.")

    # Make sure the user is in our DB (they may never have sent /start)
    await register_user(user.id, user.username or "", user.full_name)

    await _credit_referral(context.bot, user.id)


# ─── Command handlers ────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    # Parse referral arg
    referred_by = None
    if context.args:
        try:
            ref_id = int(context.args[0])
            if ref_id != user.id:
                referred_by = ref_id
        except ValueError:
            pass

    existing = await get_user(user.id)
    is_new = existing is None

    await register_user(
        user_id=user.id,
        username=user.username or "",
        full_name=user.full_name,
        invited_by=referred_by if is_new else None,
    )

    user_row = await get_user(user.id)
    saved_referrer = user_row["invited_by"] if user_row else None

    in_channel = await is_channel_member(context.bot, user.id)

    if in_channel:
        # Credit immediately if they're already in the channel
        await _credit_referral(context.bot, user.id)
    else:
        # Not in channel yet — show button and wait for ChatMemberHandler to fire
        await update.message.reply_text(
            "⚠️ To use this bot you need to join our channel first.\n\n"
            "👇 Click the button below, then come back — your referral will be "
            "credited automatically the moment you join!",
            reply_markup=channel_button(),
        )
        return

    invite_link = f"https://t.me/{context.bot.username}?start={user.id}"
    count = await get_invite_count(user.id)

    await update.message.reply_html(
        f"👋 Welcome, {user.first_name}!\n\n"
        f"🔗 Your personal invite link:\n"
        f"<code>{invite_link}</code>\n\n"
        f"Share it with friends. When they join and subscribe to the channel, "
        f"it counts as a verified invite.\n\n"
        f"📊 You have <b>{count}</b> verified invite(s) so far.\n"
        f"🎯 Goal: <b>{MILESTONE}</b> invites"
    )


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await register_user(user.id, user.username or "", user.full_name)

    if await is_channel_member(context.bot, user.id):
        await _credit_referral(context.bot, user.id)

    count = await get_invite_count(user.id)
    remaining = max(0, MILESTONE - count)
    invite_link = f"https://t.me/{context.bot.username}?start={user.id}"

    if count == 0:
        bar, pct = "▱▱▱▱▱▱▱▱▱▱", 0
    else:
        filled = min(10, round(count / MILESTONE * 10))
        bar = "▰" * filled + "▱" * (10 - filled)
        pct = min(100, round(count / MILESTONE * 100))

    await update.message.reply_html(
        f"📊 <b>Your referral stats</b>\n\n"
        f"✅ Verified invites: <b>{count}</b>\n"
        f"🎯 Goal: <b>{MILESTONE}</b>\n"
        f"⏳ Remaining: <b>{remaining}</b>\n"
        f"📈 Progress: {pct}% {bar}\n\n"
        f"🔗 Your link:\n<code>{invite_link}</code>"
    )


async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = await get_all_users_with_counts(10)
    if not rows or all(r["invite_count"] == 0 for r in rows):
        await update.message.reply_text("No referrals yet — be the first to invite someone!")
        return

    medals = ["🥇", "🥈", "🥉"]
    lines = ["🏆 <b>Top 10 Referrers</b>\n"]
    for i, row in enumerate(rows):
        if row["invite_count"] == 0:
            break
        medal = medals[i] if i < 3 else f"{i + 1}."
        name = row["full_name"] or row["username"] or f"User {row['user_id']}"
        lines.append(f"{medal} {name} — <b>{row['invite_count']}</b> invite(s)")

    await update.message.reply_html("\n".join(lines))


async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Admin only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /broadcast <message>")
        return

    text = " ".join(context.args)
    user_ids = await get_all_user_ids()
    if not user_ids:
        await update.message.reply_text("No users registered yet.")
        return

    sent = skipped = 0
    for uid in user_ids:
        try:
            await context.bot.send_message(chat_id=uid, text=text)
            sent += 1
            await asyncio.sleep(0.05)
        except (BadRequest, Forbidden):
            skipped += 1
        except Exception as e:
            logger.warning(f"Broadcast failed for {uid}: {e}")
            skipped += 1

    await update.message.reply_html(
        f"📣 Broadcast complete.\n\n"
        f"✅ Delivered: <b>{sent}</b>\n"
        f"⛔ Skipped: <b>{skipped}</b>"
    )


async def refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Re-check every user against the channel and credit missed referrals or revoke left ones."""
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Admin only.")
        return

    all_users = await get_all_user_ids()
    if not all_users:
        await update.message.reply_text("✅ No users registered yet — nothing to refresh.")
        return

    status_msg = await update.message.reply_html(
        f"🔄 Syncing and checking <b>{len(all_users)}</b> user(s) against the channel…"
    )

    newly_credited = 0
    newly_revoked = 0

    for uid in all_users:
        try:
            # JAVÍTVA: Külön belső try-except blokk, hogy egyetlen hiba se akassza meg a ciklust
            try:
                in_channel = await is_channel_member(context.bot, uid)
            except Exception as e:
                logger.warning(f"Refresh API error for user {uid}: {e}")
                continue

            user_row = await get_user(uid)
            if not user_row:
                continue

            was_verified = bool(user_row["channel_verified"])

            # ─── 1. ESET: BENT VAN a csatornában, de eddig nem volt igazolva ───
            if in_channel and not was_verified:
                credited = await verify_channel_member(uid)
                if credited:
                    newly_credited += 1
                    referrer_id = user_row["invited_by"]
                    if referrer_id and await get_user(referrer_id):
                        count = await get_invite_count(referrer_id)
                        try:
                            await context.bot.send_message(
                                chat_id=referrer_id,
                                text=(
                                    f"🎉 Someone joined the channel using your invite link!\n"
                                    f"You now have <b>{count}</b> verified invite(s)."
                                ),
                                parse_mode="HTML",
                            )
                        except (BadRequest, Forbidden):
                            pass
                        if count >= MILESTONE and not await milestone_already_notified(referrer_id, MILESTONE):
                            await mark_milestone_notified(referrer_id, MILESTONE)
                            try:
                                await context.bot.send_message(
                                    chat_id=referrer_id,
                                    text=f"🏆 Congratulations! You reached <b>{MILESTONE} invites</b>!",
                                    parse_mode="HTML",
                                )
                            except (BadRequest, Forbidden):
                                pass

            # ─── 2. ESET: KILÉPETT a csatornából, de az adatbázisban még igazolt ───
            elif not in_channel and was_verified:
                was_revoked, referrer_id, full_name = await unverify_channel_member(uid)
                if was_revoked:
                    newly_revoked += 1
                    if referrer_id:
                        count = await get_invite_count(referrer_id)
                        try:
                            await context.bot.send_message(
                                chat_id=referrer_id,
                                text=(
                                    f"⚠️ <b>Hoppá, egy meghívottad kilépett!</b>\n\n"
                                    f"👤 <b>{full_name}</b> elhagyta a csatornát, ezért a pontja levonásra került.\n"
                                    f"Jelenlegi sikeres meghívásaid száma: <b>{count}</b>."
                                ),
                                parse_mode="HTML",
                            )
                        except (BadRequest, Forbidden):
                            pass

            await asyncio.sleep(0.05)  # Biztonsági késleltetés a rate-limit ellen
            
        except Exception as general_item_error:
            logger.error(f"Error processing refresh item for uid {uid}: {general_item_error}")
            continue

    try:
        await status_msg.edit_text(
            f"✅ <b>Refresh complete.</b>\n\n"
            f"🟢 Newly verified & credited: <b>{newly_credited}</b>\n"
            f"🔴 Revoked & deducted (left): <b>{newly_revoked}</b>",
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error(f"Failed to send final status update message: {e}")


async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Admin only.")
        return

    rows = await get_all_users_with_counts(50)
    if not rows:
        await update.message.reply_text("No users registered yet.")
        return

    total_users = len(rows)
    total_invites = sum(r["invite_count"] for r in rows)
    lines = [f"👥 <b>Users ({total_users}) — Total invites: {total_invites}</b>\n"]
    for row in rows:
        name = row["full_name"] or row["username"] or str(row["user_id"])
        uname = f" (@{row['username']})" if row["username"] else ""
        icon = "✅" if row["channel_verified"] else "⏳"
        lines.append(
            f"{icon} {name}{uname} — <b>{row['invite_count']}</b> "
            f"invite(s) [<code>{row['user_id']}</code>]"
        )

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n…(truncated)"
    await update.message.reply_html(text)


# ─── Error handler ───────────────────────────────────────────────────────────

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    err = context.error
    if isinstance(err, RetryAfter):
        logger.warning(f"Rate limited — waiting {err.retry_after}s")
        await asyncio.sleep(err.retry_after)
    elif isinstance(err, (TimedOut, NetworkError)):
        logger.warning(f"Network error (auto-recover): {err}")
    elif isinstance(err, (BadRequest, Forbidden)):
        logger.warning(f"Telegram API error: {err}")
    else:
        logger.error(f"Unhandled error: {err}", exc_info=err)


# ─── Health server ───────────────────────────────────────────────────────────

async def run_health_server():
    app = web.Application()
    app.router.add_get("/", lambda r: web.Response(text="OK"))
    app.router.add_get("/health", lambda r: web.Response(text="OK"))
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", HEALTH_PORT).start()
    logger.info(f"Health server listening on port {HEALTH_PORT}")
    await _shutdown.wait()
    await runner.cleanup()


# ─── Bot runner ──────────────────────────────────────────────────────────────

def _build_app() -> Application:
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(30)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("leaderboard", leaderboard))
    app.add_handler(CommandHandler("broadcast", broadcast))
    app.add_handler(CommandHandler("admin", admin))
    app.add_handler(CommandHandler("refresh", refresh))
    # Auto-detect channel joins → credit referral without /start needed
    app.add_handler(ChatMemberHandler(on_channel_member_update, ChatMemberHandler.CHAT_MEMBER))
    app.add_error_handler(error_handler)
    return app


async def _run_bot_once():
    app = _build_app()
    await init_db()
    async with app:
        await app.initialize()
        await app.start()
        await app.updater.start_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        )
        logger.info("Bot polling active.")
        await _shutdown.wait()
        logger.info("Shutdown signal — stopping bot.")
        await app.updater.stop()
        await app.stop()


async def run_bot_with_watchdog():
    attempt = 0
    while not _shutdown.is_set():
        attempt += 1
        try:
            logger.info(f"Bot starting (attempt #{attempt})")
            await _run_bot_once()
            break  # clean exit
        except (KeyboardInterrupt, SystemExit):
            break
        except Exception:
            wait = min(120, 5 * attempt)
            logger.error(
                f"Bot crashed (attempt #{attempt}). Restarting in {wait}s...\n"
                + traceback.format_exc()
            )
            try:
                await asyncio.sleep(wait)
            except asyncio.CancelledError:
                break


# ─── Entry point ─────────────────────────────────────────────────────────────

def _handle_signal(sig):
    logger.info(f"Received {sig.name} — shutting down.")
    _shutdown.set()


def _asyncio_exception_handler(loop, context):
    msg = context.get("exception", context.get("message", "unknown"))
    logger.error(f"Uncaught asyncio exception: {msg}", exc_info=context.get("exception"))


async def main():
    loop = asyncio.get_running_loop()
    loop.set_exception_handler(_asyncio_exception_handler)
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _handle_signal, sig)

    logger.info("=== Telegram Referral Bot starting ===")
    await asyncio.gather(run_health_server(), run_bot_with_watchdog())
    logger.info("=== Bot shut down cleanly ===")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
