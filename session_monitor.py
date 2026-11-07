import asyncio
import base64
import glob
import json
import logging
import os
import re
import sqlite3
import time
from typing import Callable, Optional, Dict, List, Any, Set
from mgy_runner import MgyRunner
from config import POLL_INTERVAL

logger = logging.getLogger(__name__)


def _poll_scan_sync(handled_map: Dict[str, List[int]], handled_quotas: List[str], last_mtimes: Dict[str, float] = None) -> Dict[str, Any]:
    """
    Sync scanning function that runs locally or remotely over SSH.
    Discovers all candidate conversations, detects new PLANNER_RESPONSE prompt responses,
    and detects recent quota exhaustion in CLI logs.
    """
    if last_mtimes is None:
        last_mtimes = {}
    new_mtimes = dict(last_mtimes)

    # 1. Discover all candidate conversations from DBs across all profiles
    dbs = [os.path.expanduser("~/.gemini/antigravity-cli/conversation_summaries.db")] + glob.glob(
        os.path.expanduser("~/.config/multigravity-profiles/*/.gemini/antigravity-cli/conversation_summaries.db")
    )
    conv_map: Dict[str, Dict[str, Any]] = {}
    for db in dbs:
        if os.path.exists(db):
            try:
                conn = sqlite3.connect(db)
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT conversation_id, title, preview, step_count, status, not_fully_idle, datetime(last_modified_time) as last_mod FROM conversation_summaries ORDER BY last_modified_time DESC LIMIT 25"
                ).fetchall()
                for r in rows:
                    cid = r["conversation_id"]
                    if cid and cid not in conv_map:
                        conv_map[cid] = dict(r)
            except Exception:
                pass

    # 2. Discover running instances from /proc to get active PIDs and profiles
    running_pids: Dict[str, Dict[str, Any]] = {}
    if os.path.exists("/proc"):
        try:
            for p in glob.glob("/proc/[0-9]*"):
                pid = os.path.basename(p)
                try:
                    with open(f"{p}/cmdline", "rb") as f:
                        cmdline = f.read().decode("utf-8", errors="replace").replace("\x00", " ")
                    if "agy" in cmdline or "mgy" in cmdline:
                        m_cid = re.search(r"--conversation[=\s]+([^\s]+)", cmdline)
                        m_prof = re.search(r"multigravity-profiles/([^/]+)/", cmdline)
                        if not m_prof:
                            m_mgy = re.search(r"mgy\s+([a-zA-Z0-9_\-]+)", cmdline)
                            prof = m_mgy.group(1) if m_mgy else "default"
                        else:
                            prof = m_prof.group(1)
                        if m_cid:
                            cid = m_cid.group(1)
                            running_pids[cid] = {"pid": int(pid), "profile": prof}
                except Exception:
                    pass
        except Exception:
            pass

    for cid, info in running_pids.items():
        if cid in conv_map:
            conv_map[cid]["pid"] = info["pid"]
            conv_map[cid]["profile"] = info["profile"]
        else:
            conv_map[cid] = {
                "conversation_id": cid,
                "title": f"Active Session ({cid[:8]})",
                "pid": info["pid"],
                "profile": info["profile"],
                "not_fully_idle": 1,
            }

    # 3. Scan transcripts of candidate conversations for prompt responses
    new_responses = []
    all_known_steps: Dict[str, List[int]] = {}

    for cid, cinfo in conv_map.items():
        cand = [
            os.path.expanduser(f"~/.gemini/antigravity-cli/brain/{cid}/.system_generated/logs/transcript.jsonl")
        ] + glob.glob(
            os.path.expanduser(f"~/.config/multigravity-profiles/*/.gemini/antigravity-cli/brain/{cid}/.system_generated/logs/transcript.jsonl")
        )
        tpath = next((p for p in cand if os.path.exists(p)), None)
        if not tpath:
            continue

        m_prof = re.search(r"multigravity-profiles/([^/]+)/", tpath)
        t_profile = m_prof.group(1) if m_prof else (cinfo.get("profile") or "default")

        curr_mtime = os.path.getmtime(tpath)
        prev_mtime = last_mtimes.get(cid, 0.0)
        new_mtimes[cid] = curr_mtime

        handled_for_cid = set(handled_map.get(cid, []))
        found_steps = []

        # If mtime has not changed AND we already have recorded handled steps for this cid, skip reading file
        if curr_mtime <= prev_mtime and cid in handled_map:
            all_known_steps[cid] = handled_map[cid]
            continue

        try:
            with open(tpath, "r", errors="replace") as f:
                lines = f.readlines()
            for line_idx, line in enumerate(lines):
                try:
                    data = json.loads(line)
                    if data.get("type") == "PLANNER_RESPONSE":
                        content = data.get("content", "")
                        step_idx = data.get("step_index")
                        if step_idx is not None:
                            found_steps.append(int(step_idx))
                        if content and content.strip() and step_idx is not None:
                            s_int = int(step_idx)
                            if s_int not in handled_for_cid:
                                # Fallback to transcript_full.jsonl if content was truncated
                                if "content" in data.get("truncated_fields", []):
                                    full_path = tpath.replace("transcript.jsonl", "transcript_full.jsonl")
                                    if os.path.exists(full_path):
                                        try:
                                            with open(full_path, "r", errors="replace") as ff:
                                                for f_idx, f_line in enumerate(ff):
                                                    if f_idx == line_idx:
                                                        f_data = json.loads(f_line)
                                                        content = f_data.get("content", content)
                                                        break
                                        except Exception:
                                            pass
                                new_responses.append({
                                    "conversation_id": cid,
                                    "title": cinfo.get("title") or f"Session ({cid[:8]})",
                                    "step_index": s_int,
                                    "content": content.strip(),
                                    "pid": cinfo.get("pid"),
                                    "profile": t_profile,
                                })
                except Exception:
                    pass
        except Exception:
            pass

        all_known_steps[cid] = found_steps

    # 4. Check for quota exhaustion in recent CLI logs
    new_quotas = []
    cand_logs = glob.glob(os.path.expanduser("~/.gemini/antigravity-cli/log/cli-*.log")) + glob.glob(
        os.path.expanduser("~/.config/multigravity-profiles/*/.gemini/antigravity-cli/log/cli-*.log")
    )
    if cand_logs:
        cand_logs.sort(key=os.path.getmtime, reverse=True)
        for lf in cand_logs[:2]:
            # If log was modified in the last 10 minutes (600s)
            if (time.time() - os.path.getmtime(lf)) < 600:
                try:
                    with open(lf, "r", errors="replace") as f:
                        log_text = f.read()
                    if "RESOURCE_EXHAUSTED" in log_text or "quota reached" in log_text:
                        m_quota = re.search(r"(Individual quota reached[^\n\r]+)", log_text)
                        if m_quota:
                            q_msg = m_quota.group(1).strip()
                            sig = f"{os.path.basename(lf)}:{q_msg}"
                            if sig not in handled_quotas:
                                m_conv = re.search(r"conversation[ =]+([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", log_text)
                                q_cid = m_conv.group(1) if m_conv else ""
                                m_prof = re.search(r"multigravity-profiles/([^/]+)/", log_text)
                                q_prof = m_prof.group(1) if m_prof else ""
                                q_title = conv_map.get(q_cid, {}).get("title", "Active Task")
                                new_quotas.append({
                                    "signature": sig,
                                    "conversation_id": q_cid,
                                    "title": q_title,
                                    "profile": q_prof,
                                    "message": "⚠️ " + q_msg,
                                })
                except Exception:
                    pass

    return {
        "new_responses": new_responses,
        "new_quotas": new_quotas,
        "all_known_steps": all_known_steps,
        "new_mtimes": new_mtimes,
        "conversations": conv_map,
    }


# Standalone code for remote SSH execution
_REMOTE_SCAN_CODE = r'''
import base64, glob, json, os, re, sqlite3, time, sys

def _poll_scan_sync(handled_map, handled_quotas, last_mtimes=None):
    if last_mtimes is None:
        last_mtimes = {}
    new_mtimes = dict(last_mtimes)

    dbs = [os.path.expanduser("~/.gemini/antigravity-cli/conversation_summaries.db")] + glob.glob(
        os.path.expanduser("~/.config/multigravity-profiles/*/.gemini/antigravity-cli/conversation_summaries.db")
    )
    conv_map = {}
    for db in dbs:
        if os.path.exists(db):
            try:
                conn = sqlite3.connect(db)
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT conversation_id, title, preview, step_count, status, not_fully_idle, datetime(last_modified_time) as last_mod FROM conversation_summaries ORDER BY last_modified_time DESC LIMIT 25"
                ).fetchall()
                for r in rows:
                    cid = r["conversation_id"]
                    if cid and cid not in conv_map:
                        conv_map[cid] = dict(r)
            except Exception:
                pass

    running_pids = {}
    if os.path.exists("/proc"):
        try:
            for p in glob.glob("/proc/[0-9]*"):
                pid = os.path.basename(p)
                try:
                    with open(f"{p}/cmdline", "rb") as f:
                        cmdline = f.read().decode("utf-8", errors="replace").replace("\x00", " ")
                    if "agy" in cmdline or "mgy" in cmdline:
                        m_cid = re.search(r"--conversation[=\s]+([^\s]+)", cmdline)
                        m_prof = re.search(r"multigravity-profiles/([^/]+)/", cmdline)
                        if not m_prof:
                            m_mgy = re.search(r"mgy\s+([a-zA-Z0-9_\-]+)", cmdline)
                            prof = m_mgy.group(1) if m_mgy else "default"
                        else:
                            prof = m_prof.group(1)
                        if m_cid:
                            cid = m_cid.group(1)
                            running_pids[cid] = {"pid": int(pid), "profile": prof}
                except Exception:
                    pass
        except Exception:
            pass

    for cid, info in running_pids.items():
        if cid in conv_map:
            conv_map[cid]["pid"] = info["pid"]
            conv_map[cid]["profile"] = info["profile"]
        else:
            conv_map[cid] = {
                "conversation_id": cid,
                "title": f"Active Session ({cid[:8]})",
                "pid": info["pid"],
                "profile": info["profile"],
                "not_fully_idle": 1,
            }

    new_responses = []
    all_known_steps = {}

    for cid, cinfo in conv_map.items():
        cand = [
            os.path.expanduser(f"~/.gemini/antigravity-cli/brain/{cid}/.system_generated/logs/transcript.jsonl")
        ] + glob.glob(
            os.path.expanduser(f"~/.config/multigravity-profiles/*/.gemini/antigravity-cli/brain/{cid}/.system_generated/logs/transcript.jsonl")
        )
        tpath = next((p for p in cand if os.path.exists(p)), None)
        if not tpath:
            continue

        m_prof = re.search(r"multigravity-profiles/([^/]+)/", tpath)
        t_profile = m_prof.group(1) if m_prof else (cinfo.get("profile") or "default")

        curr_mtime = os.path.getmtime(tpath)
        prev_mtime = last_mtimes.get(cid, 0.0)
        new_mtimes[cid] = curr_mtime

        handled_for_cid = set(handled_map.get(cid, []))
        found_steps = []

        if curr_mtime <= prev_mtime and cid in handled_map:
            all_known_steps[cid] = handled_map[cid]
            continue

        try:
            with open(tpath, "r", errors="replace") as f:
                lines = f.readlines()
            for line_idx, line in enumerate(lines):
                try:
                    data = json.loads(line)
                    if data.get("type") == "PLANNER_RESPONSE":
                        content = data.get("content", "")
                        step_idx = data.get("step_index")
                        if step_idx is not None:
                            found_steps.append(int(step_idx))
                        if content and content.strip() and step_idx is not None:
                            s_int = int(step_idx)
                            if s_int not in handled_for_cid:
                                if "content" in data.get("truncated_fields", []):
                                    full_path = tpath.replace("transcript.jsonl", "transcript_full.jsonl")
                                    if os.path.exists(full_path):
                                        try:
                                            with open(full_path, "r", errors="replace") as ff:
                                                for f_idx, f_line in enumerate(ff):
                                                    if f_idx == line_idx:
                                                        f_data = json.loads(f_line)
                                                        content = f_data.get("content", content)
                                                        break
                                        except Exception:
                                            pass
                                new_responses.append({
                                    "conversation_id": cid,
                                    "title": cinfo.get("title") or f"Session ({cid[:8]})",
                                    "step_index": s_int,
                                    "content": content.strip(),
                                    "pid": cinfo.get("pid"),
                                    "profile": t_profile,
                                })
                except Exception:
                    pass
        except Exception:
            pass

        all_known_steps[cid] = found_steps

    new_quotas = []
    cand_logs = glob.glob(os.path.expanduser("~/.gemini/antigravity-cli/log/cli-*.log")) + glob.glob(
        os.path.expanduser("~/.config/multigravity-profiles/*/.gemini/antigravity-cli/log/cli-*.log")
    )
    if cand_logs:
        cand_logs.sort(key=os.path.getmtime, reverse=True)
        for lf in cand_logs[:2]:
            if (time.time() - os.path.getmtime(lf)) < 600:
                try:
                    with open(lf, "r", errors="replace") as f:
                        log_text = f.read()
                    if "RESOURCE_EXHAUSTED" in log_text or "quota reached" in log_text:
                        m_quota = re.search(r"(Individual quota reached[^\n\r]+)", log_text)
                        if m_quota:
                            q_msg = m_quota.group(1).strip()
                            sig = f"{os.path.basename(lf)}:{q_msg}"
                            if sig not in handled_quotas:
                                m_conv = re.search(r"conversation[ =]+([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", log_text)
                                q_cid = m_conv.group(1) if m_conv else ""
                                m_prof = re.search(r"multigravity-profiles/([^/]+)/", log_text)
                                q_prof = m_prof.group(1) if m_prof else ""
                                q_title = conv_map.get(q_cid, {}).get("title", "Active Task")
                                new_quotas.append({
                                    "signature": sig,
                                    "conversation_id": q_cid,
                                    "title": q_title,
                                    "profile": q_prof,
                                    "message": "⚠️ " + q_msg,
                                })
                except Exception:
                    pass

    return {
        "new_responses": new_responses,
        "new_quotas": new_quotas,
        "all_known_steps": all_known_steps,
        "new_mtimes": new_mtimes,
        "conversations": conv_map,
    }

raw_in = sys.stdin.read().strip()
payload = json.loads(raw_in) if raw_in else {}
res = _poll_scan_sync(payload.get("handled_map", {}), payload.get("handled_quotas", []), payload.get("last_mtimes", {}))
print(json.dumps(res))
'''


class SessionMonitor:
    """Monitors agy/mgy conversation state and delivers every prompt response to Telegram."""

    def __init__(self, runner: MgyRunner, poll_interval: int = POLL_INTERVAL):
        self.runner = runner
        self.poll_interval = poll_interval
        self._running = False
        self._handled_conv_steps: Dict[str, Set[int]] = {}  # conv_id -> set of step_index already handled
        self._handled_quotas: Set[str] = set()               # error signatures already reported
        self._last_mtimes: Dict[str, float] = {}             # conv_id -> last seen mtime of transcript
        self._on_task_completed_cb: Optional[Callable[[Dict[str, Any], str], Any]] = None

    def claim_step(self, conversation_id: str, step_index: int) -> bool:
        """Atomically claims a step. Returns True if claimed, False if already handled."""
        if not conversation_id or step_index < 0:
            return False
        handled = self._handled_conv_steps.setdefault(conversation_id, set())
        if step_index in handled:
            return False
        handled.add(step_index)
        return True

    def unclaim_step(self, conversation_id: str, step_index: int) -> None:
        """Unclaims a step if network delivery failed, allowing a retry."""
        if conversation_id in self._handled_conv_steps:
            self._handled_conv_steps[conversation_id].discard(step_index)

    def mark_handled(self, conversation_id: str, step_index: int) -> None:
        """Marks a conversation step as handled."""
        if conversation_id and step_index >= 0:
            self._handled_conv_steps.setdefault(conversation_id, set()).add(step_index)

    def is_handled(self, conversation_id: str, step_index: int) -> bool:
        """Checks if a step was already delivered."""
        if not conversation_id or step_index < 0:
            return False
        return step_index in self._handled_conv_steps.get(conversation_id, set())

    def on_task_completed(self, callback: Callable[[Dict[str, Any], str], Any]) -> None:
        """Register a callback when a response or quota error arrives: callback(conv_info, output_text)"""
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
        # Proactively check for recent quota exhaustion in CLI logs across all profiles
        cand_logs = glob.glob(os.path.expanduser("~/.gemini/antigravity-cli/log/cli-*.log")) + glob.glob(
            os.path.expanduser("~/.config/multigravity-profiles/*/.gemini/antigravity-cli/log/cli-*.log")
        )
        if cand_logs:
            latest_log = max(cand_logs, key=os.path.getmtime)
            if (time.time() - os.path.getmtime(latest_log)) < 480:
                try:
                    with open(latest_log, "r", errors="replace") as f:
                        log_lines = f.readlines()
                    for l in reversed(log_lines[-80:]):
                        if "RESOURCE_EXHAUSTED" in l or "quota reached" in l:
                            match_quota = re.search(r"(Individual quota reached[^\n\r]+)", l)
                            if match_quota:
                                return "⚠️ " + match_quota.group(1).strip()
                except Exception:
                    pass

        # Extract latest PLANNER_RESPONSE from transcript.jsonl
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

        return output

    async def get_recent_conversations(self, limit: int = 15) -> List[Dict[str, Any]]:
        """Fetches recent conversations from conversation_summaries.db across all profiles."""
        if self.runner.mode == "local":
            return await asyncio.to_thread(self._fetch_recent_conversations_sync, limit)

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

        import base64
        script = (
            "import json, os, glob, re\n"
            f"conv_id = '{conversation_id}'\n"
            "cand = [os.path.expanduser(f'~/.gemini/antigravity-cli/brain/{conv_id}/.system_generated/logs/transcript.jsonl')] + glob.glob(os.path.expanduser(f'~/.config/multigravity-profiles/*/.gemini/antigravity-cli/brain/{conv_id}/.system_generated/logs/transcript.jsonl'))\n"
            "path = next((p for p in cand if os.path.exists(p)), None)\n"
            "output = ''\n"
            "if path and os.path.exists(path):\n"
            "    try:\n"
            "        with open(path, 'r', errors='replace') as f:\n"
            "            lines = f.readlines()\n"
            "        for l in reversed(lines):\n"
            "            data = json.loads(l)\n"
            "            if data.get('type') == 'PLANNER_RESPONSE' and data.get('content'):\n"
            "                output = data.get('content', '').strip()\n"
            "                break\n"
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

    async def _run_poll_scan(self, initial: bool = False) -> Dict[str, Any]:
        """Runs poll scan locally or via SSH."""
        handled_map = {cid: list(steps) for cid, steps in self._handled_conv_steps.items()}
        handled_quotas = list(self._handled_quotas)
        last_mtimes = {} if initial else self._last_mtimes

        if self.runner.mode == "local":
            return await asyncio.to_thread(_poll_scan_sync, handled_map, handled_quotas, last_mtimes)

        # SSH mode execution via self-contained script
        payload = json.dumps({
            "handled_map": handled_map,
            "handled_quotas": handled_quotas,
            "last_mtimes": last_mtimes,
        })
        b64_code = base64.b64encode(_REMOTE_SCAN_CODE.strip().encode("utf-8")).decode("ascii")
        b64_payload = base64.b64encode(payload.encode("utf-8")).decode("ascii")

        cmd = f"python3 -c \"import base64; exec(base64.b64decode('{b64_code}').decode('utf-8'))\""
        code, out, _ = await self.runner.run_raw(f"echo '{b64_payload}' | base64 -d | {cmd}", timeout=15)
        if code == 0 and out.strip():
            try:
                return json.loads(out.strip())
            except Exception:
                pass
        return {}

    async def start(self) -> None:
        """Starts the background monitoring loop."""
        self._running = True
        logger.info("Starting SessionMonitor background watcher...")

        # Warm-up cycle: index all existing conversation steps & quota errors so we don't spam historical messages
        try:
            init_res = await self._run_poll_scan(initial=True)
            for cid, steps in init_res.get("all_known_steps", {}).items():
                self._handled_conv_steps.setdefault(cid, set()).update(steps)
            for q in init_res.get("new_quotas", []):
                self._handled_quotas.add(q["signature"])
            self._last_mtimes.update(init_res.get("new_mtimes", {}))
            logger.info("SessionMonitor initialized: indexed %d conversations.", len(self._handled_conv_steps))
        except Exception as e:
            logger.warning("SessionMonitor warm-up scan error: %s", e)

        # Main monitoring loop
        while self._running:
            try:
                await asyncio.sleep(self.poll_interval)
                res = await self._run_poll_scan(initial=False)
                self._last_mtimes.update(res.get("new_mtimes", {}))

                # Handle new prompt responses across all conversations
                for resp in res.get("new_responses", []):
                    cid = resp.get("conversation_id")
                    s_idx = resp.get("step_index")
                    content = resp.get("content", "").strip()

                    if not cid or s_idx is None or not content:
                        continue

                    # Atomically claim step to ensure exactly one delivery
                    if self.claim_step(cid, s_idx):
                        logger.info("Delivering prompt response for %s (Step #%s, PID %s)", cid, s_idx, resp.get("pid"))
                        delivered = False
                        if self._on_task_completed_cb:
                            try:
                                r = self._on_task_completed_cb(resp, content)
                                if asyncio.iscoroutine(r):
                                    delivered = await r
                                else:
                                    delivered = bool(r)
                            except Exception as cb_err:
                                logger.error("Error in on_task_completed callback: %s", cb_err)
                        if not delivered:
                            logger.warning("Delivery failed for %s step #%s (likely network drop), unclaiming to retry on next poll", cid, s_idx)
                            self.unclaim_step(cid, s_idx)

                # Handle new quota exhaustion errors
                for q in res.get("new_quotas", []):
                    sig = q.get("signature")
                    if sig and sig not in self._handled_quotas:
                        self._handled_quotas.add(sig)
                        logger.warning("Delivering quota alert for %s: %s", q.get("conversation_id"), q.get("message"))
                        if self._on_task_completed_cb:
                            try:
                                r = self._on_task_completed_cb(q, q.get("message", "⚠️ Quota limit reached"))
                                if asyncio.iscoroutine(r):
                                    await r
                            except Exception as cb_err:
                                logger.error("Error in quota callback: %s", cb_err)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Error in session monitor loop: %s", e)
                await asyncio.sleep(self.poll_interval)

    def stop(self) -> None:
        self._running = False
