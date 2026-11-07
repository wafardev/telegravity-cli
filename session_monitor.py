import asyncio
import glob
import json
import logging
import os
import re
import sqlite3
from typing import Callable, Optional, Dict, List, Any
from mgy_runner import MgyRunner
from config import POLL_INTERVAL

logger = logging.getLogger(__name__)


class SessionMonitor:
    """Monitors agy/mgy conversation state and detects task completion events."""

    def __init__(self, runner: MgyRunner, poll_interval: int = POLL_INTERVAL):
        self.runner = runner
        self.poll_interval = poll_interval
        self._running = False
        self._known_states: Dict[str, int] = {}  # conv_id -> not_fully_idle (1 or 0)
        self._known_steps: Dict[str, int] = {}   # conv_id -> step_count
        self._handled_conv_steps: Dict[str, int] = {}  # conv_id -> latest handled step_index
        self._on_task_completed_cb: Optional[Callable[[Dict[str, Any], str], Any]] = None

    def mark_handled(self, conversation_id: str, step_index: int) -> None:
        """Marks a conversation step as already delivered to user chat to prevent duplicate alerts."""
        if conversation_id:
            self._handled_conv_steps[conversation_id] = max(self._handled_conv_steps.get(conversation_id, -1), step_index)

    def is_handled(self, conversation_id: str, step_index: int) -> bool:
        """Returns True if the step was already delivered."""
        return self._handled_conv_steps.get(conversation_id, -1) >= step_index

    def on_task_completed(self, callback: Callable[[Dict[str, Any], str], Any]) -> None:
        """Register a callback when a task finishes: callback(conv_info, output_text)"""
        self._on_task_completed_cb = callback

    @staticmethod
    def _fetch_recent_conversations_sync(limit: int) -> List[Dict[str, Any]]:
        dbs = [os.path.expanduser("~/.gemini/antigravity-cli/conversation_summaries.db")] + glob.glob(
            os.path.expanduser("~/.config/multigravity-profiles/*/.gemini/antigravity-cli/conversation_summaries.db")
        )
        all_rows = []
        for db in dbs:
            if os.path.exists(db):
                try:
                    conn = sqlite3.connect(db)
                    conn.row_factory = sqlite3.Row
                    rows = conn.execute(
                        f"SELECT conversation_id, title, preview, step_count, status, not_fully_idle, datetime(last_modified_time) as last_mod FROM conversation_summaries ORDER BY last_modified_time DESC LIMIT {limit}"
                    ).fetchall()
                    all_rows.extend([dict(r) for r in rows])
                except Exception:
                    pass
        all_rows.sort(key=lambda r: r.get("last_mod") or "", reverse=True)
        return all_rows[:limit]

    @staticmethod
    def _fetch_latest_output_sync(conversation_id: str) -> str:
        cand = [
            os.path.expanduser(f"~/.gemini/antigravity-cli/brain/{conversation_id}/.system_generated/logs/transcript.jsonl")
        ] + glob.glob(
            os.path.expanduser(f"~/.config/multigravity-profiles/*/.gemini/antigravity-cli/brain/{conversation_id}/.system_generated/logs/transcript.jsonl")
        )
        path = next((p for p in cand if os.path.exists(p)), None)
        output = ""

        if path and os.path.exists(path):
            try:
                with open(path, "r", errors="replace") as f:
                    lines = f.readlines()
                for line in reversed(lines):
                    try:
                        data = json.loads(line)
                        if data.get("type") == "PLANNER_RESPONSE" and data.get("content"):
                            output = data.get("content", "").strip()
                            break
                    except Exception:
                        pass
            except Exception:
                pass

        if not output:
            cand_logs = glob.glob(os.path.expanduser("~/.gemini/antigravity-cli/log/cli-*.log")) + glob.glob(
                os.path.expanduser("~/.config/multigravity-profiles/*/.gemini/antigravity-cli/log/cli-*.log")
            )
            log_files = sorted([f for f in cand_logs if os.path.exists(f)], key=os.path.getmtime, reverse=True)
            if log_files:
                try:
                    with open(log_files[0], "r", errors="replace") as f:
                        log_lines = f.readlines()
                    for l in reversed(log_lines[-80:]):
                        if "RESOURCE_EXHAUSTED" in l or "quota reached" in l:
                            match_quota = re.search(r"(Individual quota reached[^\n\r]+)", l)
                            if match_quota:
                                output = "⚠️ " + match_quota.group(1).strip()
                                break
                except Exception:
                    pass

        return output

    async def get_recent_conversations(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Fetches recent conversations from conversation_summaries.db across all profiles."""
        if self.runner.mode == "local":
            return await asyncio.to_thread(self._fetch_recent_conversations_sync, limit)

        # SSH mode execution
        import base64
        script = (
            "import sqlite3, json, os, glob\n"
            f"dbs = [os.path.expanduser('~/.gemini/antigravity-cli/conversation_summaries.db')] + glob.glob(os.path.expanduser('~/.config/multigravity-profiles/*/.gemini/antigravity-cli/conversation_summaries.db'))\n"
            "all_rows = []\n"
            "for db in dbs:\n"
            "    if os.path.exists(db):\n"
            "        try:\n"
            "            conn = sqlite3.connect(db); conn.row_factory = sqlite3.Row\n"
            f"            rows = conn.execute('SELECT conversation_id, title, preview, step_count, status, not_fully_idle, datetime(last_modified_time) as last_mod FROM conversation_summaries ORDER BY last_modified_time DESC LIMIT {limit}').fetchall()\n"
            "            all_rows.extend([dict(r) for r in rows])\n"
            "        except: pass\n"
            "all_rows.sort(key=lambda r: r.get('last_mod') or '', reverse=True)\n"
            f"print(json.dumps(all_rows[:{limit}]))\n"
        )
        b64 = base64.b64encode(script.encode("utf-8")).decode("ascii")
        code, out, _ = await self.runner.run_raw(f"python3 -c \"import base64; exec(base64.b64decode('{b64}').decode('utf-8'))\"", timeout=10)
        if code == 0 and out.strip():
            try:
                return json.loads(out.strip())
            except Exception:
                pass
        return []

    async def get_latest_output(self, conversation_id: str) -> str:
        """Retrieves the latest PLANNER_RESPONSE from transcript.jsonl or detects quota exhaustion."""
        if self.runner.mode == "local":
            return await asyncio.to_thread(self._fetch_latest_output_sync, conversation_id)

        # SSH mode execution
        import base64
        script = (
            "import json, os, glob, re\n"
            f"conv_id = '{conversation_id}'\n"
            "cand = [os.path.expanduser(f'~/.gemini/antigravity-cli/brain/{conv_id}/.system_generated/logs/transcript.jsonl')] + glob.glob(os.path.expanduser(f'~/.config/multigravity-profiles/*/.gemini/antigravity-cli/brain/{conv_id}/.system_generated/logs/transcript.jsonl'))\n"
            "path = next((p for p in cand if os.path.exists(p)), None)\n"
            "output = ''\n"
            "if path and os.path.exists(path):\n"
            "    try:\n"
            "        with open(path, 'r', errors='replace') as f: lines = f.readlines()\n"
            "        for l in reversed(lines):\n"
            "            data = json.loads(l)\n"
            "            if data.get('type') == 'PLANNER_RESPONSE' and data.get('content'):\n"
            "                output = data.get('content', '').strip(); break\n"
            "    except: pass\n"
            "if not output:\n"
            "    cand_logs = glob.glob(os.path.expanduser('~/.gemini/antigravity-cli/log/cli-*.log')) + glob.glob(os.path.expanduser('~/.config/multigravity-profiles/*/.gemini/antigravity-cli/log/cli-*.log'))\n"
            "    for lf in sorted([f for f in cand_logs if os.path.exists(f)], key=os.path.getmtime, reverse=True)[:1]:\n"
            "        try:\n"
            "            with open(lf, 'r', errors='replace') as f: lines = f.readlines()\n"
            "            for l in reversed(lines[-80:]):\n"
            "                if 'RESOURCE_EXHAUSTED' in l or 'quota reached' in l:\n"
            "                    m = re.search(r'(Individual quota reached[^\n\r]+)', l)\n"
            "                    if m: output = '⚠️ ' + m.group(1).strip(); break\n"
            "        except: pass\n"
            "print(output)\n"
        )
        b64 = base64.b64encode(script.encode("utf-8")).decode("ascii")
        code, out, _ = await self.runner.run_raw(f"python3 -c \"import base64; exec(base64.b64decode('{b64}').decode('utf-8'))\"", timeout=10)
        return out.strip() if code == 0 else ""
        cmd = f"python3 -c '{fetch_code.strip()}'"
        code, out, _ = await self.runner.run_raw(cmd, timeout=10)
        return out.strip() if code == 0 else ""

    async def start(self) -> None:
        """Starts the background monitoring loop."""
        self._running = True
        logger.info("Starting SessionMonitor background watcher...")

        # Initialize known states on startup without triggering false alerts
        initial_convs = await self.get_recent_conversations(limit=20)
        for c in initial_convs:
            cid = c.get("conversation_id")
            if cid:
                self._known_states[cid] = int(c.get("not_fully_idle", 0))
                self._known_steps[cid] = int(c.get("step_count", 0))
                self._handled_conv_steps[cid] = int(c.get("step_count", 0))

        while self._running:
            try:
                await asyncio.sleep(self.poll_interval)
                convs = await self.get_recent_conversations(limit=15)

                for c in convs:
                    cid = c.get("conversation_id")
                    if not cid:
                        continue

                    current_idle_flag = int(c.get("not_fully_idle", 0))
                    prev_flag = self._known_states.get(cid)
                    self._known_states[cid] = current_idle_flag

                    curr_steps = int(c.get("step_count", 0))
                    prev_steps = self._known_steps.get(cid)
                    self._known_steps[cid] = curr_steps

                    # Completion detected if:
                    # 1. State transitioned from running (1) -> idle (0)
                    # 2. Or step_count incremented and current state is idle (0)
                    step_incremented = (prev_steps is not None and curr_steps > prev_steps)
                    state_finished = (prev_flag == 1 and current_idle_flag == 0)

                    if (state_finished or step_incremented) and current_idle_flag == 0:
                        if not self.is_handled(cid, curr_steps):
                            self.mark_handled(cid, curr_steps)
                            logger.info("Detected task completion for conversation %s (%s, step: %s)", cid, c.get("title"), curr_steps)
                            latest_output = await self.get_latest_output(cid)
                            if not latest_output:
                                latest_output = c.get("preview", "")

                            if self._on_task_completed_cb:
                                try:
                                    res = self._on_task_completed_cb(c, latest_output)
                                    if asyncio.iscoroutine(res):
                                        await res
                                except Exception as cb_err:
                                    logger.error("Error in on_task_completed callback: %s", cb_err)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Error in session monitor loop: %s", e)
                await asyncio.sleep(self.poll_interval)

    def stop(self) -> None:
        self._running = False
