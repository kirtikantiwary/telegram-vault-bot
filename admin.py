"""
admin.py
Admin-only commands for monitoring traffic, viewing stored data, and moderation.
Only users whose IDs are in ADMIN_IDS (.env) can use these.
"""

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes, Application

import db


def admin_only(admin_ids):
    """Decorator factory: blocks non-admins from running a handler."""
    def decorator(func):
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
            user = update.effective_user
            if user.id not in admin_ids and not db.is_admin(user.id):
                await update.message.reply_text("⛔ Admin only.")
                return
            db.log_traffic(user.id, "incoming", f"admin_{func.__name__}")
            return await func(update, context)
        return wrapper
    return decorator


def register_admin_handlers(app: Application, admin_ids):

    @admin_only(admin_ids)
    async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
        s = db.get_stats()
        lines = [
            "📊 Bot Stats",
            f"Total users: {s['total_users']}",
            f"Total saved messages: {s['total_messages']}",
            f"Incoming events logged: {s['incoming_events']}",
            f"Outgoing events logged: {s['outgoing_events']}",
            "",
            "By content type:",
        ]
        for ctype, n in s["by_type"].items():
            lines.append(f"  {ctype}: {n}")

        lines.append("\nTop 5 users by saved messages:")
        for uid, n in s["top_users"]:
            lines.append(f"  {uid}: {n} items")

        await update.message.reply_text("\n".join(lines))

    @admin_only(admin_ids)
    async def traffic(update: Update, context: ContextTypes.DEFAULT_TYPE):
        rows = db.get_recent_traffic(limit=30)
        if not rows:
            await update.message.reply_text("No traffic logged yet.")
            return

        lines = ["📡 Recent traffic (latest 30):\n"]
        for r in rows:
            lines.append(f"[{r['timestamp']}] user={r['user_id']} {r['direction']} - {r['action']} {r['detail'] or ''}")

        text = "\n".join(lines)
        for i in range(0, len(text), 4000):
            await update.message.reply_text(text[i:i+4000])

    @admin_only(admin_ids)
    async def view_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Admin: raw dump of all stored messages across all users. /viewall [offset]"""
        offset = int(context.args[0]) if context.args and context.args[0].isdigit() else 0
        rows = db.get_all_messages(limit=20, offset=offset)

        if not rows:
            await update.message.reply_text("No more messages.")
            return

        lines = [f"🗄 All messages (offset {offset}):\n"]
        for r in rows:
            preview = (r["content_text"] or f"[{r['content_type']}]")[:60]
            lines.append(f"#{r['msg_id']} owner={r['owner_id']} (@{r['username']}) [{r['content_type']}] {preview}")

        lines.append(f"\nNext page: /viewall {offset + 20}")
        await update.message.reply_text("\n".join(lines))

    @admin_only(admin_ids)
    async def view_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Admin: view a specific user's full vault. /viewuser <user_id>"""
        if not context.args or not context.args[0].isdigit():
            await update.message.reply_text("Usage: /viewuser <user_id>")
            return

        target = int(context.args[0])
        rows = db.get_user_messages_admin(target)

        if not rows:
            await update.message.reply_text("No messages for this user.")
            return

        lines = [f"👤 Messages for user {target}:\n"]
        for r in rows:
            preview = (r["content_text"] or f"[{r['content_type']}]")[:60]
            lines.append(f"#{r['msg_id']} [{r['content_type']}] {preview}")

        await update.message.reply_text("\n".join(lines))

    @admin_only(admin_ids)
    async def ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/ban <user_id>"""
        if not context.args or not context.args[0].isdigit():
            await update.message.reply_text("Usage: /ban <user_id>")
            return
        target = int(context.args[0])
        db.ban_user(target, True)
        await update.message.reply_text(f"🚫 User {target} banned.")

    @admin_only(admin_ids)
    async def unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/unban <user_id>"""
        if not context.args or not context.args[0].isdigit():
            await update.message.reply_text("Usage: /unban <user_id>")
            return
        target = int(context.args[0])
        db.ban_user(target, False)
        await update.message.reply_text(f"✅ User {target} unbanned.")

    @admin_only(admin_ids)
    async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/broadcast <message> - sends to all known users. Use sparingly."""
        if not context.args:
            await update.message.reply_text("Usage: /broadcast <message>")
            return

        text = "📢 " + " ".join(context.args)
        with db.get_conn() as conn:
            c = conn.cursor()
            c.execute("SELECT user_id FROM users WHERE is_banned=0")
            user_ids = [row["user_id"] for row in c.fetchall()]

        sent, failed = 0, 0
        for uid in user_ids:
            try:
                await context.bot.send_message(uid, text)
                sent += 1
            except Exception:
                failed += 1

        await update.message.reply_text(f"Broadcast sent: {sent} ok, {failed} failed.")

    @admin_only(admin_ids)
    async def view_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Admin: view/download the actual saved file for any message, by ID.
        Usage: /viewfile <msg_id>
        """
        if not context.args or not context.args[0].isdigit():
            await update.message.reply_text("Usage: /viewfile <msg_id> (get the id from /viewall)")
            return

        msg_id = int(context.args[0])
        row = db.get_message(msg_id)

        if not row:
            await update.message.reply_text("No message found with that ID.")
            return

        chat_id = update.effective_chat.id
        caption = f"#{row['msg_id']} from user {row['owner_id']} | {row['content_type']}"
        if row["content_text"]:
            caption += f"\n\n{row['content_text'][:500]}"

        try:
            if row["content_type"] == "text":
                await update.message.reply_text(caption)
            elif row["content_type"] == "photo":
                await context.bot.send_photo(chat_id, row["file_id"], caption=caption)
            elif row["content_type"] == "document":
                await context.bot.send_document(chat_id, row["file_id"], caption=caption)
            elif row["content_type"] == "video":
                await context.bot.send_video(chat_id, row["file_id"], caption=caption)
            elif row["content_type"] == "voice":
                await context.bot.send_voice(chat_id, row["file_id"], caption=caption)
        except Exception as e:
            await update.message.reply_text(f"Couldn't retrieve file: {e}")

    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("traffic", traffic))
    app.add_handler(CommandHandler("viewall", view_all))
    app.add_handler(CommandHandler("viewfile", view_file))
    app.add_handler(CommandHandler("viewuser", view_user))
    app.add_handler(CommandHandler("ban", ban))
    app.add_handler(CommandHandler("unban", unban))
    app.add_handler(CommandHandler("broadcast", broadcast))
