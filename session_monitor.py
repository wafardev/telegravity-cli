import asyncio
import json
import logging
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
        self._on_task_completed_cb: Optional[Callable[[Dict[str, Any], str], Any]] = None

    def on_task_completed(self, callback: Callable[[Dict[str, Any], str], Any]) -> None:
        """Register a callback when a task finishes: callback(conv_info, output_text)"""
        self._on_task_completed_cb = callback

    async def get_recent_conversations(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Fetches recent conversations from conversation_summaries.db."""
        query_code = f"""
import sqlite3, json, os
db = os.path.expanduser("~/.gemini/antigravity-cli/conversation_summaries.db")
if not os.path.exists(db):
    print("[]")
    exit(0)
conn = sqlite3.connect(db)
conn.row_factory = sqlite3.Row
rows = conn.execute("SELECT conversation_id, title, preview, status, not_fully_idle, datetime(last_modified_time) as last_mod FROM conversation_summaries ORDER BY last_modified_time DESC LIMIT {limit}").fetchall()
print(json.dumps([dict(r) for r in rows]))
"""
        cmd = f"python3 -c '{query_code.strip()}'"
        code, out, err = await self.runner.run_raw(cmd, timeout=10)
        if code == 0 and out.strip():
            try:
                return json.loads(out.strip())
            except Exception as e:
                logger.error("Failed to parse conversation json: %s", e)
        return []

    async def get_latest_output(self, conversation_id: str) -> str:
        """Retrieves the latest PLANNER_RESPONSE from transcript.jsonl or detects quota exhaustion."""
        fetch_code = f"""
import json, os, glob, re
conv_id = "{conversation_id}"
path = os.path.expanduser(f"~/.gemini/antigravity-cli/brain/{{conv_id}}/.system_generated/logs/transcript.jsonl")
output = ""

if os.path.exists(path):
    with open(path, "r", errors="replace") as f:
        lines = f.readlines()
        for line in reversed(lines):
            try:
                data = json.loads(line)
                if data.get("type") == "PLANNER_RESPONSE" and data.get("content"):
                    output = data.get("content", "").strip()
                    break
            except:
                pass

if not output:
    log_files = sorted(glob.glob(os.path.expanduser("~/.gemini/antigravity-cli/log/cli-*.log")), key=os.path.getmtime, reverse=True)
    if log_files:
        try:
            with open(log_files[0], "r", errors="replace") as f:
                log_lines = f.readlines()
            for l in reversed(log_lines[-80:]):
                if "RESOURCE_EXHAUSTED" in l or "quota reached" in l:
                    m = re.search(r"(Individual quota reached[^\n\r]+)", l)
                    if m:
                        output = f"⚠️ {m.group(1).strip()}"
                        break
        except:
            pass

print(output)
"""
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

                    # Update state
                    self._known_states[cid] = current_idle_flag

                    # Detect transition from RUNNING (1) -> IDLE (0)
                    if prev_flag == 1 and current_idle_flag == 0:
                        logger.info("Detected task completion for conversation %s (%s)", cid, c.get("title"))
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
