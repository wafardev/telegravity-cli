import asyncio
import json
import os
import re
import shlex
from typing import Optional, Tuple, List, Dict, Callable, Any
from config import (
    RUN_MODE,
    MGY_PROFILE,
    SSH_USER,
    SSH_HOST,
    SSH_PORT,
    SSH_KEY_PATH,
    SSH_TIMEOUT,
)
from ssh_runner import SSHRunner


class MgyRunner:
    """Manages executing multigravity (mgy) tasks and inspecting state."""

    def __init__(self, mode: str = RUN_MODE, default_profile: Optional[str] = MGY_PROFILE):
        self.mode = mode
        self.default_profile = default_profile
        self.ssh_runner = SSHRunner(
            user=SSH_USER,
            host=SSH_HOST,
            port=SSH_PORT,
            key_path=SSH_KEY_PATH,
            timeout=SSH_TIMEOUT,
        ) if mode == "ssh" else None
        self.current_dir: Optional[str] = None
        self._active_proc: Optional[asyncio.subprocess.Process] = None
        self._active_pid: Optional[int] = None
        self._live_stream: Dict[int, List[str]] = {}
        self._is_busy: bool = False
        self._current_task_description: Optional[str] = None

    @property
    def is_busy(self) -> bool:
        return self._is_busy

    @property
    def active_pid(self) -> Optional[int]:
        return self._active_pid

    @property
    def current_task(self) -> Optional[str]:
        return self._current_task_description

    @property
    def mgy_bin(self) -> str:
        """Returns path or command name to mgy binary."""
        if self.mode == "local":
            venv_mgy = os.path.join(os.path.dirname(__file__), ".venv", "bin", "mgy")
            if os.path.isfile(venv_mgy) and os.access(venv_mgy, os.X_OK):
                return venv_mgy
        return "mgy"

    async def run_raw(self, cmd: str, timeout: Optional[int] = None) -> Tuple[int, str, str]:
        """Runs a command either over SSH or locally."""
        t = timeout or SSH_TIMEOUT
        if self.mode == "ssh":
            if self.current_dir:
                exec_cmd = f"cd {self.current_dir} && {cmd}"
            else:
                exec_cmd = cmd
            return await self.ssh_runner._run_raw(exec_cmd)
        else:
            # Local execution
            cwd = os.path.expanduser(self.current_dir) if self.current_dir else None
            try:
                proc = await asyncio.create_subprocess_shell(
                    cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                )
                self._active_proc = proc
                stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=t)
                code = proc.returncode if proc.returncode is not None else 1
                return code, stdout_b.decode("utf-8", errors="replace"), stderr_b.decode("utf-8", errors="replace")
            except asyncio.TimeoutError:
                if self._active_proc:
                    try:
                        self._active_proc.kill()
                    except ProcessLookupError:
                        pass
                return 124, "", f"Error: Command timed out after {t}s"
            except Exception as e:
                return 1, "", f"Execution error: {str(e)}"
            finally:
                self._active_proc = None

    async def get_mgy_list(self) -> str:
        """Returns the raw output of 'mgy list' from the device."""
        code, out, err = await self.run_raw(f"{self.mgy_bin} list", timeout=15)
        if code == 0:
            return out.strip()
        return (out + "\n" + err).strip() or "No profiles found or error running mgy list."

    async def get_instances(self) -> List[Dict[str, Any]]:
        """
        Returns active mgy and agy instances running on the target device.
        Each entry contains: {
            'pid': str,
            'cmd': str,
            'type': 'mgy' | 'agy',
            'is_running': bool,  # True = in a prompt (running), False = waiting for a prompt (ready)
            'status': str,       # 'running' or 'ready'
            'profile': Optional[str],
            'conversation_id': Optional[str],
        }
        """
        code, out, _ = await self.run_raw("ps -eo pid,ppid,args", timeout=15)
        if code != 0 or not out:
            return []

        lines = out.splitlines()
        instances: List[Dict[str, Any]] = []
        mgy_pids = set()
        children_map: Dict[str, List[Tuple[str, str]]] = {}

        raw_procs = []
        for line in lines[1:]:
            parts = line.strip().split(None, 2)
            if len(parts) < 3:
                continue
            pid, ppid, args = parts[0], parts[1], parts[2]
            raw_procs.append((pid, ppid, args))
            children_map.setdefault(ppid, []).append((pid, args))

        # First pass: find mgy processes
        for pid, ppid, args in raw_procs:
            if "telegravity" in args or "grep" in args:
                continue

            match = re.search(r"(?:^|[\s/])mgy(?:\.sh|\.py)?(?:\s+(.*))?$", args)
            if match:
                mgy_args = (match.group(1) or "").strip()
                cmd_clean = f"mgy {mgy_args}".strip()
                instances.append({
                    "pid": pid,
                    "ppid": ppid,
                    "cmd": cmd_clean,
                    "type": "mgy",
                    "args": mgy_args,
                })
                mgy_pids.add(pid)

        # Second pass: find standalone agy processes whose parent is not mgy
        for pid, ppid, args in raw_procs:
            if "telegravity" in args or "grep" in args:
                continue
            if ppid in mgy_pids:
                continue

            match = re.search(r"(?:^|[\s/])agy(?:\s+(.*))?$", args)
            if match:
                agy_args = (match.group(1) or "").strip()
                cmd_clean = f"agy {agy_args}".strip()
                instances.append({
                    "pid": pid,
                    "ppid": ppid,
                    "cmd": cmd_clean,
                    "type": "agy",
                    "args": agy_args,
                })

        if not instances:
            return []

        # Query active conversations in SQLite database
        active_conv_query = """
import sqlite3, json, os
db = os.path.expanduser("~/.gemini/antigravity-cli/conversation_summaries.db")
if not os.path.exists(db):
    print("[]")
    exit(0)
conn = sqlite3.connect(db)
conn.row_factory = sqlite3.Row
rows = conn.execute("SELECT conversation_id, title, not_fully_idle FROM conversation_summaries WHERE not_fully_idle = 1").fetchall()
print(json.dumps([dict(r) for r in rows]))
"""
        code2, out2, _ = await self.run_raw(f"python3 -c '{active_conv_query.strip()}'", timeout=5)
        active_convs = []
        if code2 == 0 and out2.strip():
            try:
                active_convs = json.loads(out2.strip())
            except Exception:
                pass

        active_conv_ids = {c["conversation_id"] for c in active_convs if "conversation_id" in c}

        # Check running state for each instance
        for inst in instances:
            inst_pid = inst["pid"]
            args_str = inst.get("args", "")

            # Extract conversation_id if present
            conv_match = re.search(r"--conversation[=\s]+([^\s]+)", args_str)
            conv_id = conv_match.group(1) if conv_match else None
            inst["conversation_id"] = conv_id

            # Extract profile name
            if inst["type"] == "mgy":
                prof_tokens = [t for t in args_str.split() if not t.startswith("-")]
                inst["profile"] = prof_tokens[0] if prof_tokens else (self.default_profile or "default")
            else:
                inst["profile"] = self.default_profile or "default"

            is_running = False

            # Check 1: runner is currently busy with this PID or its descendants
            if self._is_busy and self._active_pid:
                if str(self._active_pid) == inst_pid:
                    is_running = True
                else:
                    stack = [inst_pid]
                    while stack:
                        p = stack.pop()
                        for c_pid, _ in children_map.get(p, []):
                            if c_pid == str(self._active_pid):
                                is_running = True
                                break
                            stack.append(c_pid)
                        if is_running:
                            break

            # Check 2: specific conversation is marked not_fully_idle
            if not is_running and conv_id and conv_id in active_conv_ids:
                is_running = True

            # Check 3: any conversation is not_fully_idle and only 1 instance exists
            if not is_running and active_conv_ids and len(instances) == 1:
                is_running = True

            # Check 4: instance has active tool child processes running under agy
            if not is_running:
                stack = [inst_pid]
                while stack:
                    p = stack.pop()
                    for c_pid, c_args in children_map.get(p, []):
                        if not re.search(r"(?:^|[\s/])(mgy|agy)(?:\s|$)", c_args):
                            is_running = True
                            break
                        stack.append(c_pid)
                    if is_running:
                        break

            inst["is_running"] = is_running
            inst["status"] = "running" if is_running else "ready"

        return instances

    async def list_profiles(self) -> List[Dict[str, str]]:
        """Lists available multigravity profiles."""
        code, out, _ = await self.run_raw(f"{self.mgy_bin} list", timeout=15)
        profiles = []
        if code == 0:
            for line in out.splitlines():
                line = line.strip()
                if line.startswith("•") or line.startswith("*") or line.startswith("-"):
                    parts = line.lstrip("•*- ").split()
                    name = parts[0] if parts else ""
                    email = ""
                    match = re.search(r"\(([^)]+)\)", line)
                    if match:
                        email = match.group(1)
                    if name:
                        profiles.append({"name": name, "email": email})
        return profiles

    async def get_quotas(self) -> str:
        """Fetches quota details across profiles via 'mgy quotas'."""
        code, out, err = await self.run_raw(f"{self.mgy_bin} quotas", timeout=60)
        raw = out if code == 0 else (out + "\n" + err)
        cleaned = re.sub(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])", "", raw)
        return cleaned.strip() or "No quota data returned."

    async def kill_process(self, pid: int) -> Tuple[bool, str]:
        """Kills a process by PID on the target device."""
        if pid <= 1:
            return False, "Invalid PID: cannot kill system processes."

        # Check if process exists first
        code, _, _ = await self.run_raw(f"kill -0 {pid} 2>/dev/null", timeout=5)
        if code != 0:
            return False, f"Process {pid} does not exist or has already terminated."

        # Send SIGTERM first
        await self.run_raw(f"kill -15 {pid} 2>/dev/null", timeout=5)
        await asyncio.sleep(0.5)

        # Check if process is gone
        code2, _, _ = await self.run_raw(f"kill -0 {pid} 2>/dev/null", timeout=5)
        if code2 != 0:
            return True, f"Process {pid} terminated."

        # Force kill with SIGKILL
        await self.run_raw(f"kill -9 {pid} 2>/dev/null", timeout=5)
        await asyncio.sleep(0.3)
        code3, _, _ = await self.run_raw(f"kill -0 {pid} 2>/dev/null", timeout=5)
        if code3 != 0:
            return True, f"Process {pid} terminated (SIGKILL)."

        return False, f"Failed to terminate process {pid} (permission denied or protected)."

    async def change_dir(self, target: str) -> Tuple[int, str]:
        """Changes the working directory."""
        stripped = target.strip() or "~"
        cmd = f"cd {stripped} && pwd"
        code, out, err = await self.run_raw(cmd, timeout=10)
        if code == 0:
            self.current_dir = out.strip()
            return 0, self.current_dir
        return code, err or out

    async def dispatch_task(
        self,
        prompt: str,
        profile: str,
        conversation_id: Optional[str] = None,
        continue_session: bool = True,
        on_start: Optional[Callable[[int], Any]] = None,
    ) -> Tuple[int, str, str]:
        """
        Dispatches an agy task via mgy using the specified profile.
        Returns: (returncode, stdout, stderr)
        """
        if self._is_busy:
            return 1, "", "Runner is busy with another task. Use /cancel first."

        self._is_busy = True
        self._current_task_description = prompt[:80] + ("..." if len(prompt) > 80 else "")

        try:
            # Escape prompt safely for shell
            safe_prompt = shlex.quote(prompt)

            # Build mgy command
            # Auto-approves tool executions with --dangerously-skip-permissions for headless runs
            args = [f"{self.mgy_bin} {profile}"]

            if conversation_id:
                args.append(f"--conversation={shlex.quote(conversation_id)}")
            elif continue_session:
                args.append("-c")

            args.append(f"-p {safe_prompt}")
            args.append("--dangerously-skip-permissions")

            full_cmd = " ".join(args)

            if self.mode == "local":
                cwd = os.path.expanduser(self.current_dir) if self.current_dir else None
                try:
                    proc = await asyncio.create_subprocess_shell(
                        full_cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        cwd=cwd,
                    )
                    self._active_proc = proc
                    self._active_pid = proc.pid
                    self._live_stream[proc.pid] = []

                    if on_start and proc.pid:
                        try:
                            res = on_start(proc.pid)
                            if asyncio.iscoroutine(res):
                                asyncio.create_task(res)
                        except Exception:
                            pass

                    async def _stream_pipe(stream, pid, collector):
                        while True:
                            line = await stream.readline()
                            if not line:
                                break
                            decoded = line.decode("utf-8", errors="replace")
                            collector.append(decoded)
                            buf = self._live_stream.setdefault(pid, [])
                            buf.append(decoded)
                            if len(buf) > 100:
                                buf.pop(0)

                    stdout_parts: List[str] = []
                    stderr_parts: List[str] = []
                    t_out = asyncio.create_task(_stream_pipe(proc.stdout, proc.pid, stdout_parts))
                    t_err = asyncio.create_task(_stream_pipe(proc.stderr, proc.pid, stderr_parts))

                    await asyncio.wait_for(asyncio.gather(t_out, t_err, proc.wait()), timeout=SSH_TIMEOUT)
                    code = proc.returncode if proc.returncode is not None else 1
                    return code, "".join(stdout_parts), "".join(stderr_parts)
                except asyncio.TimeoutError:
                    if self._active_proc:
                        try:
                            self._active_proc.kill()
                        except ProcessLookupError:
                            pass
                    return 124, "", f"Error: Command timed out after {SSH_TIMEOUT}s"
                except Exception as e:
                    return 1, "", f"Execution error: {str(e)}"
                finally:
                    self._active_proc = None
                    self._active_pid = None
            else:
                # SSH mode
                code, out, err = await self.run_raw(full_cmd, timeout=SSH_TIMEOUT)
                return code, out, err
        finally:
            self._is_busy = False
            self._current_task_description = None
            self._active_pid = None

    async def cancel_active_task(self, pid: Optional[int] = None) -> Tuple[int, Optional[int], str]:
        """
        Cancels any running mgy / agy turn.
        Returns: (returncode, pid, status_message)
        """
        target_pid = pid or self._active_pid

        if not target_pid:
            # Look up active mgy instance in process table
            instances = await self.get_instances()
            for inst in instances:
                if inst.get("type") == "mgy":
                    try:
                        target_pid = int(inst["pid"])
                        break
                    except ValueError:
                        pass

        if target_pid:
            success, msg = await self.kill_process(target_pid)
            self._is_busy = False
            self._current_task_description = None
            self._active_pid = None
            if self._active_proc:
                try:
                    self._active_proc.kill()
                except ProcessLookupError:
                    pass
                self._active_proc = None
            return (0 if success else 1), target_pid, f"Cancelled task (PID: {target_pid})"

        if self._active_proc:
            proc_pid = self._active_proc.pid
            try:
                self._active_proc.kill()
            except ProcessLookupError:
                pass
            self._is_busy = False
            self._current_task_description = None
            self._active_proc = None
            self._active_pid = None
            return 0, proc_pid, f"Cancelled task (PID: {proc_pid})"

        self._is_busy = False
        self._current_task_description = None
        self._active_pid = None
        return 0, None, "No active running task found."

    @staticmethod
    def _extract_last_sentences(text: str, max_sentences: int = 4, max_chars: int = 1200) -> str:
        """Extracts the last few sentences from output text."""
        cleaned = text.strip()
        if not cleaned or len(cleaned) <= max_chars:
            return cleaned
        sentences = re.split(r"(?<=[.!?])\s+", cleaned)
        if len(sentences) <= max_sentences:
            return cleaned[-max_chars:]
        last_few = " ".join(sentences[-max_sentences:]).strip()
        return last_few if len(last_few) <= max_chars else last_few[-max_chars:]

    @staticmethod
    def _peek_inspect(pid: int) -> Dict[str, Any]:
        """Direct local inspection of conversation and transcript for an instance PID."""
        found_cid = None
        found_title = None

        # Check cmdline
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmdline = f.read().decode("utf-8", errors="replace").replace("\x00", " ")
            m = re.search(r"--conversation[=\s]+([^\s]+)", cmdline)
            if m:
                found_cid = m.group(1)
        except Exception:
            pass

        # Check open fds
        if not found_cid:
            child_pids = [str(pid)]
            try:
                with open(f"/proc/{pid}/task/{pid}/children", "r") as f:
                    child_pids.extend(f.read().split())
            except Exception:
                pass

            for p in child_pids:
                fd_dir = f"/proc/{p}/fd"
                if os.path.exists(fd_dir):
                    try:
                        for link in os.listdir(fd_dir):
                            target = os.readlink(os.path.join(fd_dir, link))
                            m = re.search(r"brain/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", target)
                            if m:
                                found_cid = m.group(1)
                                break
                    except Exception:
                        pass
                if found_cid:
                    break

        # Fallback to latest conversation in database
        db = os.path.expanduser("~/.gemini/antigravity-cli/conversation_summaries.db")
        if os.path.exists(db):
            try:
                import sqlite3
                conn = sqlite3.connect(db)
                if found_cid:
                    r = conn.execute("SELECT title, not_fully_idle FROM conversation_summaries WHERE conversation_id = ?", (found_cid,)).fetchone()
                    if r:
                        found_title = r[0]
                else:
                    r = conn.execute("SELECT conversation_id, title, not_fully_idle FROM conversation_summaries ORDER BY last_modified_time DESC LIMIT 1").fetchone()
                    if r:
                        found_cid = r[0]
                        found_title = r[1]
            except Exception:
                pass

        # Extract latest output from transcript
        content = ""
        status = "unknown"
        if found_cid:
            tpath = os.path.expanduser(f"~/.gemini/antigravity-cli/brain/{found_cid}/.system_generated/logs/transcript.jsonl")
            if os.path.exists(tpath):
                try:
                    with open(tpath, "r", errors="replace") as f:
                        lines = f.readlines()
                    for line in reversed(lines):
                        try:
                            entry = json.loads(line)
                            m_type = entry.get("type")
                            c_text = entry.get("content", "").strip()
                            th_text = entry.get("thinking", "").strip()
                            t_calls = entry.get("tool_calls", [])
                            if c_text:
                                content = c_text
                                status = "completed"
                                break
                            elif th_text:
                                content = f"[Thinking]: {th_text}"
                                status = "thinking"
                                break
                            elif t_calls:
                                names = [t.get("name", "tool") for t in t_calls]
                                content = f"[Tool Call]: {', '.join(names)}"
                                status = "tool_call"
                                break
                        except Exception:
                            pass
                except Exception:
                    pass

        return {
            "found": True,
            "conversation_id": found_cid,
            "conversation_title": found_title,
            "content": content,
            "transcript_status": status,
        }

    async def peek_instance(self, pid: int) -> Dict[str, Any]:
        """
        Inspects the instance with PID and returns its latest answer or streaming output.
        """
        code, out, _ = await self.run_raw(f"ps -p {pid} -o args=", timeout=5)
        if code != 0 or not out.strip():
            return {
                "pid": pid,
                "found": False,
                "error": f"Process {pid} does not exist or has terminated.",
            }

        cmd = out.strip()

        # Check running state
        instances = await self.get_instances()
        target_inst = next((i for i in instances if str(i.get("pid")) == str(pid)), None)
        is_running = target_inst.get("is_running", False) if target_inst else (self._is_busy and self._active_pid == pid)

        # Check live stream buffer if task was dispatched via telegravity
        live_lines = self._live_stream.get(pid, [])
        if is_running and live_lines:
            stream_text = "".join(live_lines).strip()
            sentences = self._extract_last_sentences(stream_text, max_sentences=5, max_chars=1200)
            return {
                "pid": pid,
                "found": True,
                "cmd": cmd,
                "is_running": True,
                "source": "live_stream",
                "conversation_id": target_inst.get("conversation_id") if target_inst else None,
                "conversation_title": None,
                "content": sentences or stream_text[-1000:],
            }

        if self.mode == "local":
            res = await asyncio.to_thread(self._peek_inspect, pid)
        else:
            import base64
            inspect_code = """
import os, sys, json, sqlite3, re
pid = int(os.environ.get("PEEK_PID", 0))
found_cid = None
found_title = None
try:
    with open(f"/proc/{pid}/cmdline", "rb") as f:
        cmdline = f.read().decode("utf-8", errors="replace").replace("\\x00", " ")
    m = re.search(r"--conversation[=\\s]+([^\\s]+)", cmdline)
    if m:
        found_cid = m.group(1)
except Exception:
    pass
if not found_cid:
    child_pids = [str(pid)]
    try:
        with open(f"/proc/{pid}/task/{pid}/children", "r") as f:
            child_pids.extend(f.read().split())
    except Exception:
        pass
    for p in child_pids:
        fd_dir = f"/proc/{p}/fd"
        if os.path.exists(fd_dir):
            try:
                for link in os.listdir(fd_dir):
                    target = os.readlink(os.path.join(fd_dir, link))
                    m = re.search(r"brain/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", target)
                    if m:
                        found_cid = m.group(1)
                        break
            except Exception:
                pass
        if found_cid:
            break
db = os.path.expanduser("~/.gemini/antigravity-cli/conversation_summaries.db")
if os.path.exists(db):
    try:
        conn = sqlite3.connect(db)
        if found_cid:
            r = conn.execute("SELECT title, not_fully_idle FROM conversation_summaries WHERE conversation_id = ?", (found_cid,)).fetchone()
            if r:
                found_title = r[0]
        else:
            r = conn.execute("SELECT conversation_id, title, not_fully_idle FROM conversation_summaries ORDER BY last_modified_time DESC LIMIT 1").fetchone()
            if r:
                found_cid = r[0]
                found_title = r[1]
    except Exception:
        pass
content = ""
status = "unknown"
if found_cid:
    tpath = os.path.expanduser(f"~/.gemini/antigravity-cli/brain/{found_cid}/.system_generated/logs/transcript.jsonl")
    if os.path.exists(tpath):
        try:
            with open(tpath, "r", errors="replace") as f:
                lines = f.readlines()
            for line in reversed(lines):
                try:
                    entry = json.loads(line)
                    m_type = entry.get("type")
                    c_text = entry.get("content", "").strip()
                    th_text = entry.get("thinking", "").strip()
                    t_calls = entry.get("tool_calls", [])
                    if c_text:
                        content = c_text
                        status = "completed"
                        break
                    elif th_text:
                        content = f"[Thinking]: {th_text}"
                        status = "thinking"
                        break
                    elif t_calls:
                        names = [t.get("name", "tool") for t in t_calls]
                        content = f"[Tool Call]: {', '.join(names)}"
                        status = "tool_call"
                        break
                except Exception:
                    pass
        except Exception:
            pass
print(json.dumps({"found": True, "conversation_id": found_cid, "conversation_title": found_title, "content": content, "transcript_status": status}))
"""
            b64_script = base64.b64encode(inspect_code.strip().encode()).decode()
            code_p, out_p, _ = await self.run_raw(f"PEEK_PID={pid} python3 -c \"import base64; exec(base64.b64decode('{b64_script}'))\"", timeout=8)
            res = json.loads(out_p.strip()) if code_p == 0 and out_p.strip() else {}

        if res.get("found"):
            res_content = res.get("content", "")
            if is_running and res_content:
                res_content = self._extract_last_sentences(res_content, max_sentences=5, max_chars=1200)

            return {
                "pid": pid,
                "found": True,
                "cmd": cmd,
                "is_running": is_running,
                "source": "transcript",
                "conversation_id": res.get("conversation_id"),
                "conversation_title": res.get("conversation_title"),
                "content": res_content or ("(Waiting for first output from prompt...)" if is_running else "(No recent answer found)"),
            }

        return {
            "pid": pid,
            "found": True,
            "cmd": cmd,
            "is_running": is_running,
            "source": "none",
            "conversation_id": None,
            "conversation_title": None,
            "content": "(No conversation or transcript data found for this instance)",
        }
