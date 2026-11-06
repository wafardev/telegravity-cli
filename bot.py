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

    if "Individual quota reached" in output_text or "RESOURCE_EXHAUSTED" in output_text:
        header = f"⚠️ <b>Quota Limit Reached!</b>\n<b>Session:</b> <code>{html.escape(title)}</code> (<code>{short_id}</code>)"
        keyboard = [
            [
                InlineKeyboardButton("📊 Check Quotas", callback_data="do_quotas"),
                InlineKeyboardButton("✨ New Session", callback_data="new_session"),
            ]
        ]
    else:
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
        f"• <code>/accounts</code> - List all mgy accounts on the device.\n"
        f"• <code>/instances</code> - List running mgy & agy process PIDs.\n"
        f"• <code>/quotas</code> - Get all mgy quotas per account.\n"
        f"• <code>/peek &lt;pid&gt;</code> - Get latest answer or stream from instance.\n"
        f"• <code>/run &lt;pid&gt; [prompt]</code> - Run a prompt on a specific instance.\n"
        f"• <code>/new &lt;profile&gt; [conv]</code> - Bind session to profile & conversation.\n"
        f"• <code>/cancel [pid]</code> - Cancel running task (appends PID).\n"
        f"• <code>/kill &lt;pid&gt;</code> - Kill any process by PID.\n"
        f"• <code>/cd &lt;path&gt;</code> - Change target directory.\n"
        f"• <code>/status</code> - View host and task status."
    )
    if update.message:
        await update.message.reply_html(welcome_text)


async def accounts_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Returns raw output of 'mgy list' from the device (lists accounts)."""
    if not await check_auth_or_reject(update):
        return

    if update.message:
        await update.message.reply_chat_action(ChatAction.TYPING)

    output = await runner.get_mgy_list()
    await send_chunked_code(
        chat_id=update.effective_chat.id,
        bot=context.bot,
        text=output or "(no accounts found from mgy list)",
        header="👥 <b>mgy accounts:</b>",
    )


def format_instances_view(instances: list[dict]) -> tuple[str, InlineKeyboardMarkup]:
    """Formats the instances list with status indicators and interactive action buttons."""
    if not instances:
        text = "🔍 <b>Active Instances:</b>\n\n<i>No running mgy or agy instances found on target device.</i>"
        keyboard = [[InlineKeyboardButton("🔄 Refresh", callback_data="refresh_instances")]]
        return text, InlineKeyboardMarkup(keyboard)

    lines = ["🔍 <b>Active Instances:</b>\n"]
    keyboard = []

    for inst in instances:
        pid = inst["pid"]
        cmd = inst["cmd"]
        is_running = inst.get("is_running", False)
        is_depleted = inst.get("is_quota_depleted", False) or inst.get("status") == "quota_depleted"
        reset_time = inst.get("reset_time")

        if is_running:
            status_text = "🟡 <b>Running</b> <i>(in a prompt)</i>"
            row = [
                InlineKeyboardButton(f"👁️ Peek {pid}", callback_data=f"peek:{pid}"),
                InlineKeyboardButton(f"🛑 Cancel {pid}", callback_data=f"cancel_task:{pid}"),
            ]
        elif is_depleted:
            reset_desc = f" <i>(resets in {reset_time})</i>" if reset_time else ""
            status_text = f"🔴 <b>Quota Depleted</b>{reset_desc}"
            row = [
                InlineKeyboardButton(f"👁️ Peek {pid}", callback_data=f"peek:{pid}"),
                InlineKeyboardButton(f"🚀 Run {pid}", callback_data=f"run_inst:{pid}"),
                InlineKeyboardButton(f"💀 Kill {pid}", callback_data=f"kill_inst:{pid}"),
            ]
        else:
            status_text = "🟢 <b>Ready</b> <i>(waiting for a prompt)</i>"
            row = [
                InlineKeyboardButton(f"👁️ Peek {pid}", callback_data=f"peek:{pid}"),
                InlineKeyboardButton(f"🚀 Run {pid}", callback_data=f"run_inst:{pid}"),
                InlineKeyboardButton(f"💀 Kill {pid}", callback_data=f"kill_inst:{pid}"),
            ]

        lines.append(
            f"• <b>PID:</b> <code>{pid}</code> — {status_text}\n"
            f"  <code>{html.escape(cmd)}</code>\n"
        )
        keyboard.append(row)

    keyboard.append([InlineKeyboardButton("🔄 Refresh Instances", callback_data="refresh_instances")])
    return "\n".join(lines), InlineKeyboardMarkup(keyboard)


async def instances_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Returns PIDs, commands, and live status (ready/running) of active mgy/agy instances."""
    if not await check_auth_or_reject(update):
        return

    if update.message:
        await update.message.reply_chat_action(ChatAction.TYPING)

    instances = await runner.get_instances()
    text, markup = format_instances_view(instances)

    if update.message:
        await update.message.reply_html(text, reply_markup=markup)


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


def format_peek_view(info: dict) -> tuple[str, InlineKeyboardMarkup]:
    """Formats the peek result with status, session info, and latest answer or stream."""
    pid = info.get("pid")
    if not info.get("found"):
        msg = f"❌ <b>Process {pid} not found:</b> {html.escape(info.get('error', 'Instance terminated or not running.'))}"
        keyboard = [[InlineKeyboardButton("📋 View Instances", callback_data="refresh_instances")]]
        return msg, InlineKeyboardMarkup(keyboard)

    is_running = info.get("is_running", False)
    content = info.get("content", "")
    is_depleted = info.get("is_quota_depleted", False) or ("Individual quota reached" in content) or ("RESOURCE_EXHAUSTED" in content)

    if is_running:
        status_badge = "🟡 <b>Running / Streaming</b> <i>(in a prompt)</i>"
    elif is_depleted:
        status_badge = "🔴 <b>Quota Depleted</b> <i>(stopped: quota reached)</i>"
    else:
        status_badge = "🟢 <b>Ready / Idle</b> <i>(waiting for prompt)</i>"
    cmd = info.get("cmd", "")
    conv_id = info.get("conversation_id")
    conv_title = info.get("conversation_title")

    header_lines = [
        f"👁️ <b>Instance Peek (PID: <code>{pid}</code>)</b>",
        f"• <b>Status:</b> {status_badge}",
        f"• <b>Command:</b> <code>{html.escape(cmd)}</code>",
    ]
    if conv_title or conv_id:
        cid_disp = f" (<code>{conv_id[:8]}</code>)" if conv_id else ""
        header_lines.append(f"• <b>Session:</b> <code>{html.escape(conv_title or 'Active')}</code>{cid_disp}")

    header = "\n".join(header_lines)
    body_label = "⏳ <b>Streaming output (last sentences):</b>" if is_running else "💬 <b>Latest answer:</b>"

    display_text = content.strip() if content else "(No output captured yet)"
    if len(display_text) > 3200:
        display_text = "..." + display_text[-3000:]

    full_msg = f"{header}\n\n{body_label}\n<pre><code>{html.escape(display_text)}</code></pre>"

    row1 = [
        InlineKeyboardButton("🔄 Refresh", callback_data=f"peek:{pid}"),
        InlineKeyboardButton("🚀 Run", callback_data=f"run_inst:{pid}"),
    ]
    keyboard = [row1]
    if is_running:
        keyboard.append([InlineKeyboardButton("🛑 Cancel Task", callback_data=f"cancel_task:{pid}")])
    else:
        keyboard.append([InlineKeyboardButton("💀 Kill Process", callback_data=f"kill_inst:{pid}")])

    keyboard.append([InlineKeyboardButton("📋 Back to Instances", callback_data="refresh_instances")])

    return full_msg, InlineKeyboardMarkup(keyboard)


async def peek_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Gets the latest answer or streaming output from an instance PID."""
    if not await check_auth_or_reject(update):
        return

    pid: Optional[int] = None
    if context.args:
        try:
            pid = int(context.args[0])
        except ValueError:
            if update.message:
                await update.message.reply_html("❌ PID must be an integer: <code>/peek &lt;pid&gt;</code>")
            return
    else:
        # If no PID provided, peek active task or single instance if only one exists
        if runner.active_pid:
            pid = runner.active_pid
        else:
            instances = await runner.get_instances()
            if len(instances) == 1:
                pid = int(instances[0]["pid"])
            elif len(instances) > 1:
                keyboard = []
                for inst in instances:
                    p = inst["pid"]
                    st = "🟡 Running" if inst.get("is_running") else "🟢 Ready"
                    keyboard.append([InlineKeyboardButton(f"{st} PID {p} ({inst['cmd'][:20]})", callback_data=f"peek:{p}")])
                if update.message:
                    await update.message.reply_html(
                        "🔍 <b>Multiple instances found.</b> Select an instance to peek:",
                        reply_markup=InlineKeyboardMarkup(keyboard),
                    )
                return
            else:
                if update.message:
                    await update.message.reply_html(
                        "⚠️ <b>Usage:</b> <code>/peek &lt;pid&gt;</code>\n<i>No active mgy or agy instances running.</i>"
                    )
                return

    if update.message:
        await update.message.reply_chat_action(ChatAction.TYPING)

    info = await runner.peek_instance(pid)
    text, markup = format_peek_view(info)

    if update.message:
        await update.message.reply_html(text, reply_markup=markup)


async def run_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs a prompt on a specific instance: /run <pid> [prompt]"""
    if not await check_auth_or_reject(update):
        return

    chat_id = update.effective_chat.id

    if not context.args:
        instances = await runner.get_instances()
        if not instances:
            if update.message:
                await update.message.reply_html(
                    "⚠️ <b>Usage:</b> <code>/run &lt;pid&gt; [prompt]</code>\n<i>No active mgy or agy instances running.</i>"
                )
            return

        keyboard = []
        for inst in instances:
            p = inst["pid"]
            prof = inst.get("profile", "default")
            st = "🟡" if inst.get("is_running") else ("🔴" if inst.get("is_quota_depleted") else "🟢")
            keyboard.append([InlineKeyboardButton(f"{st} Run on PID {p} ({prof})", callback_data=f"run_inst:{p}")])

        if update.message:
            await update.message.reply_html(
                "🚀 <b>Select an instance to run a prompt on:</b>",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
        return

    try:
        pid = int(context.args[0])
    except ValueError:
        if update.message:
            await update.message.reply_html("❌ PID must be an integer: <code>/run &lt;pid&gt; [prompt]</code>")
        return

    instances = await runner.get_instances()
    inst = next((i for i in instances if str(i.get("pid")) == str(pid)), None)
    prof = inst.get("profile", runner.default_profile or "default") if inst else (runner.default_profile or "default")
    cid = inst.get("conversation_id") if inst else None

    prompt = " ".join(context.args[1:]).strip() if len(context.args) > 1 else ""
    if prompt:
        chat_sessions[chat_id] = {
            "profile": prof,
            "conversation_id": cid,
        }
        preview_prompt = prompt[:100] + ("..." if len(prompt) > 100 else "")
        cancel_btn = InlineKeyboardMarkup([[InlineKeyboardButton("🛑 Cancel Task", callback_data=f"cancel_task:{pid}")]])
        status_msg = await update.message.reply_html(
            f"🚀 <b>Task dispatched to {prof} (Target PID: <code>{pid}</code>)...</b>\n"
            f"<i>Prompt:</i> <code>{html.escape(preview_prompt)}</code>\n\n"
            f"⏳ Processing in background...",
            reply_markup=cancel_btn,
        )
        asyncio.create_task(
            _execute_and_reply(
                prompt=prompt,
                profile=prof,
                conv_id=cid,
                continue_session=(cid is None),
                status_msg=status_msg,
                preview_prompt=preview_prompt,
                chat_id=chat_id,
                context=context,
            )
        )
    else:
        context.user_data["pending_run"] = {
            "pid": pid,
            "profile": prof,
            "conversation_id": cid,
        }
        cancel_btn = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel Prompt", callback_data="cancel_pending_run")]])
        cid_disp = f" (conv: <code>{cid[:8]}</code>)" if cid else ""
        if update.message:
            await update.message.reply_html(
                f"🚀 <b>Run on PID <code>{pid}</code></b> (profile: <b>{prof}</b>{cid_disp})\n\n"
                f"<i>Send the prompt to insert:</i>",
                reply_markup=cancel_btn,
            )



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

    elif data == "do_quotas":
        quotas_text = await runner.get_quotas()
        await send_chunked_code(
            chat_id=chat_id,
            bot=context.bot,
            text=quotas_text or "No quota data returned.",
            header="📊 <b>mgy quotas:</b>",
        )

    elif data.startswith("instance_ready:"):
        pid = data.split(":", 1)[1]
        await query.answer(f"🟢 Instance {pid} is ready (waiting for a prompt).", show_alert=True)

    elif data.startswith("quota_info:"):
        pid = data.split(":", 1)[1]
        instances = await runner.get_instances()
        target = next((i for i in instances if str(i.get("pid")) == str(pid)), None)
        rst = target.get("reset_time") if target else None
        rst_str = f" Resets in {rst}." if rst else ""
        await query.answer(f"🔴 Instance {pid} stopped: Quota reached.{rst_str}\nSwitch accounts with /new or check /quotas.", show_alert=True)

    elif data.startswith("run_inst:"):
        pid_str = data.split(":", 1)[1]
        try:
            pid = int(pid_str)
            instances = await runner.get_instances()
            inst = next((i for i in instances if str(i.get("pid")) == str(pid)), None)
            if inst and inst.get("is_running"):
                await query.answer(f"⚠️ Instance {pid} is currently busy running a prompt. Cancel it first.", show_alert=True)
                return

            prof = inst.get("profile", runner.default_profile or "default") if inst else (runner.default_profile or "default")
            cid = inst.get("conversation_id") if inst else None

            context.user_data["pending_run"] = {
                "pid": pid,
                "profile": prof,
                "conversation_id": cid,
            }

            cancel_btn = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel Prompt", callback_data="cancel_pending_run")]])
            cid_disp = f" (conv: <code>{cid[:8]}</code>)" if cid else ""
            await query.message.reply_html(
                f"🚀 <b>Run on PID <code>{pid}</code></b> (profile: <b>{prof}</b>{cid_disp})\n\n"
                f"<i>Send the prompt to insert:</i>",
                reply_markup=cancel_btn,
            )
            await query.answer()
        except Exception as e:
            await query.answer(f"Error: {e}")

    elif data.startswith("kill_inst:"):
        pid_str = data.split(":", 1)[1]
        try:
            pid = int(pid_str)
            success, msg = await runner.kill_process(pid)
            await query.answer(f"💀 Process {pid}: {msg}", show_alert=True)
            await asyncio.sleep(0.4)
            # Refresh view
            orig_text = query.message.text or "" if query.message else ""
            if "Active Instances" in orig_text:
                instances = await runner.get_instances()
                text, markup = format_instances_view(instances)
                try:
                    await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
                except Exception:
                    pass
            elif "Instance Peek" in orig_text:
                info = await runner.peek_instance(pid)
                text, markup = format_peek_view(info)
                try:
                    await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
                except Exception:
                    pass
        except Exception as e:
            await query.answer(f"Error killing PID {pid_str}: {e}")

    elif data == "cancel_pending_run":
        context.user_data.pop("pending_run", None)
        await query.edit_message_text("❌ Prompt insertion cancelled.")

    elif data.startswith("peek:"):
        pid_str = data.split(":", 1)[1]
        try:
            p_val = int(pid_str)
            info = await runner.peek_instance(p_val)
            text, markup = format_peek_view(info)
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
        except Exception as e:
            await query.answer(f"Error: {e}")

    elif data == "refresh_instances":
        instances = await runner.get_instances()
        text, markup = format_instances_view(instances)
        try:
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
        except Exception:
            pass
        await query.answer("Refreshed instances!")

    elif data.startswith("cancel_task"):
        parts = data.split(":", 1)
        target_pid = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
        code, pid, msg = await runner.cancel_active_task(pid=target_pid)
        pid_str = f" (PID: <code>{pid}</code>)" if pid else ""

        # If clicked from instances view, refresh the instances view directly
        orig_text = query.message.text or "" if query.message else ""
        if "Active Instances" in orig_text:
            await asyncio.sleep(0.5)
            instances = await runner.get_instances()
            text, markup = format_instances_view(instances)
            try:
                await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
            except Exception:
                await query.edit_message_text(f"🛑 Cancelled task{pid_str}", parse_mode="HTML")
        else:
            await query.edit_message_text(f"🛑 Cancelled task{pid_str}", parse_mode="HTML")


async def handle_task_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Dispatches user prompt as an agy task via mgy."""
    if not await check_auth_or_reject(update):
        return

    prompt = update.message.text.strip() if update.message and update.message.text else ""
    if not prompt:
        return

    chat_id = update.effective_chat.id

    pending = context.user_data.pop("pending_run", None)
    if pending:
        profile = pending["profile"]
        conv_id = pending.get("conversation_id")
        target_pid = pending.get("pid")
        chat_sessions[chat_id] = {
            "profile": profile,
            "conversation_id": conv_id,
        }
        dispatch_label = f"Task dispatched to {profile} (Target PID: <code>{target_pid}</code>)..."
    else:
        session = get_chat_session(chat_id)
        profile = session["profile"]
        conv_id = session.get("conversation_id")
        dispatch_label = f"Task dispatched to {profile}..."

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

    continue_session = (conv_id is None)

    # Initial acknowledgement
    preview_prompt = prompt[:100] + ("..." if len(prompt) > 100 else "")
    cancel_btn = InlineKeyboardMarkup([[InlineKeyboardButton("🛑 Cancel Task", callback_data="cancel_task")]])
    status_msg = await update.message.reply_html(
        f"🚀 <b>{dispatch_label}</b>\n"
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

        if "Individual quota reached" in output or "RESOURCE_EXHAUSTED" in output:
            header = f"⚠️ <b>Quota Limit Reached (profile: {profile})</b>:"
        else:
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
        BotCommand("accounts", "List all mgy accounts on the device."),
        BotCommand("instances", "List active mgy / agy PIDs and status"),
        BotCommand("quotas", "Get all mgy quotas per account."),
        BotCommand("peek", "Get latest answer/stream: /peek <pid>"),
        BotCommand("run", "Run prompt on instance: /run <pid> [prompt]"),
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
    app.add_handler(CommandHandler("accounts", accounts_command))
    app.add_handler(CommandHandler("list", accounts_command))
    app.add_handler(CommandHandler("instances", instances_command))
    app.add_handler(CommandHandler("quotas", quotas_command))
    app.add_handler(CommandHandler("peek", peek_command))
    app.add_handler(CommandHandler("tail", peek_command))
    app.add_handler(CommandHandler("output", peek_command))
    app.add_handler(CommandHandler("run", run_command))
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
