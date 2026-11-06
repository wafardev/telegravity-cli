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
    BotCommand,
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

# Active session tracking per chat (chat_id -> {"profile": str, "conversation_id": Optional[str]})
chat_sessions: dict[int, dict] = {}
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
        logger.warning(f"Unauthorized access attempt ignored from user ID: {user.id}")
        return False
    if update.effective_chat:
        known_chats.add(update.effective_chat.id)
    return True


def get_chat_session(chat_id: int) -> dict:
    if chat_id not in chat_sessions:
        chat_sessions[chat_id] = {
            "profile": runner.default_profile or "default",
            "conversation_id": None,
        }
    return chat_sessions[chat_id]


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

    chat_id = update.effective_chat.id
    session = get_chat_session(chat_id)
    cwd = runner.current_dir or "~"
    target_display = f"{SSH_USER}@{SSH_HOST}:{SSH_PORT}" if RUN_MODE == "ssh" else "Local Machine"
    active_cid = session.get("conversation_id")
    cid_str = active_cid[:8] if active_cid else "Latest / Active"

    welcome_text = (
        f"⚡ <b>telegravity daemon ready</b>\n\n"
        f"<b>Target:</b> <code>{target_display}</code>\n"
        f"<b>Profile:</b> <code>{session['profile']}</code>\n"
        f"<b>Session:</b> <code>{cid_str}</code>\n"
        f"<b>Remote CWD:</b> <code>{cwd}</code>\n\n"
        f"<b>Usage:</b>\n"
        f"• <b>Send any message</b> to dispatch an agy task via mgy.\n"
        f"• <code>/list</code> - Run 'mgy list' on the device.\n"
        f"• <code>/instances</code> - List running mgy & agy process PIDs.\n"
        f"• <code>/quotas</code> - Run 'mgy quotas' on the device.\n"
        f"• <code>/conversations</code> - List recent sessions.\n"
        f"• <code>/new &lt;profile&gt; [conv]</code> - Bind session to profile & conversation.\n"
        f"• <code>/cancel [pid]</code> - Cancel running task (appends PID).\n"
        f"• <code>/kill &lt;pid&gt;</code> - Kill any process by PID.\n"
        f"• <code>/cd &lt;path&gt;</code> - Change target directory.\n"
        f"• <code>/status</code> - View host and task status."
    )
    if update.message:
        await update.message.reply_html(welcome_text)


async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Returns raw output of 'mgy list' from the device."""
    if not await check_auth_or_reject(update):
        return

    if update.message:
        await update.message.reply_chat_action(ChatAction.TYPING)

    output = await runner.get_mgy_list()
    await send_chunked_code(
        chat_id=update.effective_chat.id,
        bot=context.bot,
        text=output or "(no output from mgy list)",
        header="📋 <b>mgy list:</b>",
    )


async def instances_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Returns PIDs and command lines of active mgy/agy instances on the target device."""
    if not await check_auth_or_reject(update):
        return

    if update.message:
        await update.message.reply_chat_action(ChatAction.TYPING)

    instances = await runner.get_instances()
    if not instances:
        if update.message:
            await update.message.reply_html(
                "🔍 <b>Active Instances:</b>\n<i>No running mgy or agy instances found on target device.</i>"
            )
        return

    lines = ["🔍 <b>Active Instances:</b>\n"]
    for inst in instances:
        lines.append(f"• <b>PID:</b> <code>{inst['pid']}</code>\n  <code>{html.escape(inst['cmd'])}</code>")

    if update.message:
        await update.message.reply_html("\n".join(lines))


async def quotas_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Returns raw output of 'mgy quotas' from the device."""
    if not await check_auth_or_reject(update):
        return

    if update.message:
        await update.message.reply_chat_action(ChatAction.TYPING)

    quotas_text = await runner.get_quotas()
    await send_chunked_code(
        chat_id=update.effective_chat.id,
        bot=context.bot,
        text=quotas_text or "No quota data returned.",
        header="📊 <b>mgy quotas:</b>",
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
    sess = get_chat_session(update.effective_chat.id)
    active_cid = sess.get("conversation_id")

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
    """Binds the chat session to a profile and optional conversation ID."""
    if not await check_auth_or_reject(update):
        return

    chat_id = update.effective_chat.id
    args = context.args or []
    if not args:
        profiles = await runner.list_profiles()
        prof_names = ", ".join(f"<code>{p['name']}</code>" for p in profiles) if profiles else "None found"
        current = get_chat_session(chat_id)
        cid_display = current.get("conversation_id") or "None (starts new conversation)"
        text = (
            f"ℹ️ <b>Usage:</b> <code>/new &lt;profile&gt; [conversation_id]</code>\n\n"
            f"<b>Current Session:</b>\n"
            f"• Profile: <code>{current['profile']}</code>\n"
            f"• Conversation: <code>{cid_display}</code>\n\n"
            f"<b>Available Profiles:</b>\n{prof_names}"
        )
        if update.message:
            await update.message.reply_html(text)
        return

    profile = args[0]
    conv_id = args[1] if len(args) > 1 else None

    chat_sessions[chat_id] = {
        "profile": profile,
        "conversation_id": conv_id,
    }

    conv_info = f" with conversation <code>{conv_id}</code>" if conv_id else " (new conversation)"
    if update.message:
        await update.message.reply_html(
            f"✨ Session configured for profile <b>{profile}</b>{conv_info}.\n"
            f"Next message will execute under this profile."
        )


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancels ongoing task execution, optionally targeting a specific PID."""
    if not await check_auth_or_reject(update):
        return

    target_pid: Optional[int] = None
    if context.args:
        try:
            target_pid = int(context.args[0])
        except ValueError:
            pass

    code, pid, msg = await runner.cancel_active_task(pid=target_pid)
    pid_str = f" (PID: <code>{pid}</code>)" if pid else ""
    if code == 0 and pid:
        reply_text = f"🛑 <b>Cancelled task{pid_str}</b>"
    else:
        reply_text = f"🛑 <b>Cancel Request{pid_str}:</b> {html.escape(msg)}"

    if update.message:
        await update.message.reply_html(reply_text)


async def kill_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Kills a process by PID on the target device."""
    if not await check_auth_or_reject(update):
        return

    if not context.args:
        if update.message:
            await update.message.reply_html("⚠️ <b>Usage:</b> <code>/kill &lt;pid&gt;</code>")
        return

    try:
        pid = int(context.args[0])
    except ValueError:
        if update.message:
            await update.message.reply_html("❌ PID must be an integer.")
        return

    success, msg = await runner.kill_process(pid)
    emoji = "💀" if success else "❌"
    if update.message:
        await update.message.reply_html(f"{emoji} <b>Kill PID {pid}:</b> {html.escape(msg)}")


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_auth_or_reject(update):
        return

    chat_id = update.effective_chat.id
    session = get_chat_session(chat_id)
    target_display = f"{SSH_USER}@{SSH_HOST}:{SSH_PORT}" if RUN_MODE == "ssh" else "Local Machine"
    active_pid_str = f" (PID: <code>{runner.active_pid}</code>)" if runner.active_pid else ""
    task_status = f"⚡ BUSY{active_pid_str} ({html.escape(runner.current_task or '')})" if runner.is_busy else "🟢 IDLE"
    active_cid = session.get("conversation_id")
    cid_str = active_cid[:8] if active_cid else "Default / Latest"

    code, sys_info, _ = await runner.run_raw("uptime && free -h 2>/dev/null || uname -a", timeout=10)

    text = (
        f"📊 <b>System & Daemon Status:</b>\n\n"
        f"• <b>Target:</b> <code>{target_display}</code>\n"
        f"• <b>Profile:</b> <code>{session['profile']}</code>\n"
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
        logger.warning(f"Unauthorized callback attempt ignored from user ID: {user_id}")
        return

    data = query.data or ""
    chat_id = query.message.chat_id if query.message else None

    if data.startswith("resume:"):
        cid = data.split(":", 1)[1]
        if chat_id:
            sess = get_chat_session(chat_id)
            sess["conversation_id"] = cid
        await query.edit_message_text(
            f"🔄 Pinned active conversation to <code>{cid[:8]}</code>.\nNext prompt will resume this conversation.",
            parse_mode="HTML",
        )

    elif data == "new_session":
        if chat_id:
            sess = get_chat_session(chat_id)
            sess["conversation_id"] = None
            prof = sess["profile"]
        else:
            prof = runner.default_profile or "default"
        await query.edit_message_text(
            f"✨ Started fresh session on profile <b>{prof}</b>.",
            parse_mode="HTML",
        )

    elif data.startswith("cancel_task"):
        parts = data.split(":", 1)
        target_pid = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
        code, pid, msg = await runner.cancel_active_task(pid=target_pid)
        pid_str = f" (PID: <code>{pid}</code>)" if pid else ""
        await query.edit_message_text(f"🛑 Cancelled task{pid_str}", parse_mode="HTML")


async def handle_task_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Dispatches user prompt as an agy task via mgy."""
    if not await check_auth_or_reject(update):
        return

    prompt = update.message.text.strip() if update.message and update.message.text else ""
    if not prompt:
        return

    chat_id = update.effective_chat.id
    session = get_chat_session(chat_id)
    profile = session["profile"]

    if runner.is_busy:
        if update.message:
            pid = runner.active_pid
            btn_label = f"🛑 Cancel Running Task (PID: {pid})" if pid else "🛑 Cancel Running Task"
            cb_data = f"cancel_task:{pid}" if pid else "cancel_task"
            cancel_btn = InlineKeyboardMarkup([[InlineKeyboardButton(btn_label, callback_data=cb_data)]])
            pid_info = f" (PID: <code>{pid}</code>)" if pid else ""
            await update.message.reply_html(
                f"⏳ Runner is currently busy with{pid_info}: <code>{html.escape(runner.current_task or '')}</code>\n"
                f"Please wait for it to complete or cancel it.",
                reply_markup=cancel_btn,
            )
        return

    conv_id = session.get("conversation_id")
    continue_session = (conv_id is None)

    # Initial acknowledgement
    preview_prompt = prompt[:100] + ("..." if len(prompt) > 100 else "")
    cancel_btn = InlineKeyboardMarkup([[InlineKeyboardButton("🛑 Cancel Task", callback_data="cancel_task")]])
    status_msg = await update.message.reply_html(
        f"🚀 <b>Task dispatched to {profile}...</b>\n"
        f"<i>Prompt:</i> <code>{html.escape(preview_prompt)}</code>\n\n"
        f"⏳ Processing in background...",
        reply_markup=cancel_btn,
    )

    # Run execution in background task so Telegram remains responsive
    asyncio.create_task(
        _execute_and_reply(
            prompt=prompt,
            profile=profile,
            conv_id=conv_id,
            continue_session=continue_session,
            status_msg=status_msg,
            preview_prompt=preview_prompt,
            chat_id=chat_id,
            context=context,
        )
    )


async def _execute_and_reply(
    prompt: str,
    profile: str,
    conv_id: Optional[str],
    continue_session: bool,
    status_msg,
    preview_prompt: str,
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    try:
        async def on_start_callback(pid: int):
            try:
                cancel_btn = InlineKeyboardMarkup(
                    [[InlineKeyboardButton(f"🛑 Cancel Task (PID: {pid})", callback_data=f"cancel_task:{pid}")]]
                )
                await status_msg.edit_text(
                    f"🚀 <b>Task dispatched to {profile} (PID: <code>{pid}</code>)...</b>\n"
                    f"<i>Prompt:</i> <code>{html.escape(preview_prompt)}</code>\n\n"
                    f"⏳ Processing in background...",
                    parse_mode="HTML",
                    reply_markup=cancel_btn,
                )
            except Exception:
                pass

        code, out, err = await runner.dispatch_task(
            prompt=prompt,
            profile=profile,
            conversation_id=conv_id,
            continue_session=continue_session,
            on_start=on_start_callback,
        )

        # Remove "Processing in background..." indicator
        try:
            await status_msg.delete()
        except Exception:
            pass

        output = out.strip()
        if not output and err.strip():
            output = err.strip()
        if not output:
            output = "(Task completed with empty output)"

        header = f"✅ <b>Task Completed (profile: {profile})</b>:"
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
    """Called after application is initialized. Sets up commands and starts session monitor."""
    commands = [
        BotCommand("start", "Show status & help"),
        BotCommand("list", "Run 'mgy list' on device"),
        BotCommand("instances", "List active mgy / agy PIDs"),
        BotCommand("quotas", "View quota usage across profiles"),
        BotCommand("conversations", "List recent sessions"),
        BotCommand("new", "Bind session: /new <profile> [conv]"),
        BotCommand("cancel", "Cancel running task: /cancel [pid]"),
        BotCommand("kill", "Kill process by PID: /kill <pid>"),
        BotCommand("status", "View host & runner status"),
        BotCommand("cd", "Change remote directory"),
        BotCommand("pwd", "Print working directory"),
        BotCommand("help", "Show help and command list"),
    ]
    try:
        await application.bot.set_my_commands(commands)
        logger.info("Bot commands registered successfully with Telegram.")
    except Exception as e:
        logger.warning("Could not register bot commands with Telegram: %s", e)

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
    print(f"Default Profile: {runner.default_profile}")

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
    app.add_handler(CommandHandler("list", list_command))
    app.add_handler(CommandHandler("instances", instances_command))
    app.add_handler(CommandHandler("quotas", quotas_command))
    app.add_handler(CommandHandler("conversations", conversations_command))
    app.add_handler(CommandHandler("new", new_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CommandHandler("kill", kill_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("cd", cd_command))
    app.add_handler(CommandHandler("pwd", pwd_command))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_task_message))

    app.run_polling()


if __name__ == "__main__":
    main()
