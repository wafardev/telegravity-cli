#!/usr/bin/env python3
"""
telegravity-cli daemon & Telegram bot.
Integrates multigravity (mgy) task dispatching with real-time session completion monitoring.
"""

import asyncio
import html
import logging
from typing import Optional, Set
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.request import HTTPXRequest
from telegram.constants import ChatAction
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

from config import (
    TELEGRAM_BOT_TOKEN,
    ALLOWED_USER_IDS,
    RUN_MODE,
    MGY_PROFILE,
    SSH_USER,
    SSH_HOST,
    SSH_PORT,
)
from mgy_runner import MgyRunner
from session_monitor import SessionMonitor

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Global instances
runner = MgyRunner(mode=RUN_MODE, default_profile=MGY_PROFILE)
monitor = SessionMonitor(runner=runner)

# Active conversation tracking per chat (chat_id -> conversation_id or None)
active_conversations: dict[int, Optional[str]] = {}
# Known authorized chats to notify on external completion
known_chats: Set[int] = set()


def is_authorized(user_id: int) -> bool:
    if not ALLOWED_USER_IDS:
        return True
    return user_id in ALLOWED_USER_IDS


async def check_auth_or_reject(update: Update) -> bool:
    user = update.effective_user
    if not user:
        return False
    if not is_authorized(user.id):
        if update.message:
            await update.message.reply_html(
                f"⛔ Access denied.\nYour Telegram User ID is: <code>{user.id}</code>\n"
                f"Add this ID to ALLOWED_USER_IDS in .env to grant access."
            )
        return False
    if update.effective_chat:
        known_chats.add(update.effective_chat.id)
    return True


async def send_chunked_code(
    chat_id: int,
    bot,
    text: str,
    header: str = "",
    reply_markup: Optional[InlineKeyboardMarkup] = None,
) -> None:
    """Sends text in chunks safely formatted inside code blocks."""
    if not text:
        return

    MAX_CHUNK = 3800
    chunks = [text[i : i + MAX_CHUNK] for i in range(0, len(text), MAX_CHUNK)]

    for idx, chunk in enumerate(chunks):
        escaped_chunk = html.escape(chunk)
        if idx == 0 and header:
            msg = f"{header}\n<pre><code>{escaped_chunk}</code></pre>"
        else:
            msg = f"<pre><code>{escaped_chunk}</code></pre>"

        # Only attach reply_markup to the final chunk
        markup = reply_markup if idx == len(chunks) - 1 else None
        await bot.send_message(
            chat_id=chat_id,
            text=msg,
            parse_mode="HTML",
            reply_markup=markup,
        )


async def on_external_task_completed(conv_info: dict, output_text: str, application) -> None:
    """Notification callback triggered by SessionMonitor when any agy turn completes."""
    title = conv_info.get("title", "Untitled Task")
    cid = conv_info.get("conversation_id", "")
    short_id = cid[:8] if cid else "unknown"

    header = f"🔔 <b>Task Completed!</b>\n<b>Session:</b> <code>{html.escape(title)}</code> (<code>{short_id}</code>)"

    keyboard = [
        [InlineKeyboardButton("💬 Continue This Session", callback_data=f"resume:{cid}")]
    ]
    markup = InlineKeyboardMarkup(keyboard)

    # Deliver to all known authorized chats
    for cid_target in list(known_chats):
        try:
            await send_chunked_code(
                chat_id=cid_target,
                bot=application.bot,
                text=output_text or "(Task finished with no text output)",
                header=header,
                reply_markup=markup,
            )
        except Exception as e:
            logger.warning("Failed to deliver completion alert to chat %s: %s", cid_target, e)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_auth_or_reject(update):
        return

    cwd = runner.current_dir or "~"
    target_display = f"{SSH_USER}@{SSH_HOST}:{SSH_PORT}" if RUN_MODE == "ssh" else "Local Machine"
    active_cid = active_conversations.get(update.effective_chat.id)
    cid_str = active_cid[:8] if active_cid else "Latest / Active"

    welcome_text = (
        f"⚡ <b>telegravity daemon ready</b>\n\n"
        f"<b>Target:</b> <code>{target_display}</code>\n"
        f"<b>Profile:</b> <code>{runner.active_profile}</code>\n"
        f"<b>Session:</b> <code>{cid_str}</code>\n"
        f"<b>Remote CWD:</b> <code>{cwd}</code>\n\n"
        f"<b>Usage:</b>\n"
        f"• <b>Send any message</b> to dispatch an agy task via mgy.\n"
        f"• <code>/profiles</code> - View and switch mgy profiles.\n"
        f"• <code>/quotas</code> - Check profile token & model quotas.\n"
        f"• <code>/conversations</code> - List recent sessions.\n"
        f"• <code>/new</code> - Start a brand new conversation.\n"
        f"• <code>/cancel</code> - Cancel ongoing task execution.\n"
        f"• <code>/cd &lt;path&gt;</code> - Change target directory.\n"
        f"• <code>/status</code> - View host and task status."
    )
    if update.message:
        await update.message.reply_html(welcome_text)


async def profiles_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_auth_or_reject(update):
        return

    profiles = await runner.list_profiles()
    if not profiles:
        if update.message:
            await update.message.reply_text("No multigravity profiles found.")
        return

    keyboard = []
    text_lines = ["👥 <b>Multigravity Profiles:</b>\n"]
    for p in profiles:
        name = p["name"]
        email = f" ({p['email']})" if p.get("email") else ""
        is_active = (name == runner.active_profile)
        prefix = "✅ " if is_active else "  • "
        text_lines.append(f"{prefix}<code>{name}</code>{email}")

        btn_text = f"✅ {name}" if is_active else f"Select {name}"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"set_profile:{name}")])

    markup = InlineKeyboardMarkup(keyboard)
    if update.message:
        await update.message.reply_html("\n".join(text_lines), reply_markup=markup)


async def quotas_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_auth_or_reject(update):
        return

    if update.message:
        await update.message.reply_chat_action(ChatAction.TYPING)

    quotas_text = await runner.get_quotas()
    await send_chunked_code(
        chat_id=update.effective_chat.id,
        bot=context.bot,
        text=quotas_text or "No quota data returned.",
        header="📊 <b>Multigravity Quotas:</b>",
    )


async def conversations_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_auth_or_reject(update):
        return

    if update.message:
        await update.message.reply_chat_action(ChatAction.TYPING)

    convs = await monitor.get_recent_conversations(limit=8)
    if not convs:
        if update.message:
            await update.message.reply_text("No recent conversations found.")
        return

    keyboard = []
    text_lines = ["📜 <b>Recent Conversations:</b>\n"]
    active_cid = active_conversations.get(update.effective_chat.id)

    for c in convs:
        cid = c.get("conversation_id", "")
        title = c.get("title", "Untitled")
        is_running = int(c.get("not_fully_idle", 0)) == 1
        status_emoji = "⚡ RUNNING" if is_running else "🟢 IDLE"
        is_selected = (cid == active_cid)
        sel_prefix = "👉 " if is_selected else ""

        text_lines.append(f"{sel_prefix}<b>{html.escape(title)}</b>\nID: <code>{cid[:8]}</code> [{status_emoji}]\n")
        keyboard.append([InlineKeyboardButton(f"Resume {title[:20]}", callback_data=f"resume:{cid}")])

    keyboard.append([InlineKeyboardButton("✨ Start Fresh (/new)", callback_data="new_session")])
    markup = InlineKeyboardMarkup(keyboard)

    if update.message:
        await update.message.reply_html("\n".join(text_lines), reply_markup=markup)


async def new_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_auth_or_reject(update):
        return

    chat_id = update.effective_chat.id
    active_conversations[chat_id] = None
    if update.message:
        await update.message.reply_html(
            f"✨ Started fresh session on profile <b>{runner.active_profile}</b>.\nNext message will create a new conversation."
        )


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_auth_or_reject(update):
        return

    code, msg = await runner.cancel_active_task()
    if update.message:
        await update.message.reply_html(f"🛑 <b>Cancel Request:</b> {html.escape(msg)}")


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_auth_or_reject(update):
        return

    target_display = f"{SSH_USER}@{SSH_HOST}:{SSH_PORT}" if RUN_MODE == "ssh" else "Local Machine"
    task_status = f"⚡ BUSY ({html.escape(runner.current_task or '')})" if runner.is_busy else "🟢 IDLE"
    active_cid = active_conversations.get(update.effective_chat.id)
    cid_str = active_cid[:8] if active_cid else "Default / Latest"

    code, sys_info, _ = await runner.run_raw("uptime && free -h 2>/dev/null || uname -a", timeout=10)

    text = (
        f"📊 <b>System & Daemon Status:</b>\n\n"
        f"• <b>Target:</b> <code>{target_display}</code>\n"
        f"• <b>Profile:</b> <code>{runner.active_profile}</code>\n"
        f"• <b>Active Conversation:</b> <code>{cid_str}</code>\n"
        f"• <b>Remote CWD:</b> <code>{runner.current_dir or '~'}</code>\n"
        f"• <b>Task State:</b> {task_status}\n\n"
        f"<b>Host Metrics:</b>\n<pre><code>{html.escape(sys_info.strip())}</code></pre>"
    )
    if update.message:
        await update.message.reply_html(text)


async def cd_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_auth_or_reject(update):
        return

    target = " ".join(context.args) if context.args else "~"
    code, out = await runner.change_dir(target)
    if code == 0 and update.message:
        await update.message.reply_html(f"📂 Changed directory to: <code>{out}</code>")
    elif update.message:
        await update.message.reply_html(f"❌ Failed to change directory: {html.escape(out)}")


async def pwd_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_auth_or_reject(update):
        return

    cwd = runner.current_dir or "~"
    code, out, _ = await runner.run_raw("pwd", timeout=5)
    if code == 0:
        cwd = out.strip()
    if update.message:
        await update.message.reply_html(f"📂 Current directory: <code>{cwd}</code>")


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()

    user_id = query.from_user.id
    if not is_authorized(user_id):
        await query.edit_message_text("⛔ Unauthorized.")
        return

    data = query.data or ""
    chat_id = query.message.chat_id if query.message else None

    if data.startswith("set_profile:"):
        new_prof = data.split(":", 1)[1]
        success = await runner.set_active_profile(new_prof)
        if success:
            await query.edit_message_text(
                f"✅ Switched active profile to <b>{new_prof}</b>.",
                parse_mode="HTML",
            )
        else:
            await query.edit_message_text(f"❌ Profile {new_prof} not found.")

    elif data.startswith("resume:"):
        cid = data.split(":", 1)[1]
        if chat_id:
            active_conversations[chat_id] = cid
        await query.edit_message_text(
            f"🔄 Pinned active conversation to <code>{cid[:8]}</code>.\nNext prompt will resume this conversation.",
            parse_mode="HTML",
        )

    elif data == "new_session":
        if chat_id:
            active_conversations[chat_id] = None
        await query.edit_message_text(
            f"✨ Started fresh session on profile <b>{runner.active_profile}</b>.",
            parse_mode="HTML",
        )

    elif data == "cancel_task":
        code, msg = await runner.cancel_active_task()
        await query.edit_message_text(f"🛑 {msg}")


async def handle_task_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Dispatches user prompt as an agy task via mgy."""
    if not await check_auth_or_reject(update):
        return

    prompt = update.message.text.strip() if update.message and update.message.text else ""
    if not prompt:
        return

    chat_id = update.effective_chat.id

    if runner.is_busy:
        if update.message:
            cancel_btn = InlineKeyboardMarkup([[InlineKeyboardButton("🛑 Cancel Running Task", callback_data="cancel_task")]])
            await update.message.reply_html(
                f"⏳ Runner is currently busy with: <code>{html.escape(runner.current_task or '')}</code>\n"
                f"Please wait for it to complete or cancel it.",
                reply_markup=cancel_btn,
            )
        return

    conv_id = active_conversations.get(chat_id)
    continue_session = (conv_id is None)

    # Initial acknowledgement
    preview_prompt = prompt[:100] + ("..." if len(prompt) > 100 else "")
    cancel_btn = InlineKeyboardMarkup([[InlineKeyboardButton("🛑 Cancel Task", callback_data="cancel_task")]])
    status_msg = await update.message.reply_html(
        f"🚀 <b>Task dispatched to {runner.active_profile}...</b>\n"
        f"<i>Prompt:</i> <code>{html.escape(preview_prompt)}</code>\n\n"
        f"⏳ Processing in background...",
        reply_markup=cancel_btn,
    )

    # Run execution in background task so Telegram remains responsive
    asyncio.create_task(
        _execute_and_reply(
            prompt=prompt,
            conv_id=conv_id,
            continue_session=continue_session,
            status_msg=status_msg,
            chat_id=chat_id,
            context=context,
        )
    )


async def _execute_and_reply(
    prompt: str,
    conv_id: Optional[str],
    continue_session: bool,
    status_msg,
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    try:
        code, out, err = await runner.dispatch_task(
            prompt=prompt,
            conversation_id=conv_id,
            continue_session=continue_session,
        )

        # Remove "Processing in background..." indicator
        try:
            await status_msg.delete()
        except:
            pass

        output = out.strip()
        if not output and err.strip():
            output = err.strip()
        if not output:
            output = "(Task completed with empty output)"

        header = f"✅ <b>Task Completed (profile: {runner.active_profile})</b>:"
        await send_chunked_code(
            chat_id=chat_id,
            bot=context.bot,
            text=output,
            header=header,
        )

    except Exception as e:
        logger.error("Error executing task: %s", e)
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"❌ <b>Error executing task:</b>\n<pre><code>{html.escape(str(e))}</code></pre>",
            parse_mode="HTML",
        )


async def post_init(application) -> None:
    """Called after application is initialized. Starts the background session monitor."""
    async def _on_complete(conv_info, output_text):
        await on_external_task_completed(conv_info, output_text, application)

    monitor.on_task_completed(_on_complete)
    asyncio.create_task(monitor.start())
    logger.info("Session monitor started in post_init.")


def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN is not configured in .env or environment!")
        print("Copy .env.example to .env and configure your bot token.")
        return

    print("Starting telegravity-cli daemon...")
    print(f"Mode: {RUN_MODE.upper()}")
    if RUN_MODE == "ssh":
        print(f"SSH Target: {SSH_USER}@{SSH_HOST}:{SSH_PORT}")
    print(f"Active Profile: {runner.active_profile}")

    request = HTTPXRequest(connect_timeout=30.0, read_timeout=30.0, write_timeout=30.0)
    app = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        .request(request)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", start_command))
    app.add_handler(CommandHandler("profiles", profiles_command))
    app.add_handler(CommandHandler("quotas", quotas_command))
    app.add_handler(CommandHandler("conversations", conversations_command))
    app.add_handler(CommandHandler("new", new_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("cd", cd_command))
    app.add_handler(CommandHandler("pwd", pwd_command))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_task_message))

    app.run_polling()


if __name__ == "__main__":
    main()
