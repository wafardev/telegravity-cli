#!/usr/bin/env python3
"""
telegravity-cli daemon & Telegram bot.
Integrates multigravity (mgy) task dispatching with real-time session completion monitoring.
"""

import asyncio
import glob
import hashlib
import html
import io
import logging
import os
import re
import time
from typing import Optional, Set, Dict
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

# Global instances - no default profile or conversation configured
runner = MgyRunner(mode=RUN_MODE, default_profile=None)
monitor = SessionMonitor(runner=runner)

# Active session tracking per chat (chat_id -> {"profile": Optional[str], "conversation_id": Optional[str]})
chat_sessions: dict[int, dict] = {}
# Known authorized chats to notify on external completion
known_chats: Set[int] = set(ALLOWED_USER_IDS)


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
            "profile": None,
            "conversation_id": None,
        }
    return chat_sessions[chat_id]


# Track prompt retries on definitive Gemini filter blocks: key -> retry_count (0 or 1)
filter_retry_tracker: Dict[str, int] = {}
# Cache last sent prompts: key (cid or pid) -> prompt string
last_sent_prompts: Dict[str, str] = {}
# Cache conversation titles: cid -> title
conv_titles_cache: Dict[str, str] = {}
# Deduplication cache: key (cid:step or cid:hash) -> delivery timestamp
delivered_responses_cache: Dict[str, float] = {}


def normalize_model_output(text: str) -> str:
    """Normalizes output text by stripping common CLI prefix wrappers."""
    if not text:
        return ""
    cleaned = re.sub(r"^Starting (?:agy|mgy) with profile [^\n]+\.\.\.\s*", "", text.strip())
    cleaned = re.sub(r"^🚀 [^\n]+\.\.\.\s*", "", cleaned)
    return cleaned.strip()


def mark_response_delivered(
    cid: Optional[str],
    step_index: Optional[int],
    content: str,
    window_seconds: float = 120.0,
) -> bool:
    """
    Checks if a response has already been delivered for this conversation/step/content.
    If already delivered within window_seconds, returns False (duplicate).
    If new, marks it as delivered and returns True.
    """
    now = time.time()
    # Expire old entries older than 300 seconds
    to_delete = [k for k, ts in delivered_responses_cache.items() if now - ts > 300]
    for k in to_delete:
        delivered_responses_cache.pop(k, None)

    keys = []
    if cid and step_index is not None and step_index >= 0:
        keys.append(f"{cid}:step:{step_index}")

    clean = normalize_model_output(content)
    if clean:
        norm_clean = "".join(clean.split())[:600]
        content_hash = hashlib.sha256(norm_clean.encode("utf-8")).hexdigest()[:16]
        if cid:
            keys.append(f"{cid}:hash:{content_hash}")
        else:
            keys.append(f"global:hash:{content_hash}")

    for k in keys:
        if k in delivered_responses_cache:
            if now - delivered_responses_cache[k] < window_seconds:
                return False  # Duplicate!

    for k in keys:
        delivered_responses_cache[k] = now
    return True


async def get_conversation_title(cid: str) -> str:
    """Retrieves conversation title/label for cid, querying database cache if not yet cached."""
    if not cid:
        return ""
    if cid in conv_titles_cache and conv_titles_cache[cid]:
        return conv_titles_cache[cid]
    try:
        convs = await monitor.get_recent_conversations(limit=30)
        for c in convs:
            c_id = c.get("conversation_id")
            c_title = c.get("title", "")
            if c_id and c_title:
                conv_titles_cache[c_id] = c_title
        if cid in conv_titles_cache:
            return conv_titles_cache[cid]
    except Exception as e:
        logger.debug("Could not fetch conversation title for %s: %s", cid, e)
    return ""


def format_task_completed_header(
    profile: Optional[str] = None,
    cid: Optional[str] = None,
    title: Optional[str] = None,
) -> str:
    """Formats a concise 'Task Completed' header with profile name and conversation."""
    parts = []
    if profile:
        parts.append(f"profile: <b>{html.escape(profile)}</b>")

    conv_disp = ""
    clean_title = (title or "").strip()
    is_generic = (
        not clean_title
        or clean_title.lower().startswith("active session")
        or clean_title.lower().startswith("untitled")
        or clean_title == cid
    )

    if not is_generic and cid:
        if cid[:8] in clean_title:
            conv_disp = html.escape(clean_title)
        else:
            conv_disp = f"<b>{html.escape(clean_title)}</b> (<code>{cid[:8]}</code>)"
    elif not is_generic:
        conv_disp = f"<b>{html.escape(clean_title)}</b>"
    elif cid:
        conv_disp = f"<code>{cid[:8]}</code>"

    if conv_disp:
        parts.append(f"conv: {conv_disp}")

    details = f" ({', '.join(parts)})" if parts else ""
    return f"✅ <b>Task Completed{details}</b>:"



async def get_last_prompt_for_target(cid: Optional[str] = None, pid: Optional[int] = None) -> str:
    """Retrieves the last prompt sent to a session or PID."""
    if cid and cid in last_sent_prompts and last_sent_prompts[cid]:
        return last_sent_prompts[cid]
    if pid and str(pid) in last_sent_prompts and last_sent_prompts[str(pid)]:
        return last_sent_prompts[str(pid)]
    if cid:
        try:
            prompt = await runner.get_last_user_prompt(cid)
            if prompt:
                return prompt
        except Exception:
            pass
    return ""


def is_gemini_filter_blocked(text: str) -> bool:
    """Checks if output contains Gemini filter block notification."""
    if not text:
        return False
    return bool(re.search(r"request was blocked by Gemini['’]?s filters", text, re.IGNORECASE))


def format_filter_blocked_message(
    cid: Optional[str],
    title: Optional[str],
    pid: Optional[int],
    profile: Optional[str],
    step_index: Optional[int],
    is_running: bool,
    is_retry: bool = False,
) -> tuple[str, str]:
    """Formats filter block notification indicating whether session is still running or definitive return."""
    pid_disp = f" (PID: <code>{pid}</code>)" if pid else ""
    prof_disp = f" (<b>{html.escape(profile)}</b>)" if profile else ""
    step_disp = f" [Step #{step_index}]" if step_index is not None else ""
    cid_disp = f" for <code>{cid}</code>" if cid else ""

    header = f"🚫 <b>Request blocked by Gemini's filters</b>{cid_disp}{pid_disp}{prof_disp}{step_disp}:"
    if title:
        header += f"\n<b>Session:</b> <i>{html.escape(title)}</i>"

    if is_running:
        status_icon = "⏳"
        status_text = "Task is still running in background."
    else:
        status_icon = "🛑"
        if is_retry:
            status_text = "Definitive return (second attempt blocked). Process kept idle (ready for next prompt)."
        else:
            status_text = "Definitive return (process halted)."
    body = f"🚫 <b>Request blocked.</b>\n{status_icon} <b>Status:</b> {status_text}"
    return header, body


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


async def send_response_or_file(
    chat_id: int,
    bot,
    text: str,
    header: str = "",
    filename: str = "response.txt",
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    max_direct_length: int = 2500,
) -> None:
    """
    Delivers response text directly in a Telegram message if <= max_direct_length characters.
    If very long, sends a preview message and attaches the complete text as a .txt file document.
    """
    if not text:
        text = "(No output text returned)"

    if len(text) <= max_direct_length:
        escaped = html.escape(text)
        msg = f"{header}\n<pre><code>{escaped}</code></pre>" if header else f"<pre><code>{escaped}</code></pre>"
        await bot.send_message(
            chat_id=chat_id,
            text=msg,
            parse_mode="HTML",
            reply_markup=reply_markup,
        )
    else:
        # Long response: send preview + document attachment
        preview_text = text[:800] + "\n\n... [truncated - see attached file for full response] ..."
        escaped_preview = html.escape(preview_text)
        preview_msg = (
            f"{header}\n\n"
            f"📄 <i>Response is long ({len(text)} characters). Full output attached as text file below.</i>\n\n"
            f"<b>Preview:</b>\n"
            f"<pre><code>{escaped_preview}</code></pre>"
        )
        await bot.send_message(
            chat_id=chat_id,
            text=preview_msg,
            parse_mode="HTML",
        )

        doc = io.BytesIO(text.encode("utf-8"))
        doc.name = filename
        await bot.send_document(
            chat_id=chat_id,
            document=doc,
            caption=f"📄 {filename} ({len(text)} characters)",
            reply_markup=reply_markup,
        )


async def on_external_task_completed(conv_info: dict, output_text: str, application) -> None:
    """Notification callback triggered by SessionMonitor when any prompt response completes."""
    title = conv_info.get("title") or "Untitled Task"
    cid = conv_info.get("conversation_id", "")
    short_id = cid[:8] if cid else "unknown"
    pid = conv_info.get("pid")
    profile = conv_info.get("profile", "")
    step_index = conv_info.get("step_index")
    retry_key = cid or (str(pid) if pid else profile)

    if is_gemini_filter_blocked(output_text):
        is_running = False
        if pid:
            is_running = await runner.is_pid_running(pid)
        if not is_running and cid:
            is_running = await runner.is_conversation_running(cid)

        if not is_running:
            # Definitive return! Check if we should retry once with the same prompt
            attempts = filter_retry_tracker.get(retry_key, 0)
            if attempts == 0:
                filter_retry_tracker[retry_key] = 1
                last_prompt = await get_last_prompt_for_target(cid=cid, pid=pid)
                if last_prompt:
                    logger.info("Definitive filter block for %s. Retrying once with prompt: %s", retry_key, last_prompt[:60])
                    for cid_target in list(known_chats):
                        try:
                            await application.bot.send_message(
                                chat_id=cid_target,
                                text=(
                                    f"🚫 <b>Request blocked by Gemini's filters</b>"
                                    f"{f' for <code>{cid[:8]}</code>' if cid else ''}"
                                    f"{f' (PID: <code>{pid}</code>)' if pid else ''}:\n\n"
                                    f"🔄 <i>Retrying once with the same prompt...</i>"
                                ),
                                parse_mode="HTML",
                            )
                        except Exception as e:
                            logger.warning("Failed to deliver retry notification: %s", e)

                    # Trigger retry without killing the process
                    if pid and await runner.has_interactive_tty(pid):
                        await runner.inject_input_to_pid(pid, last_prompt)
                    else:
                        asyncio.create_task(
                            runner.dispatch_task(
                                prompt=last_prompt,
                                profile=profile or "default",
                                conversation_id=cid,
                                continue_session=(cid is None),
                            )
                        )
                    return True

            # If second time block: keep it idle too, do NOT kill it!
            filter_retry_tracker.pop(retry_key, None)
            header, body = format_filter_blocked_message(
                cid=cid,
                title=title,
                pid=pid,
                profile=profile,
                step_index=step_index,
                is_running=False,
                is_retry=True,
            )
        else:
            # Task is still actively running in background
            header, body = format_filter_blocked_message(
                cid=cid,
                title=title,
                pid=pid,
                profile=profile,
                step_index=step_index,
                is_running=True,
                is_retry=False,
            )

        buttons = []
        if pid:
            buttons.append(InlineKeyboardButton("👁️ Peek", callback_data=f"peek:{pid}"))
            if is_running:
                buttons.append(InlineKeyboardButton("🛑 Cancel", callback_data=f"cancel_task:{pid}"))
                buttons.append(InlineKeyboardButton("💀 Kill", callback_data=f"kill_inst:{pid}"))
            else:
                buttons.append(InlineKeyboardButton("🚀 Run", callback_data=f"run_inst:{pid}"))
                buttons.append(InlineKeyboardButton("💀 Kill", callback_data=f"kill_inst:{pid}"))
        buttons.append(InlineKeyboardButton("💬 Select", callback_data=f"resume:{cid}"))
        markup = InlineKeyboardMarkup([buttons])

        delivered_any = False
        for cid_target in list(known_chats):
            try:
                await application.bot.send_message(
                    chat_id=cid_target,
                    text=f"{header}\n\n{body}",
                    parse_mode="HTML",
                    reply_markup=markup,
                )
                delivered_any = True
            except Exception as e:
                logger.warning("Failed to deliver filter block alert to chat %s: %s", cid_target, e)
        return delivered_any

    elif "Individual quota reached" in output_text or "RESOURCE_EXHAUSTED" in output_text or output_text.startswith("⚠️"):
        # ONLY kill the process when quota is depleted!
        if pid and await runner.is_pid_running(pid):
            logger.warning("Quota depleted for PID %s (%s). Terminating process.", pid, profile)
            await runner.kill_process(pid)

        header = f"🔴 <b>Quota Limit Reached!</b>\n<b>Session:</b> <code>{html.escape(title)}</code> (<code>{cid}</code>)"
        if pid:
            header += f"\n<i>Process PID <code>{pid}</code> terminated due to quota exhaustion.</i>"
        keyboard = [
            [
                InlineKeyboardButton("📊 Check Quotas", callback_data="do_quotas"),
                InlineKeyboardButton("✨ New Session", callback_data="new_session"),
            ]
        ]
    else:
        # Normal / valid response: return answer and keep process idle (do NOT kill process)
        filter_retry_tracker.pop(retry_key, None)
        if cid and title:
            conv_titles_cache[cid] = title

        # Deduplication check: if already delivered, skip duplicate
        if not mark_response_delivered(cid, step_index, output_text):
            logger.info("Task response for %s (step %s) already delivered. Skipping duplicate.", cid, step_index)
            return True

        header = format_task_completed_header(profile=profile, cid=cid, title=title)
        btn_row = [InlineKeyboardButton("💬 Select", callback_data=f"resume:{cid}")]
        if pid:
            btn_row.append(InlineKeyboardButton("👁️ Peek", callback_data=f"peek:{pid}"))
            btn_row.append(InlineKeyboardButton("🚀 Run", callback_data=f"run_inst:{pid}"))
            btn_row.append(InlineKeyboardButton("💀 Kill", callback_data=f"kill_inst:{pid}"))
        keyboard = [btn_row]
    markup = InlineKeyboardMarkup(keyboard)

    # Deliver to all known authorized chats
    delivered_any = False
    for cid_target in list(known_chats):
        try:
            await send_response_or_file(
                chat_id=cid_target,
                bot=application.bot,
                text=output_text or "(Task finished with no text output)",
                header=header,
                filename=f"response_{short_id}.txt",
                reply_markup=markup,
            )
            delivered_any = True
        except Exception as e:
            logger.warning("Failed to deliver completion alert to chat %s: %s", cid_target, e)
    return delivered_any


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_auth_or_reject(update):
        return

    chat_id = update.effective_chat.id
    session = get_chat_session(chat_id)
    cwd = runner.current_dir or "~"
    target_display = f"{SSH_USER}@{SSH_HOST}:{SSH_PORT}" if RUN_MODE == "ssh" else "Local Machine"
    prof_str = session.get("profile") or "None (select via /new or /conversations)"
    active_cid = session.get("conversation_id")
    cid_str = active_cid[:8] if active_cid else "None"

    welcome_text = (
        f"⚡ <b>telegravity daemon ready</b>\n\n"
        f"<b>Target:</b> <code>{target_display}</code>\n"
        f"<b>Profile:</b> <code>{prof_str}</code>\n"
        f"<b>Session:</b> <code>{cid_str}</code>\n"
        f"<b>Remote CWD:</b> <code>{cwd}</code>\n\n"
        f"<b>Usage:</b>\n"
        f"• <b>Send any message</b> to dispatch an agy task via mgy.\n"
        f"• /instances - List running mgy & agy process PIDs.\n"
        f"• /quotas - Get all mgy quotas per account.\n"
        f"• /accounts - List all mgy accounts on the device.\n"
        f"• /conversations - List imported conversations.\n"
        f"• /peek &lt;pid&gt; - Get latest answer or stream from instance.\n"
        f"• /run &lt;pid&gt; [prompt] - Run a prompt on a specific instance.\n"
        f"• /new &lt;profile&gt; [conv] - Bind session to profile & conversation.\n"
        f"• /cancel [pid] - Cancel running task (appends PID).\n"
        f"• /kill &lt;pid&gt; - Kill any process by PID.\n"
        f"• /cd &lt;path&gt; - Change target directory.\n"
        f"• /status - View host and task status."
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
            detail = inst.get("status_detail") or "in a prompt"
            status_text = f"🟡 <b>Running</b> <i>({html.escape(detail)})</i>"
            row = [
                InlineKeyboardButton(f"👁️ Peek {pid}", callback_data=f"peek:{pid}"),
                InlineKeyboardButton(f"🛑 Cancel {pid}", callback_data=f"cancel_task:{pid}"),
                InlineKeyboardButton(f"💀 Kill {pid}", callback_data=f"kill_inst:{pid}"),
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
    cmd = info.get("cmd", "")
    conv_id = info.get("conversation_id")
    conv_title = info.get("conversation_title")
    is_depleted = info.get("is_quota_depleted", False) or ("Individual quota reached" in content) or ("RESOURCE_EXHAUSTED" in content)

    if is_running:
        status_detail = info.get("status_detail")
        not_fully_idle = info.get("not_fully_idle")
        db_status = info.get("db_status", "")
        if "goal" in cmd or status_detail == "in a goal / autonomous loop":
            sub = "in a goal / autonomous loop"
        elif not_fully_idle or "RUNNING" in db_status or status_detail == "awaiting background task":
            sub = "in a prompt / awaiting task"
        elif status_detail:
            sub = status_detail
        else:
            sub = "in a prompt"
        status_badge = f"🟡 <b>Running</b> <i>({sub})</i>"
    elif is_depleted:
        status_badge = "🔴 <b>Quota Depleted</b> <i>(stopped: quota reached)</i>"
    else:
        status_badge = "🟢 <b>Ready / Idle</b> <i>(waiting for prompt)</i>"

    header_lines = [
        f"👁️ <b>Instance Peek (PID: <code>{pid}</code>)</b>",
        f"• <b>Status:</b> {status_badge}",
        f"• <b>Command:</b> <code>{html.escape(cmd)}</code>",
    ]
    if conv_title or conv_id:
        cid_disp = f" (<code>{conv_id[:8]}</code>)" if conv_id else ""
        header_lines.append(f"• <b>Session:</b> <code>{html.escape(conv_title or 'Active')}</code>{cid_disp}")

    header = "\n".join(header_lines)
    if is_gemini_filter_blocked(content):
        body_label = "🚫 <b>Gemini Filter Block:</b>"
        st_desc = "Task is still running in background." if is_running else "Definitive return (process halted)."
        display_text = f"Request was blocked by Gemini's filters.\nStatus: {st_desc}"
    else:
        body_label = "⏳ <b>Streaming output / Working:</b>" if is_running else "💬 <b>Recent Conversation Context:</b>"
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
        keyboard.append([
            InlineKeyboardButton("🛑 Cancel Task", callback_data=f"cancel_task:{pid}"),
            InlineKeyboardButton("💀 Kill Process", callback_data=f"kill_inst:{pid}"),
        ])
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


async def _wait_for_injected_response(
    pid: int,
    profile: str,
    prompt: str,
    initial_step: int,
    initial_content: str,
    initial_cid: Optional[str],
    status_msg,
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Waits for the interactive instance to complete its turns and delivers responses as they are produced."""
    poll_count = 0
    max_idle_polls = 450  # 450 * 0.8s = 360 seconds of complete inactivity
    last_seen_step = initial_step
    last_delivered_content = initial_content
    responses_delivered = 0

    if initial_cid:
        last_sent_prompts[initial_cid] = prompt
    last_sent_prompts[str(pid)] = prompt
    retry_key = initial_cid or str(pid)

    while poll_count < max_idle_polls:
        await asyncio.sleep(0.8)
        poll_count += 1

        info = await runner.peek_instance(pid)
        if not info.get("found"):
            # Check if terminated due to quota limit before reporting termination
            quota_err = ""
            try:
                cand_logs = (
                    glob.glob(os.path.expanduser("~/.gemini/antigravity-cli/log/cli-*.log")) +
                    glob.glob(os.path.expanduser("~/.config/multigravity-profiles/*/.gemini/antigravity-cli/log/cli-*.log"))
                )
                if cand_logs:
                    latest_log = max(cand_logs, key=os.path.getmtime)
                    if (time.time() - os.path.getmtime(latest_log)) < 180:
                        with open(latest_log, "r", errors="replace") as lf:
                            for line in reversed(lf.readlines()[-80:]):
                                if "RESOURCE_EXHAUSTED" in line or "quota reached" in line:
                                    m_q = re.search(r"(Individual quota reached[^\n\r]+)", line)
                                    if m_q:
                                        quota_err = m_q.group(1).strip()
                                        break
            except Exception:
                pass

            if quota_err:
                logger.warning("Quota error detected on terminated PID %s. Terminating any lingering processes.", pid)
                await runner.kill_process(pid)
                markup = InlineKeyboardMarkup([[InlineKeyboardButton("📊 Check Quotas", callback_data="do_quotas")]])
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"🔴 <b>Quota Limit Reached on PID <code>{pid}</code> (<b>{html.escape(profile)}</b>)!</b>\n"
                        f"<i>Process terminated due to quota exhaustion.</i>\n\n"
                        f"<pre><code>{html.escape(quota_err)}</code></pre>"
                    ),
                    parse_mode="HTML",
                    reply_markup=markup,
                )
            else:
                try:
                    await status_msg.edit_text(
                        f"⌨️ <b>PID <code>{pid}</code> terminated.</b>\n<b>Prompt:</b> <code>{html.escape(prompt[:80])}</code>",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
            return

        is_depleted = info.get("is_quota_depleted", False)
        content = info.get("content", "").strip()
        status = info.get("transcript_status", "")
        curr_step = info.get("step_index", -1)
        cid = info.get("conversation_id") or initial_cid
        is_running = info.get("is_running", False)

        if cid:
            retry_key = cid
            last_sent_prompts[cid] = prompt

        if is_depleted or "Individual quota reached" in content or "RESOURCE_EXHAUSTED" in content:
            # Quota exhausted: ONLY kill the process when quota is depleted!
            logger.warning("Quota depleted on PID %s (%s). Terminating process.", pid, profile)
            await runner.kill_process(pid)
            try:
                markup = InlineKeyboardMarkup([[InlineKeyboardButton("📊 Check Quotas", callback_data="do_quotas")]])
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"🔴 <b>Quota Limit Reached on PID <code>{pid}</code> (<b>{html.escape(profile)}</b>)!</b>\n"
                        f"<i>Process terminated due to quota exhaustion.</i>\n\n"
                        f"<pre><code>{html.escape(content)}</code></pre>"
                    ),
                    parse_mode="HTML",
                    reply_markup=markup,
                )
            except Exception:
                pass
            return

        # Check if new model response has arrived
        new_step_completed = (
            (curr_step > last_seen_step and curr_step != -1) or
            (content and content != last_delivered_content and not content.startswith("[Thinking]") and not content.startswith("[Tool Call]"))
        )

        final_text = info.get("raw_latest_model") or content
        if new_step_completed and status == "completed" and final_text and final_text != "(Waiting for first output from prompt...)" and final_text != last_delivered_content:
            last_seen_step = curr_step
            last_delivered_content = final_text
            poll_count = 0  # Reset idle timeout since progress is actively being made!

            # Atomically claim the step so we don't deliver duplicate messages if SessionMonitor also claimed it
            step_claimed = True
            if cid and curr_step != -1:
                step_claimed = monitor.claim_step(cid, curr_step)

            if step_claimed:
                responses_delivered += 1
                reply_buttons = [
                    [
                        InlineKeyboardButton(f"🚀 Run {pid}", callback_data=f"run_inst:{pid}"),
                        InlineKeyboardButton(f"👁️ Peek {pid}", callback_data=f"peek:{pid}"),
                        InlineKeyboardButton(f"🛑 Cancel {pid}", callback_data=f"cancel_task:{pid}"),
                        InlineKeyboardButton(f"💀 Kill {pid}", callback_data=f"kill_inst:{pid}"),
                    ]
                ]
                markup = InlineKeyboardMarkup(reply_buttons)
                turn_label = f" (Turn #{responses_delivered})" if responses_delivered > 1 else ""

                if is_gemini_filter_blocked(final_text):
                    if not is_running:
                        # Definitive return! Check if we should retry once with same prompt
                        attempts = filter_retry_tracker.get(retry_key, 0)
                        if attempts == 0:
                            filter_retry_tracker[retry_key] = 1
                            logger.info("Definitive filter block on PID %s. Retrying once with same prompt...", pid)
                            await context.bot.send_message(
                                chat_id=chat_id,
                                text=(
                                    f"🚫 <b>Request blocked by Gemini's filters</b> (PID: <code>{pid}</code>).\n\n"
                                    f"🔄 <i>Retrying once with the same prompt...</i>"
                                ),
                                parse_mode="HTML",
                            )
                            # Retry typing prompt into PID without killing process
                            if await runner.is_pid_running(pid) and await runner.has_interactive_tty(pid):
                                await runner.inject_input_to_pid(pid, prompt)
                                # Reset delivered marker to continue watching for retry output
                                last_delivered_content = ""
                                poll_count = 0
                                continue
                            else:
                                asyncio.create_task(
                                    runner.dispatch_task(
                                        prompt=prompt,
                                        profile=profile,
                                        conversation_id=cid,
                                        continue_session=(cid is None),
                                    )
                                )
                                return
                        else:
                            filter_retry_tracker.pop(retry_key, None)
                            f_hdr, f_body = format_filter_blocked_message(
                                cid=cid,
                                title="",
                                pid=pid,
                                profile=profile,
                                step_index=curr_step if curr_step != -1 else None,
                                is_running=False,
                                is_retry=True,
                            )
                            await context.bot.send_message(
                                chat_id=chat_id,
                                text=f"{f_hdr}\n\n{f_body}",
                                parse_mode="HTML",
                                reply_markup=markup,
                            )
                    else:
                        f_hdr, f_body = format_filter_blocked_message(
                            cid=cid,
                            title="",
                            pid=pid,
                            profile=profile,
                            step_index=curr_step if curr_step != -1 else None,
                            is_running=True,
                            is_retry=False,
                        )
                        await context.bot.send_message(
                            chat_id=chat_id,
                            text=f"{f_hdr}\n\n{f_body}",
                            parse_mode="HTML",
                            reply_markup=markup,
                        )
                else:
                    # Normal / valid response: deliver response and keep process idle (do NOT kill process)
                    filter_retry_tracker.pop(retry_key, None)
                    if not mark_response_delivered(cid, curr_step, final_text):
                        logger.info("Response for %s (step %s) already delivered. Skipping duplicate.", cid, curr_step)
                    else:
                        conv_title = info.get("conversation_title")
                        if cid and conv_title:
                            conv_titles_cache[cid] = conv_title
                        header = format_task_completed_header(profile=profile, cid=cid, title=conv_title)
                        await send_response_or_file(
                            chat_id=chat_id,
                            bot=context.bot,
                            text=final_text,
                            header=header,
                            filename=f"response_{cid[:8] if cid else pid}.txt",
                            reply_markup=markup,
                        )
            else:
                logger.info("Response for %s (step %s) already delivered by SessionMonitor.", cid, curr_step)

            # Update the status message
            try:
                action_buttons = [
                    [
                        InlineKeyboardButton(f"👁️ Peek {pid}", callback_data=f"peek:{pid}"),
                        InlineKeyboardButton(f"🛑 Cancel {pid}", callback_data=f"cancel_task:{pid}"),
                        InlineKeyboardButton(f"💀 Kill {pid}", callback_data=f"kill_inst:{pid}"),
                    ]
                ]
                if is_running:
                    st_text = (
                        f"⌨️ <b>Injected prompt into PID <code>{pid}</code></b> (<b>{html.escape(profile)}</b>)\n"
                        f"<b>Prompt:</b> <code>{html.escape(prompt[:100])}</code>\n\n"
                        f"🔄 <i>Update delivered! Task is still running in background...</i>"
                    )
                else:
                    st_text = (
                        f"⌨️ <b>Injected prompt into PID <code>{pid}</code></b> (<b>{html.escape(profile)}</b>)\n"
                        f"<b>Prompt:</b> <code>{html.escape(prompt[:100])}</code>\n\n"
                        f"✅ <i>Response received (Instance is ready / idle)!</i>"
                    )
                await status_msg.edit_text(st_text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(action_buttons))
            except Exception:
                pass

            # If the instance has finished all execution and returned to idle prompt, stop watching
            if not is_running:
                return

        # If the instance returned to idle after delivering at least one response, we can stop watching
        if responses_delivered > 0 and not is_running:
            return


async def _dispatch_to_pid_or_spawn(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    pid: int,
    prompt: str,
    profile: str,
    cid: Optional[str],
) -> None:
    """Injects prompt directly into PID's interactive prompt if attached to a TTY, else falls back to background dispatch."""
    preview_prompt = prompt[:100] + ("..." if len(prompt) > 100 else "")
    has_tty = await runner.has_interactive_tty(pid)

    if has_tty:
        if update.message:
            await update.message.reply_chat_action(ChatAction.TYPING)

        initial_info = await runner.peek_instance(pid)
        init_step = initial_info.get("step_index", -1)
        init_content = initial_info.get("content", "")
        init_cid = initial_info.get("conversation_id") or cid

        success, msg = await runner.inject_input_to_pid(pid, prompt)
        if success:
            action_buttons = [
                [
                    InlineKeyboardButton(f"👁️ Peek {pid}", callback_data=f"peek:{pid}"),
                    InlineKeyboardButton(f"🛑 Cancel {pid}", callback_data=f"cancel_task:{pid}"),
                    InlineKeyboardButton(f"💀 Kill {pid}", callback_data=f"kill_inst:{pid}"),
                ]
            ]
            text = (
                f"⌨️ <b>Injected prompt into PID <code>{pid}</code></b> (<b>{html.escape(profile)}</b>)\n"
                f"<b>Prompt:</b> <code>{html.escape(preview_prompt)}</code>\n\n"
                f"⏳ <i>Waiting for response...</i>"
            )
            markup = InlineKeyboardMarkup(action_buttons)
            if update.message:
                status_msg = await update.message.reply_html(text, reply_markup=markup)
            else:
                status_msg = await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=markup)

            asyncio.create_task(
                _wait_for_injected_response(
                    pid=pid,
                    profile=profile,
                    prompt=prompt,
                    initial_step=init_step,
                    initial_content=init_content,
                    initial_cid=init_cid,
                    status_msg=status_msg,
                    chat_id=chat_id,
                    context=context,
                )
            )
        else:
            err_text = f"❌ <b>Failed to type into PID <code>{pid}</code>:</b> {html.escape(msg)}"
            if update.message:
                await update.message.reply_html(err_text)
            else:
                await context.bot.send_message(chat_id=chat_id, text=err_text, parse_mode="HTML")
    else:
        # Check if the target PID is already actively running or has an ongoing prompt
        instances = await runner.get_instances()
        target_inst = next((i for i in instances if str(i.get("pid")) == str(pid)), None)
        is_target_running = target_inst.get("is_running", False) if target_inst else False

        if is_target_running or (cid and any(i.get("conversation_id") == cid and i.get("is_running") for i in instances)):
            warn_text = (
                f"⚠️ <b>PID <code>{pid}</code> ({profile}) is already actively executing a task.</b>\n\n"
                f"<i>Cannot inject prompts into a headless running instance, and running another task on this session concurrently would corrupt the conversation.</i>\n\n"
                f"Use /peek <code>{pid}</code> to follow progress or /cancel to stop it."
            )
            markup = InlineKeyboardMarkup([[InlineKeyboardButton(f"👁️ Peek {pid}", callback_data=f"peek:{pid}")]])
            if update.message:
                await update.message.reply_html(warn_text, reply_markup=markup)
            else:
                await context.bot.send_message(chat_id=chat_id, text=warn_text, parse_mode="HTML", reply_markup=markup)
            return

        # Fallback for non-interactive / headless PID: dispatch background task
        cancel_btn = InlineKeyboardMarkup([[
            InlineKeyboardButton(f"🛑 Cancel Task (PID: {pid})", callback_data=f"cancel_task:{pid}"),
            InlineKeyboardButton(f"💀 Kill (PID: {pid})", callback_data=f"kill_inst:{pid}"),
        ]])
        msg_obj = update.message if update.message else None
        if msg_obj:
            status_msg = await msg_obj.reply_html(
                f"🚀 <b>Task dispatched to {profile} (Target PID: <code>{pid}</code>)...</b>\n"
                f"<i>Prompt:</i> <code>{html.escape(preview_prompt)}</code>\n\n"
                f"⏳ Processing in background...",
                reply_markup=cancel_btn,
            )
        else:
            status_msg = await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"🚀 <b>Task dispatched to {profile} (Target PID: <code>{pid}</code>)...</b>\n"
                    f"<i>Prompt:</i> <code>{html.escape(preview_prompt)}</code>\n\n"
                    f"⏳ Processing in background..."
                ),
                parse_mode="HTML",
                reply_markup=cancel_btn,
            )

        asyncio.create_task(
            _execute_and_reply(
                prompt=prompt,
                profile=profile,
                conv_id=cid,
                continue_session=(cid is None),
                status_msg=status_msg,
                preview_prompt=preview_prompt,
                chat_id=chat_id,
                context=context,
            )
        )


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

        if len(instances) == 1:
            inst = instances[0]
            pid = int(inst["pid"])
            prof = inst.get("profile", runner.default_profile or "default")
            cid = inst.get("conversation_id")
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
                    f"<i>Send the prompt to insert into this instance:</i>",
                    reply_markup=cancel_btn,
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
        await _dispatch_to_pid_or_spawn(
            update=update,
            context=context,
            chat_id=chat_id,
            pid=pid,
            prompt=prompt,
            profile=prof,
            cid=cid,
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
                f"<i>Send the prompt to insert into this instance:</i>",
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
        prof_disp = current.get("profile") or "None"
        text = (
            f"ℹ️ <b>Usage:</b> <code>/new &lt;profile&gt; [conversation_id]</code>\n\n"
            f"<b>Current Session:</b>\n"
            f"• Profile: <code>{prof_disp}</code>\n"
            f"• Conversation: <code>{cid_display}</code>\n\n"
            f"<b>Select a profile to start a fresh session:</b>"
        )
        buttons = []
        row = []
        for p in profiles:
            p_name = p["name"]
            row.append(InlineKeyboardButton(p_name, callback_data=f"bind_new_prof:{p_name}"))
            if len(row) == 2:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
        buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_prof_select")])

        if update.message:
            await update.message.reply_html(text, reply_markup=InlineKeyboardMarkup(buttons))
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
            f"✨ Session configured for profile <b>{html.escape(profile)}</b>{conv_info}.\n"
            f"Next message will execute under this profile."
        )


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Interrupts ongoing task execution via Escape (Esc key), keeping the process alive."""
    if not await check_auth_or_reject(update):
        return

    chat_id = update.effective_chat.id if update.effective_chat else None
    sess = get_chat_session(chat_id) if chat_id else {}
    target_pid: Optional[int] = None
    if context.args:
        try:
            target_pid = int(context.args[0])
        except ValueError:
            pass

    code, pid, msg = await runner.cancel_active_task(pid=target_pid, conversation_id=sess.get("conversation_id"))
    pid_str = f" (PID: <code>{pid}</code>)" if pid else ""
    markup = None
    if pid:
        markup = InlineKeyboardMarkup([[
            InlineKeyboardButton(f"👁️ Peek {pid}", callback_data=f"peek:{pid}"),
            InlineKeyboardButton(f"💀 Kill {pid}", callback_data=f"kill_inst:{pid}"),
        ]])
    if code == 0 and pid:
        reply_text = f"🛑 <b>Interrupted task{pid_str}</b> (sent Escape key; process kept alive)"
    else:
        reply_text = f"🛑 <b>Cancel Request{pid_str}:</b> {html.escape(msg)}"

    if update.message:
        await update.message.reply_html(reply_text, reply_markup=markup)


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
    prof_str = session.get("profile") or "None (select via /new or /conversations)"
    active_cid = session.get("conversation_id")
    cid_str = active_cid[:8] if active_cid else "None"
    running_count = len(runner.active_tasks)
    active_pid_str = f" (PID: <code>{runner.active_pid}</code>)" if runner.active_pid else ""
    task_status = f"⚡ BUSY ({running_count} active){active_pid_str}" if running_count > 0 else "🟢 IDLE"

    code, sys_info, _ = await runner.run_raw("uptime && free -h 2>/dev/null || uname -a", timeout=10)

    text = (
        f"📊 <b>System & Daemon Status:</b>\n\n"
        f"• <b>Target:</b> <code>{target_display}</code>\n"
        f"• <b>Profile:</b> <code>{prof_str}</code>\n"
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


async def conversations_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Lists imported conversations, showing whether they are currently used by an instance."""
    if not await check_auth_or_reject(update):
        return

    if update.message:
        await update.message.reply_chat_action(ChatAction.TYPING)

    # Fetch imported conversations and running instances concurrently
    convs, instances = await asyncio.gather(
        runner.get_imported_conversations(),
        runner.get_instances(),
    )

    if not convs:
        text = "📁 <b>No imported conversations found on the device.</b>\n<i>Only externally imported archives are listed here.</i>"
        if update.message:
            await update.message.reply_html(text)
        return

    # Map conversation_id -> instance
    active_instances: Dict[str, Dict[str, Any]] = {}
    for inst in instances:
        c_id = inst.get("conversation_id")
        if c_id:
            active_instances[c_id] = inst

    lines = ["📁 <b>Imported Conversations:</b>"]
    keyboard = []

    for c in convs:
        cid = c["id"]
        title = html.escape(c.get("title", "(untitled)"))
        steps = c.get("step_count", 0)
        size_mb = c.get("total_size_mb", 0)
        inst = active_instances.get(cid)

        if inst:
            p = inst.get("pid")
            prof = html.escape(inst.get("profile", "default"))
            st_line = f"🔴 <b>In Use</b> (PID <code>{p}</code> — <b>{prof}</b>)"
            keyboard.append([InlineKeyboardButton(f"🔴 Select {cid}", callback_data=f"resume:{cid}")])
            keyboard.append([InlineKeyboardButton(f"👁️ Peek PID {p} ({prof})", callback_data=f"peek:{p}")])
        else:
            st_line = "🟢 <b>Not Used</b> (Idle)"
            keyboard.append([InlineKeyboardButton(f"🟢 Select {cid}", callback_data=f"resume:{cid}")])

        lines.append(
            f"• <b><code>{cid}</code></b> — {size_mb} MB ({steps:,} steps)\n"
            f"  Status: {st_line}\n"
            f"  Prompt: <i>{title}</i>"
        )

    text = "\n\n".join(lines)
    markup = InlineKeyboardMarkup(keyboard)

    if update.message:
        await update.message.reply_html(text, reply_markup=markup)


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

    if data.startswith("resume:") or data.startswith("choose_profile_for_conv:"):
        cid = data.split(":", 1)[1]
        context.user_data["selected_conv_id"] = cid
        if chat_id:
            sess = get_chat_session(chat_id)
            sess["conversation_id"] = cid

        profiles = await runner.list_profiles()
        if not profiles:
            context.user_data["pending_run"] = {"profile": "default", "conversation_id": cid, "pid": None}
            cancel_btn = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel Prompt", callback_data="cancel_pending_run")]])
            await query.edit_message_text(
                f"🔄 Active conversation set to <code>{cid[:8]}</code>.\n\n"
                f"💬 <i>Send the prompt you want to execute:</i>",
                parse_mode="HTML",
                reply_markup=cancel_btn,
            )
            return

        buttons = []
        row = []
        for p in profiles:
            p_name = p["name"]
            row.append(InlineKeyboardButton(p_name, callback_data=f"sel_conv_prof:{p_name}"))
            if len(row) == 2:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
        buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_prof_select")])

        await query.edit_message_text(
            f"👤 <b>Select an account / profile to run conversation <code>{cid[:8]}</code>:</b>\n\n"
            f"<i>Choose which mgy profile to use:</i>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    elif data.startswith("sel_conv_prof:") or data.startswith("bind_conv_prof:"):
        parts = data.split(":")
        prof_name = parts[-1]
        cid = context.user_data.pop("selected_conv_id", None)
        if not cid and len(parts) > 2:
            cid = parts[1]
        if chat_id:
            sess = get_chat_session(chat_id)
            if not cid:
                cid = sess.get("conversation_id")
            sess["conversation_id"] = cid
            sess["profile"] = prof_name

        context.user_data["pending_run"] = {
            "profile": prof_name,
            "conversation_id": cid,
            "pid": None,
        }
        cancel_btn = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel Prompt", callback_data="cancel_pending_run")]])
        cid_disp = f" (<code>{cid[:8]}</code>)" if cid else ""
        await query.edit_message_text(
            f"🚀 <b>Session Configured!</b>\n"
            f"• <b>Account:</b> <b>{html.escape(prof_name)}</b>\n"
            f"• <b>Conversation:</b>{cid_disp}\n\n"
            f"💬 <i>Send the prompt you want to execute on this session:</i>",
            parse_mode="HTML",
            reply_markup=cancel_btn,
        )

    elif data.startswith("select_prompt_prof:"):
        prof_name = data.split(":", 1)[1]
        if chat_id:
            sess = get_chat_session(chat_id)
            sess["profile"] = prof_name
        pending_prompt = context.user_data.pop("pending_prompt", None)
        if pending_prompt and chat_id:
            conv_id = sess.get("conversation_id")
            preview_prompt = pending_prompt[:100] + ("..." if len(pending_prompt) > 100 else "")
            cancel_btn = InlineKeyboardMarkup([[InlineKeyboardButton("🛑 Cancel Task", callback_data="cancel_task")]])
            status_msg = await query.message.reply_html(
                f"🚀 <b>Task dispatched to {html.escape(prof_name)}...</b>\n"
                f"<i>Prompt:</i> <code>{html.escape(preview_prompt)}</code>\n\n"
                f"⏳ Processing in background...",
                reply_markup=cancel_btn,
            )
            asyncio.create_task(
                _execute_and_reply(
                    prompt=pending_prompt,
                    profile=prof_name,
                    conv_id=conv_id,
                    continue_session=(conv_id is None),
                    status_msg=status_msg,
                    preview_prompt=preview_prompt,
                    chat_id=chat_id,
                    context=context,
                )
            )
            await query.edit_message_text(f"✅ Active profile set to <b>{html.escape(prof_name)}</b> and prompt dispatched!", parse_mode="HTML")
        else:
            await query.edit_message_text(f"✅ Active profile set to <b>{html.escape(prof_name)}</b>.", parse_mode="HTML")

    elif data == "cancel_prof_select":
        context.user_data.pop("pending_prompt", None)
        await query.edit_message_text("❌ Selection cancelled.")

    elif data == "new_session":
        profiles = await runner.list_profiles()
        buttons = []
        row = []
        for p in profiles:
            p_name = p["name"]
            row.append(InlineKeyboardButton(p_name, callback_data=f"bind_new_prof:{p_name}"))
            if len(row) == 2:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
        buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_prof_select")])
        await query.edit_message_text(
            "👤 <b>Select a profile for the new session:</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    elif data.startswith("bind_new_prof:"):
        prof_name = data.split(":", 1)[1]
        if chat_id:
            sess = get_chat_session(chat_id)
            sess["conversation_id"] = None
            sess["profile"] = prof_name
        context.user_data["pending_run"] = {
            "profile": prof_name,
            "conversation_id": None,
            "pid": None,
        }
        cancel_btn = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel Prompt", callback_data="cancel_pending_run")]])
        await query.edit_message_text(
            f"✨ <b>Fresh Session on {html.escape(prof_name)}!</b>\n\n"
            f"💬 <i>Send the prompt you want to execute:</i>",
            parse_mode="HTML",
            reply_markup=cancel_btn,
        )

    elif data == "do_quotas":
        quotas_text = await runner.get_quotas()
        await send_chunked_code(
            chat_id=chat_id,
            bot=context.bot,
            text=quotas_text or "No quota data returned.",
            header="📊 <b>mgy quotas:</b>",
        )

    elif data == "do_accounts":
        output = await runner.get_mgy_list()
        await send_chunked_code(
            chat_id=chat_id,
            bot=context.bot,
            text=output or "(no accounts found from mgy list)",
            header="👥 <b>mgy accounts:</b>",
        )

    elif data == "do_status":
        session = get_chat_session(chat_id)
        target_display = f"{SSH_USER}@{SSH_HOST}:{SSH_PORT}" if RUN_MODE == "ssh" else "Local Machine"
        prof_str = session.get("profile") or "None (select via /new or /conversations)"
        active_cid = session.get("conversation_id")
        cid_str = active_cid[:8] if active_cid else "None"
        running_count = len(runner.active_tasks)
        active_pid_str = f" (PID: <code>{runner.active_pid}</code>)" if runner.active_pid else ""
        task_status = f"⚡ BUSY ({running_count} active){active_pid_str}" if running_count > 0 else "🟢 IDLE"
        code, sys_info, _ = await runner.run_raw("uptime && free -h 2>/dev/null || uname -a", timeout=10)
        text = (
            f"📊 <b>System & Daemon Status:</b>\n\n"
            f"• <b>Target:</b> <code>{target_display}</code>\n"
            f"• <b>Profile:</b> <code>{prof_str}</code>\n"
            f"• <b>Active Conversation:</b> <code>{cid_str}</code>\n"
            f"• <b>Remote CWD:</b> <code>{runner.current_dir or '~'}</code>\n"
            f"• <b>Task State:</b> {task_status}\n\n"
            f"<b>Host Metrics:</b>\n<pre><code>{html.escape(sys_info.strip())}</code></pre>"
        )
        await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")

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
            action_row = []
            if pid:
                action_row.append(InlineKeyboardButton(f"👁️ Peek {pid}", callback_data=f"peek:{pid}"))
                action_row.append(InlineKeyboardButton(f"💀 Kill {pid}", callback_data=f"kill_inst:{pid}"))
            markup_after = InlineKeyboardMarkup([action_row]) if action_row else None
            await query.edit_message_text(
                f"🛑 <b>Interrupted task{pid_str}</b> (sent Escape key, process kept alive)",
                parse_mode="HTML",
                reply_markup=markup_after,
            )


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
        if target_pid:
            await _dispatch_to_pid_or_spawn(
                update=update,
                context=context,
                chat_id=chat_id,
                pid=target_pid,
                prompt=prompt,
                profile=profile,
                cid=conv_id,
            )
            return
        cid_short = f" (conv: <code>{conv_id[:8]}</code>)" if conv_id else ""
        dispatch_label = f"Task dispatched to {profile}{cid_short}..."
    else:
        session = get_chat_session(chat_id)
        profile = session.get("profile")
        conv_id = session.get("conversation_id")
        cid_short = f" (conv: <code>{conv_id[:8]}</code>)" if conv_id else ""
        dispatch_label = f"Task dispatched to {profile}{cid_short}..."

    # If no profile is selected, ask user to choose which profile to run with
    if not profile:
        context.user_data["pending_prompt"] = prompt
        profiles = await runner.list_profiles()
        buttons = []
        row = []
        for p in profiles:
            p_name = p["name"]
            row.append(InlineKeyboardButton(p_name, callback_data=f"select_prompt_prof:{p_name}"))
            if len(row) == 2:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
        buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_prof_select")])
        if update.message:
            await update.message.reply_html(
                "👤 <b>No profile selected.</b>\n"
                "Please select an account / profile to run this prompt with:",
                reply_markup=InlineKeyboardMarkup(buttons),
            )
        return

    # Check if this specific conversation is already actively running
    if conv_id and await runner.is_conversation_running(conv_id):
        instances = await runner.get_instances()
        target = next((i for i in instances if i.get("conversation_id") == conv_id and i.get("is_running")), None)
        c_pid = target.get("pid") if target else None
        btn_label = f"🛑 Cancel Task (PID: {c_pid})" if c_pid else "🛑 Cancel Task"
        cb_data = f"cancel_task:{c_pid}" if c_pid else "cancel_task"
        action_row = [InlineKeyboardButton(btn_label, callback_data=cb_data)]
        if c_pid:
            action_row.append(InlineKeyboardButton(f"💀 Kill ({c_pid})", callback_data=f"kill_inst:{c_pid}"))
        cancel_btn = InlineKeyboardMarkup([action_row])
        pid_info = f" (PID: <code>{c_pid}</code>)" if c_pid else ""
        if update.message:
            await update.message.reply_html(
                f"⏳ Conversation <code>{conv_id[:8]}</code> is already executing a task{pid_info}.\n"
                f"Please wait for it to complete or cancel it before sending another prompt to this conversation.",
                reply_markup=cancel_btn,
            )
        return

    # If this conversation has an existing idle instance waiting for a prompt, inject into it directly
    if conv_id:
        last_sent_prompts[conv_id] = prompt
        instances = await runner.get_instances()
        target_inst = next((i for i in instances if i.get("conversation_id") == conv_id and not i.get("is_running") and not i.get("is_quota_depleted")), None)
        if target_inst:
            target_pid = int(target_inst["pid"])
            last_sent_prompts[str(target_pid)] = prompt
            await _dispatch_to_pid_or_spawn(
                update=update,
                context=context,
                chat_id=chat_id,
                pid=target_pid,
                prompt=prompt,
                profile=target_inst.get("profile") or profile,
                cid=conv_id,
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
                    [[
                        InlineKeyboardButton(f"🛑 Cancel Task (PID: {pid})", callback_data=f"cancel_task:{pid}"),
                        InlineKeyboardButton(f"💀 Kill (PID: {pid})", callback_data=f"kill_inst:{pid}"),
                    ]]
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

        if conv_id:
            last_sent_prompts[conv_id] = prompt
        retry_key = conv_id or profile

        if is_gemini_filter_blocked(output):
            retries = filter_retry_tracker.get(retry_key, 0)
            if retries == 0:
                filter_retry_tracker[retry_key] = 1
                logger.info("Definitive filter block for %s in _execute_and_reply. Retrying once...", retry_key)
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"🚫 <b>Request blocked by Gemini's filters</b>"
                        f"{f' for <code>{conv_id[:8]}</code>' if conv_id else ''}"
                        f" (<b>{html.escape(profile)}</b>).\n\n"
                        f"🔄 <i>Retrying once with the same prompt...</i>"
                    ),
                    parse_mode="HTML",
                )
                return await _execute_and_reply(
                    prompt=prompt,
                    profile=profile,
                    conv_id=conv_id,
                    continue_session=continue_session,
                    status_msg=status_msg,
                    preview_prompt=preview_prompt,
                    chat_id=chat_id,
                    context=context,
                )
            else:
                filter_retry_tracker.pop(retry_key, None)
                header, body_text = format_filter_blocked_message(
                    cid=conv_id,
                    title="",
                    pid=None,
                    profile=profile,
                    step_index=None,
                    is_running=False,
                    is_retry=True,
                )
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"{header}\n\n{body_text}",
                    parse_mode="HTML",
                )
        elif "Individual quota reached" in output or "RESOURCE_EXHAUSTED" in output:
            header = f"🔴 <b>Quota Limit Reached (profile: {profile})</b>:\n<i>Task stopped due to quota exhaustion.</i>"
            await send_response_or_file(
                chat_id=chat_id,
                bot=context.bot,
                text=output,
                header=header,
                filename=f"response_{profile}.txt",
            )
        else:
            filter_retry_tracker.pop(retry_key, None)
            clean_output = normalize_model_output(output)
            if not mark_response_delivered(conv_id, None, clean_output):
                logger.info("Task completion for %s already delivered by SessionMonitor. Skipping duplicate.", conv_id)
                return

            title = await get_conversation_title(conv_id) if conv_id else ""
            header = format_task_completed_header(profile=profile, cid=conv_id, title=title)
            btn_row = []
            if conv_id:
                btn_row.append(InlineKeyboardButton("💬 Select", callback_data=f"resume:{conv_id}"))
            markup = InlineKeyboardMarkup([btn_row]) if btn_row else None
            await send_response_or_file(
                chat_id=chat_id,
                bot=context.bot,
                text=clean_output,
                header=header,
                filename=f"response_{conv_id[:8] if conv_id else profile}.txt",
                reply_markup=markup,
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
        BotCommand("conversations", "List imported conversations"),
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


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exception while handling an update: %s", context.error, exc_info=context.error)


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

    app.add_error_handler(error_handler)
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", start_command))
    app.add_handler(CommandHandler("accounts", accounts_command))
    app.add_handler(CommandHandler("list", accounts_command))
    app.add_handler(CommandHandler("instances", instances_command))
    app.add_handler(CommandHandler("quotas", quotas_command))
    app.add_handler(CommandHandler("conversations", conversations_command))
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
    app.add_handler(MessageHandler(filters.TEXT, handle_task_message))

    app.run_polling()


if __name__ == "__main__":
    main()
