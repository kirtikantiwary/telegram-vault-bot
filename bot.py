"""
bot.py
Main entry point. Registers all user-facing and admin command handlers.

Run with: python bot.py
Requires BOT_TOKEN and ADMIN_IDS set in .env (see .env.example)
"""

import os
import logging
from dotenv import load_dotenv

from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters
)

import db
import admin as admin_module

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]

# Webhook config (used only when RUN_MODE=webhook, e.g. on Render)
RUN_MODE = os.getenv("RUN_MODE", "polling")  # "polling" (local) or "webhook" (Render)
PORT = int(os.getenv("PORT", "10000"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")  # e.g. https://your-app.onrender.com

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)


# ---------- USER COMMANDS ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db.upsert_user(user.id, user.username, user.first_name)
    db.log_traffic(user.id, "incoming", "start")

    text = (
        f"Hey {user.first_name}! 👋\n\n"
        "This bot is your personal message vault — forward or send me anything "
        "(text, photos, files, links) and I'll save it for you to find later.\n\n"
        "Commands:\n"
        "/list - see your recent saved items\n"
        "/search <keyword> - search your saved items\n"
        "/share <id> <user_id> - share a saved item with someone\n"
        "/shared - see items shared with you\n"
        "/delete <id> - delete a saved item\n"
        "/privacy - read our data & privacy policy\n\n"
        "⚠️ Please read /privacy before using this bot — it explains what "
        "admins can see."
    )
    db.log_traffic(user.id, "outgoing", "start_reply")
    await update.message.reply_text(text)


async def privacy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📄 Privacy & Data Notice\n\n"
        "• Everything you send or forward to this bot is stored on our server, "
        "as-is, without encryption.\n"
        "• Bot administrators can view all stored content and usage activity "
        "for moderation, debugging, and abuse-prevention purposes.\n"
        "• Do not send anything here you wouldn't want an admin to see "
        "(passwords, financial info, private info of others, etc.).\n"
        "• Content that is illegal or violates Telegram's Terms of Service "
        "may be removed and the account may be banned from this bot.\n"
        "• This bot is an independent tool and is not affiliated with "
        "Telegram's own 'Saved Messages' feature.\n\n"
        "By using this bot, you agree to this policy."
    )
    await update.message.reply_text(text)


async def handle_incoming_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Catches any forwarded/sent message (text, photo, doc, video, voice) and saves it."""
    user = update.effective_user
    msg = update.message

    if db.is_banned(user.id):
        await msg.reply_text("You've been restricted from using this bot.")
        return

    db.upsert_user(user.id, user.username, user.first_name)
    db.log_traffic(user.id, "incoming", "message_received")

    content_type = "text"
    content_text = msg.text or msg.caption or ""
    file_id = None

    if msg.photo:
        content_type = "photo"
        file_id = msg.photo[-1].file_id
    elif msg.document:
        content_type = "document"
        file_id = msg.document.file_id
    elif msg.video:
        content_type = "video"
        file_id = msg.video.file_id
    elif msg.voice:
        content_type = "voice"
        file_id = msg.voice.file_id

    source_chat = None
    if msg.forward_origin:
        # Best-effort description of where it was forwarded from
        source_chat = str(getattr(msg.forward_origin, "sender_user", None) or
                           getattr(msg.forward_origin, "chat", None) or "unknown")

    msg_id = db.save_message(user.id, content_type, content_text, file_id, source_chat)
    db.log_traffic(user.id, "outgoing", "save_confirmation", detail=f"msg_id={msg_id}")

    await msg.reply_text(f"✅ Saved (ID: {msg_id})")


async def list_items(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db.log_traffic(user.id, "incoming", "list")
    rows = db.list_messages(user.id)

    if not rows:
        await update.message.reply_text("You haven't saved anything yet. Just forward me a message!")
        return

    lines = ["🗂 Your recent saved items:\n"]
    for r in rows:
        preview = (r["content_text"] or f"[{r['content_type']}]")[:50]
        lines.append(f"#{r['msg_id']} [{r['content_type']}] {preview}")

    db.log_traffic(user.id, "outgoing", "list_reply")
    await update.message.reply_text("\n".join(lines))


async def search_items(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not context.args:
        await update.message.reply_text("Usage: /search <keyword>")
        return

    keyword = " ".join(context.args)
    db.log_traffic(user.id, "incoming", "search", detail=keyword)
    rows = db.search_messages(user.id, keyword)

    if not rows:
        await update.message.reply_text("No matches found.")
        return

    lines = [f"🔍 Results for '{keyword}':\n"]
    for r in rows:
        preview = (r["content_text"] or f"[{r['content_type']}]")[:50]
        lines.append(f"#{r['msg_id']} [{r['content_type']}] {preview}")

    await update.message.reply_text("\n".join(lines))


async def retrieve_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send back the full saved content for a given ID: /get <id>"""
    user = update.effective_user
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /get <id>")
        return

    msg_id = int(context.args[0])
    row = db.get_message(msg_id)

    if not row or row["owner_id"] != user.id:
        await update.message.reply_text("Not found or not yours.")
        return

    db.log_traffic(user.id, "incoming", "retrieve", detail=f"msg_id={msg_id}")
    await send_saved_item(update, context, row)


async def send_saved_item(update, context, row):
    """Helper: sends a saved DB row back to the chat, matching its original type."""
    chat_id = update.effective_chat.id
    if row["content_type"] == "text":
        await context.bot.send_message(chat_id, row["content_text"] or "(empty)")
    elif row["content_type"] == "photo":
        await context.bot.send_photo(chat_id, row["file_id"], caption=row["content_text"])
    elif row["content_type"] == "document":
        await context.bot.send_document(chat_id, row["file_id"], caption=row["content_text"])
    elif row["content_type"] == "video":
        await context.bot.send_video(chat_id, row["file_id"], caption=row["content_text"])
    elif row["content_type"] == "voice":
        await context.bot.send_voice(chat_id, row["file_id"])


async def delete_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /delete <id>")
        return

    msg_id = int(context.args[0])
    ok = db.delete_message(msg_id, user.id)
    db.log_traffic(user.id, "incoming", "delete", detail=f"msg_id={msg_id} ok={ok}")

    await update.message.reply_text("🗑 Deleted." if ok else "Not found or not yours.")


async def share_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/share <id> <target_user_id> - explicit permission-based sharing."""
    user = update.effective_user
    if len(context.args) < 2 or not context.args[0].isdigit() or not context.args[1].isdigit():
        await update.message.reply_text("Usage: /share <id> <telegram_user_id>")
        return

    msg_id, target_id = int(context.args[0]), int(context.args[1])
    row = db.get_message(msg_id)

    if not row or row["owner_id"] != user.id:
        await update.message.reply_text("Not found or not yours.")
        return

    db.share_message(msg_id, user.id, target_id)
    db.log_traffic(user.id, "incoming", "share", detail=f"msg_id={msg_id} to={target_id}")

    await update.message.reply_text(f"✅ Shared item #{msg_id} with user {target_id}.")

    try:
        await context.bot.send_message(
            target_id,
            f"📩 {user.first_name} shared a saved item with you. Use /shared to view it."
        )
    except Exception:
        pass  # target may not have started the bot yet


async def shared_with_me(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    rows = db.get_shared_with_me(user.id)

    if not rows:
        await update.message.reply_text("Nothing has been shared with you yet.")
        return

    await update.message.reply_text(f"📬 You have {len(rows)} shared item(s):")
    for row in rows:
        await send_saved_item(update, context, row)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Logs errors and, if possible, tells the user something went wrong."""
    logger.error("Exception while handling an update:", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "⚠️ Something went wrong processing that command. The issue has been logged."
            )
        except Exception:
            pass


async def setup_command_menu(app):
    """Registers the tappable command menu users see when they tap '/' or the menu button."""
    from telegram import BotCommand
    await app.bot.set_my_commands([
        BotCommand("start", "Get started / see instructions"),
        BotCommand("list", "Show your recently saved items"),
        BotCommand("search", "Search your saved items by keyword"),
        BotCommand("get", "Retrieve a saved item by its ID"),
        BotCommand("delete", "Delete a saved item by its ID"),
        BotCommand("share", "Share a saved item with another user"),
        BotCommand("shared", "See items others have shared with you"),
        BotCommand("privacy", "Read the data & privacy policy"),
    ])


# ---------- APP SETUP ----------

def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN not set. Add it to your .env file.")

    db.init_db()
    for admin_id in ADMIN_IDS:
        db.add_admin(admin_id)

    app = ApplicationBuilder().token(BOT_TOKEN).post_init(setup_command_menu).build()

    # User commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("privacy", privacy))
    app.add_handler(CommandHandler("list", list_items))
    app.add_handler(CommandHandler("search", search_items))
    app.add_handler(CommandHandler("get", retrieve_item))
    app.add_handler(CommandHandler("delete", delete_item))
    app.add_handler(CommandHandler("share", share_item))
    app.add_handler(CommandHandler("shared", shared_with_me))

    # Admin commands (registered from admin.py)
    admin_module.register_admin_handlers(app, ADMIN_IDS)

    app.add_error_handler(error_handler)

    # Catch-all: any non-command message/forward gets saved
    app.add_handler(MessageHandler(
        (filters.TEXT | filters.PHOTO | filters.Document.ALL | filters.VIDEO | filters.VOICE)
        & ~filters.COMMAND,
        handle_incoming_message
    ))

    logger.info(f"Bot starting in {RUN_MODE} mode...")

    if RUN_MODE == "webhook":
        if not WEBHOOK_URL:
            raise RuntimeError("WEBHOOK_URL not set. Required for webhook mode on Render.")
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=BOT_TOKEN,
            webhook_url=f"{WEBHOOK_URL}/{BOT_TOKEN}",
        )
    else:
        app.run_polling()


if __name__ == "__main__":
    main()
