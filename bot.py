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
    unverify_channel_member,
    get_invite_count,
    milestone_already_notified,
    mark_milestone_notified,
    get_all_user_ids,
    get_all_users_with_counts,
    get_unverified_users,
    add_manual_points,
    remove_manual_points
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

# ─── Konfiguráció ───────────────────────────────────────────────────────────

BOT_TOKEN   = os.environ["TELEGRAM_BOT_TOKEN"]
ADMIN_ID    = int(os.environ["TELEGRAM_ADMIN_ID"])
CHANNEL     = os.environ["TELEGRAM_CHANNEL"]            # pl. "@csatornad"
CHANNEL_ID  = int(os.environ.get("TELEGRAM_GROUP_ID", 0))  # numerikus ID
HEALTH_PORT = int(os.environ.get("PORT", 8000))
MILESTONE   = 20

_shutdown = asyncio.Event()

ACTIVE_MEMBER_STATUSES = {"member", "administrator", "creator", "restricted"}
INACTIVE_MEMBER_STATUSES = {"left", "kicked", "banned"}


# ─── Segédfüggvények ─────────────────────────────────────────────────────────

def channel_button() -> InlineKeyboardMarkup:
    url = f"https://t.me/{CHANNEL.lstrip('@')}"
    return InlineKeyboardMarkup([[InlineKeyboardButton("📢 Csatlakozás a Csatornához", url=url)]])


def _is_our_channel(chat) -> bool:
    """Ellenőrzi, hogy a chat objektum a mi beállított csatornánkra mutat-e."""
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
        logger.warning(f"is_channel_member hiba a következő felhasználónál {user_id}: {e}")
        return False


async def _credit_referral(bot, user_id: int):
    """
    Megjelöli a felhasználót igazoltként, és jóváírja a pontot a meghívónak.
    Meghívódik a /start-ból és az automatikus csatorna-csatlakozás figyelőből is.
    """
    newly_verified = await verify_channel_member(user_id)
    if not newly_verified:
        return  # Már korábban el lett könyvelve

    user_row = await get_user(user_id)
    if not user_row:
        return

    referrer_id = user_row["invited_by"]
    if referrer_id and await get_user(referrer_id):
        # Meghívó értesítése
        count = await get_invite_count(referrer_id)
        try:
            await bot.send_message(
                chat_id=referrer_id,
                text=(
                    f"🎉 Valaki csatlakozott a csatornához a meghívó linkeddel!\n"
                    f"Jelenleg <b>{count}</b> ellenőrzött meghívásod van."
                ),
                parse_mode="HTML",
            )
        except (BadRequest, Forbidden):
            pass

        # Cél elérése (Milestone) ellenőrzése
        if count >= MILESTONE and not await milestone_already_notified(referrer_id, MILESTONE):
            await mark_milestone_notified(referrer_id, MILESTONE)
            try:
                await bot.send_message(
                    chat_id=referrer_id,
                    text=f"🏆 Gratulálunk! Elérted a(z) <b>{MILESTONE} sikeres meghívást</b>!",
                    parse_mode="HTML",
                )
            except (BadRequest, Forbidden):
                pass

        # Az új tag értesítése, hogy a meghívása sikeres volt
        try:
            invite_link = f"https://t.me/{(await bot.get_me()).username}?start={user_id}"
            await bot.send_message(
                chat_id=user_id,
                text=(
                    "✅ Sikeresen csatlakoztál a csatornához — a meghívást rögzítettük!\n\n"
                    f"🔗 A saját meghívó linked:\n<code>{invite_link}</code>\n\n"
                    f"Oszd meg a barátaiddal, hogy elérd a <b>{MILESTONE}</b> meghívottas célt."
                ),
                parse_mode="HTML",
            )
        except (BadRequest, Forbidden):
            pass


# ─── Csatorna csatlakozás figyelő (Automatikusan lefut, ha belépnek) ───────────

async def on_channel_member_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cm = update.chat_member
    if cm is None:
        return

    if not _is_our_channel(cm.chat):
        return  # Nem a mi csatornánk

    old_status = cm.old_chat_member.status
    new_status = cm.new_chat_member.status

    was_outside = old_status in INACTIVE_MEMBER_STATUSES
    is_now_inside = new_status in ACTIVE_MEMBER_STATUSES

    if not (was_outside and is_now_inside):
        return  # Nem belépési esemény

    user = cm.new_chat_member.user
    logger.info(f"User {user.id} (@{user.username}) belépett a csatornába — ellenőrzés.")

    # Regisztráljuk az adatbázisba, ha még nem indította volna el a botot
    await register_user(user.id, user.username or "", user.full_name)

    await _credit_referral(context.bot, user.id)


# ─── Parancs Kezelők (Commands) ───────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    # Meghívó ID kinyerése a linkből
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

    in_channel = await is_channel_member(context.bot, user.id)

    if in_channel:
        # Ha már bent van, azonnal jóváírjuk
        await _credit_referral(context.bot, user.id)
    else:
        # Ha még nincs bent, gombot mutatunk neki
        await update.message.reply_text(
            "⚠️ A bot használatához először csatlakoznod kell a csatornánkhoz.\n\n"
            "👇 Kattints az alábbi gombra, majd gyere vissza — a pontod automatikusan "
            "jóváíródik, amint beléptél!",
            reply_markup=channel_button(),
        )
        return

    invite_link = f"https://t.me/{context.bot.username}?start={user.id}"
    count = await get_invite_count(user.id)

    await update.message.reply_html(
        f"👋 Üdvözlünk, {user.first_name}!\n\n"
        f"🔗 A személyes meghívó linked:\n"
        f"<code>{invite_link}</code>\n\n"
        f"Küldd el az ismerőseidnek. Ha csatlakoznak a csatornához, "
        f"az ellenőrzött meghívásnak fog számítani.\n\n"
        f"📊 Jelenleg <b>{count}</b> ellenőrzött meghívásod van.\n"
        f"🎯 Cél: <b>{MILESTONE}</b> meghívott."
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
        f"📊 <b>A te statisztikád</b>\n\n"
        f"✅ Sikeres meghívások: <b>{count}</b>\n"
        f"🎯 Cél: <b>{MILESTONE}</b>\n"
        f"⏳ Hátralévő: <b>{remaining}</b>\n"
        f"📈 Haladás: {pct}% {bar}\n\n"
        f"🔗 A linked:\n<code>{invite_link}</code>"
    )


async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = await get_all_users_with_counts(10)
    if not rows or all(r["invite_count"] == 0 for r in rows):
        await update.message.reply_text("Még nincsenek meghívások — légy te az első!")
        return

    medals = ["🥇", "🥈", "🥉"]
    lines = ["🏆 <b>Top 10 Meghívó</b>\n"]
    for i, row in enumerate(rows):
        if row["invite_count"] == 0:
            break
        medal = medals[i] if i < 3 else f"{i + 1}."
        name = row["full_name"] or row["username"] or f"Felhasználó {row['user_id']}"
        lines.append(f"{medal} {name} — <b>{row['invite_count']}</b> meghívás")

    await update.message.reply_html("\n".join(lines))


async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Csak adminisztrátoroknak.")
        return
    if not context.args:
        await update.message.reply_text("Használat: /broadcast <üzenet>")
        return

    text = " ".join(context.args)
    user_ids = await get_all_user_ids()
    if not user_ids:
        await update.message.reply_text("Még nincsenek regisztrált felhasználók.")
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
            logger.warning(f"Kiküldés sikertelen a következőhöz {uid}: {e}")
            skipped += 1

    await update.message.reply_html(
        f"📣 Üzenet kiküldése kész.\n\n"
        f"✅ Kézbesítve: <b>{sent}</b>\n"
        f"⛔ Kihagyva (letiltott/hibás): <b>{skipped}</b>"
    )


async def refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Csatorna tagok ellenőrzése, hiányzó pontok megadása, kilépettektől levonás."""
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Csak adminisztrátoroknak.")
        return

    all_users = await get_all_user_ids()
    if not all_users:
        await update.message.reply_text("✅ Nincs regisztrált felhasználó az adatbázisban.")
        return

    status_msg = await update.message.reply_html(
        f"🔄 Szinkronizálás és csatorna ellenőrzés fut <b>{len(all_users)}</b> tagnál…"
    )

    newly_credited = 0
    newly_revoked = 0
    credited_details = []  
    revoked_details = []   

    for uid in all_users:
        try:
            try:
                in_channel = await is_channel_member(context.bot, uid)
            except Exception as e:
                logger.warning(f"Refresh API hiba a következő felhasználónál {uid}: {e}")
                continue

            user_row = await get_user(uid)
            if not user_row:
                continue

            was_verified = bool(user_row["channel_verified"])
            full_name = user_row["full_name"] or user_row["username"] or f"User {uid}"

            # ─── 1. ESET: BENT VAN, de eddig nem volt igazolva ───
            if in_channel and not was_verified:
                credited = await verify_channel_member(uid)
                if credited:
                    newly_credited += 1
                    referrer_id = user_row["invited_by"]
                    
                    referrer_name = "Nincs/Ismeretlen"
                    new_count_str = ""
                    
                    if referrer_id:
                        ref_row = await get_user(referrer_id)
                        if ref_row:
                            referrer_name = ref_row["full_name"] or ref_row["username"] or str(referrer_id)
                        
                        current_count = await get_invite_count(referrer_id)
                        new_count_str = f" (Új egyenleg: <b>{current_count}p</b>)"
                        
                        try:
                            await context.bot.send_message(
                                chat_id=referrer_id,
                                text=(
                                    f"🎉 Valaki csatlakozott a csatornához a meghívó linkeddel!\n"
                                    f"Jelenleg <b>{current_count}</b> ellenőrzött meghívásod van."
                                ),
                                parse_mode="HTML",
                            )
                        except (BadRequest, Forbidden):
                            pass
                            
                        if current_count >= MILESTONE and not await milestone_already_notified(referrer_id, MILESTONE):
                            await mark_milestone_notified(referrer_id, MILESTONE)
                            try:
                                await context.bot.send_message(
                                    chat_id=referrer_id,
                                    text=f"🏆 Gratulálunk! Elérted a(z) <b>{MILESTONE} meghívottas</b> célt!",
                                    parse_mode="HTML",
                                )
                            except (BadRequest, Forbidden):
                                pass
                    
                    credited_details.append(
                        f"• 👤 <b>{full_name}</b> belépett ➔ pont megadva neki: 👤 <b>{referrer_name}</b>{new_count_str}"
                    )

            # ─── 2. ESET: KILÉPETT, de az adatbázisban még igazolt volt ───
            elif not in_channel and was_verified:
                was_revoked, referrer_id, revoked_name = await unverify_channel_member(uid)
                if was_revoked:
                    newly_revoked += 1
                    
                    referrer_name = "Nincs/Ismeretlen"
                    new_count_str = ""
                    
                    if referrer_id:
                        ref_row = await get_user(referrer_id)
                        if ref_row:
                            referrer_name = ref_row["full_name"] or ref_row["username"] or str(referrer_id)
                        
                        current_count = await get_invite_count(referrer_id)
                        new_count_str = f" (Új egyenleg: <b>{current_count}p</b>)"
                        
                        try:
                            await context.bot.send_message(
                                chat_id=referrer_id,
                                text=(
                                    f"⚠️ <b>Hoppá, egy meghívottad kilépett!</b>\n\n"
                                    f"👤 <b>{revoked_name}</b> elhagyta a csatornát, ezért a pontja levonásra került.\n"
                                    f"Jelenlegi sikeres meghívásaid száma: <b>{current_count}</b>."
                                ),
                                parse_mode="HTML",
                            )
                        except (BadRequest, Forbidden):
                            pass
                    
                    revoked_details.append(
                        f"• 👤 <b>{revoked_name}</b> kilépett ➔ pont levonva tőle: 👤 <b>{referrer_name}</b>{new_count_str}"
                    )

            await asyncio.sleep(0.05)
            
        except Exception as general_item_error:
            logger.error(f"Hiba a frissítési folyamat közben (UID: {uid}): {general_item_error}")
            continue

    result_text = (
        f"✅ <b>Frissítés befejezve.</b>\n\n"
        f"🟢 Újonnan igazolt & jóváírt: <b>{newly_credited}</b>\n"
        f"🔴 Kilépett & levont: <b>{newly_revoked}</b>"
    )

    if credited_details:
        result_text += "\n\n➕ <b>Jóváírások részletei:</b>\n" + "\n".join(credited_details)

    if revoked_details:
        result_text += "\n\n📋 <b>Levonások részletei:</b>\n" + "\n".join(revoked_details)

    if len(result_text) > 4000:
        result_text = result_text[:4000] + "\n…(a lista a mérete miatt le lett vágva)"

    try:
        await status_msg.edit_text(result_text, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Nem sikerült módosítani az admin státusz üzenetet: {e}")


async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Csak adminisztrátoroknak.")
        return

    rows = await get_all_users_with_counts(50)
    if not rows:
        await update.message.reply_text("Még nincs egyetlen regisztrált tag sem.")
        return

    total_users = len(rows)
    total_invites = sum(r["invite_count"] for r in rows)
    lines = [f"👥 <b>Felhasználók ({total_users}) — Összes meghívás: {total_invites}</b>\n"]
    for row in rows:
        name = row["full_name"] or row["username"] or str(row["user_id"])
        uname = f" (@{row['username']})" if row["username"] else ""
        icon = "✅" if row["channel_verified"] else "⏳"
        lines.append(
            f"{icon} {name}{uname} — <b>{row['invite_count']}</b> "
            f"meghívás [<code>{row['user_id']}</code>]"
        )

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n…(levágva)"
    await update.message.reply_html(text)


async def pontadd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin parancs: Pont manuális hozzáadása egy felhasználóhoz ID alapján."""
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Csak adminisztrátoroknak.")
        return

    if not context.args or len(context.args) < 2:
        await update.message.reply_text("Használat: /pontadd <felhasználó_ID> <mennyiség>")
        return

    try:
        target_id = int(context.args[0])
        amount = int(context.args[1])
    except ValueError:
        await update.message.reply_text("❌ Hibás formátum! Az ID-nek és a mennyiségnek is számnak kell lennie.")
        return

    user_row = await get_user(target_id)  # <-- JAVÍTVA: get_db helyett get_user
    if not user_row:
        await update.message.reply_html(f"❌ Felhasználó [<code>{target_id}</code>] nem található az adatbázisban.")
        return

    try:
        await add_manual_points(target_id, amount)
    except Exception as e:
        await update.message.reply_text(f"❌ Hiba történt a pont hozzáadásakor: {e}")
        return

    new_count = await get_invite_count(target_id)
    name = user_row["full_name"] or user_row["username"] or str(target_id)

    await update.message.reply_html(
        f"✅ Sikeresen hozzáadva <b>{amount}</b> pont a következő taghoz: <b>{name}</b>.\n"
        f"Új egyenlege: <b>{new_count}p</b>"
    )


async def pontelvesz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin parancs: Pont manuális levonása egy felhasználótól ID alapján, kilépési értesítéssel."""
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Csak adminisztrátoroknak.")
        return

    if not context.args or len(context.args) < 2:
        await update.message.reply_text("Használat: /pontelvesz <felhasználó_ID> <mennyiség>")
        return

    try:
        target_id = int(context.args[0])
        amount = int(context.args[1])
    except ValueError:
        await update.message.reply_text("❌ Hibás formátum! Az ID-nek és a mennyiségnek is számnak kell lennie.")
        return

    user_row = await get_user(target_id)  # <-- JAVÍTVA: get_db helyett get_user
    if not user_row:
        await update.message.reply_html(f"❌ Felhasználó [<code>{target_id}</code>] nem található az adatbázisban.")
        return

    try:
        await remove_manual_points(target_id, amount)
    except Exception as e:
        await update.message.reply_text(f"❌ Hiba történt a pont levonásakor: {e}")
        return

    new_count = await get_invite_count(target_id)
    name = user_row["full_name"] or user_row["username"] or str(target_id)

    await update.message.reply_html(
        f"📉 Manuálisan levonva <b>{amount}</b> pont tőle: <b>{name}</b>.\n"
        f"Új egyenlege: <b>{new_count}p</b>\n\n"
        f"📢 <i>A rendszer elküldte neki a kilépésről szóló figyelmeztetést!</i>"
    )

    try:
        await context.bot.send_message(
            chat_id=target_id,
            text=(
                f"⚠️ <b>Hoppá, egy meghívottad kilépett!</b>\n\n"
                f"Valaki elhagyta a csatornát, ezért a pontja levonásra került.\n"
                f"Jelenlegi sikeres meghívásaid száma: <b>{new_count}</b>."
            ),
            parse_mode="HTML",
        )
    except Exception:
        pass

async def admin_pontelvesz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin parancs: Pont manuális levonása egy felhasználótól ID alapján, TELJESEN Csendben."""
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Csak adminisztrátoroknak.")
        return

    if not context.args or len(context.args) < 2:
        await update.message.reply_text("Használat: /admin_pontelvesz <felhasználó_ID> <mennyiség>")
        return

    try:
        target_id = int(context.args[0])
        amount = int(context.args[1])
    except ValueError:
        await update.message.reply_text("❌ Hibás formátum! Az ID-nek és a mennyiségnek is számnak kell lennie.")
        return

    user_row = await get_user(target_id)
    if not user_row:
        await update.message.reply_html(f"❌ Felhasználó [<code>{target_id}</code>] nem található az adatbázisban.")
        return

    try:
        await remove_manual_points(target_id, amount)
    except Exception as e:
        await update.message.reply_text(f"❌ Hiba történt a pont levonásakor: {e}")
        return

    new_count = await get_invite_count(target_id)
    name = user_row["full_name"] or user_row["username"] or str(target_id)

    # Csak az admin kap egy egyszerű visszajelzést, a felhasználó semmit nem vesz észre
    await update.message.reply_html(
        f"🤫 <b>Csendes levonás sikeres!</b>\n"
        f"Manuálisan levonva <b>{amount}</b> pont tőle: <b>{name}</b>.\n"
        f"Új egyenlege: <b>{new_count}p</b>"
    )
# ─── Hibakezelő (Error handler) ──────────────────────────────────────────────

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    err = context.error
    if isinstance(err, RetryAfter):
        logger.warning(f"Rate limit — várakozás {err.retry_after}s")
        await asyncio.sleep(err.retry_after)
    elif isinstance(err, (TimedOut, NetworkError)):
        logger.warning(f"Hálózati hiba (automatikus újrapróbálkozás): {err}")
    elif isinstance(err, (BadRequest, Forbidden)):
        logger.warning(f"Telegram API hiba: {err}")
    else:
        logger.error(f"Kezeletlen hiba lépett fel: {err}", exc_info=err)


# ─── Health server ───────────────────────────────────────────────────────────

async def run_health_server():
    app = web.Application()
    app.router.add_get("/", lambda r: web.Response(text="OK"))
    app.router.add_get("/health", lambda r: web.Response(text="OK"))
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", HEALTH_PORT).start()
    logger.info(f"Health szerver elindult a porton: {HEALTH_PORT}")
    await _shutdown.wait()
    await runner.cleanup()


# ─── Bot indító és futtató környzet ──────────────────────────────────────────

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
    app.add_handler(CommandHandler("pontadd", pontadd))
    app.add_handler(CommandHandler("pontelvesz", pontelvesz))
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
        logger.info("Bot lekérdezés (polling) aktív.")
        await _shutdown.wait()
        logger.info("Leállítási jel érkezett — bot megállítása.")
        await app.updater.stop()
        await app.stop()


async def run_bot_with_watchdog():
    attempt = 0
    while not _shutdown.is_set():
        attempt += 1
        try:
            logger.info(f"Bot indulása (Kísérlet #{attempt})")
            await _run_bot_once()
            break
        except (KeyboardInterrupt, SystemExit):
            break
        except Exception:
            wait = min(120, 5 * attempt)
            logger.error(
                f"A bot összeomlott (Kísérlet #{attempt}). Újraindítás {wait} másodperc múlva...\n"
                + traceback.format_exc()
            )
            try:
                await asyncio.sleep(wait)
            except asyncio.CancelledError:
                break


# ─── Belépési Pont (Main) ─────────────────────────────────────────────────────

def _handle_signal(sig):
    logger.info(f"Leállítási szignál érkezett ({sig.name}) — kikapcsolás.")
    _shutdown.set()


def _asyncio_exception_handler(loop, context):
    msg = context.get("exception", context.get("message", "ismeretlen"))
    logger.error(f"Kezeletlen aszinkron hiba: {msg}", exc_info=context.get("exception"))


async def main():
    loop = asyncio.get_running_loop()
    loop.set_exception_handler(_asyncio_exception_handler)
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _handle_signal, sig)

    logger.info("=== Telegram Ajánló Bot Elindul ===")
    await asyncio.gather(run_health_server(), run_bot_with_watchdog())
    logger.info("=== A bot sikeresen leállt ===")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
