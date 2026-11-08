import asyncio
import base64
import glob
import json
import os
import re
import shlex
import time
from typing import Optional, Tuple, List, Dict, Callable, Any
from config import (
    RUN_MODE,
    MGY_PROFILE,
    DEFAULT_WORKING_DIR,
    SSH_USER,
    SSH_HOST,
    SSH_PORT,
    SSH_KEY_PATH,
    SSH_TIMEOUT,
    TASK_TIMEOUT,
)
from ssh_runner import SSHRunner


class MgyRunner:
    """Manages executing multigravity (mgy) tasks and inspecting state."""

    def __init__(self, mode: str = RUN_MODE, default_profile: Optional[str] = MGY_PROFILE):
        self.mode = mode
        self.default_profile = default_profile
        if DEFAULT_WORKING_DIR:
            if mode == "ssh":
                self.current_dir = DEFAULT_WORKING_DIR
            else:
                expanded = os.path.expanduser(DEFAULT_WORKING_DIR)
                if os.path.exists(expanded):
                    self.current_dir = expanded
                else:
                    self.current_dir = None
        else:
            self.current_dir = None

        self.ssh_runner = SSHRunner(
            user=SSH_USER,
            host=SSH_HOST,
            port=SSH_PORT,
            key_path=SSH_KEY_PATH,
            timeout=SSH_TIMEOUT,
        ) if mode == "ssh" else None
        if self.ssh_runner and self.current_dir:
            self.ssh_runner.current_dir = self.current_dir
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

            # Skip shell wrappers (e.g., /bin/sh -c "mgy ...", bash -c "mgy ...")
            if re.search(r"(?:^|[\s/])(?:ba)?sh\s+-c\s+", args):
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

        # Drop any instance whose child is also an instance in mgy_pids (e.g. nested wrapper scripts)
        child_pids_set = {inst["pid"] for inst in instances}
        instances = [inst for inst in instances if inst["pid"] not in {i["ppid"] for i in instances if i["ppid"] in child_pids_set}]
        mgy_pids = {inst["pid"] for inst in instances}

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

            # Check 1: runner is currently busy with this PID or its descendants/ancestors
            if self._is_busy and self._active_pid:
                active_str = str(self._active_pid)
                if active_str == inst_pid:
                    is_running = True
                else:
                    stack = [active_str]
                    while stack:
                        p = stack.pop()
                        for c_pid, _ in children_map.get(p, []):
                            if c_pid == inst_pid:
                                is_running = True
                                break
                            stack.append(c_pid)
                        if is_running:
                            break
                    if not is_running:
                        stack = [inst_pid]
                        while stack:
                            p = stack.pop()
                            for c_pid, _ in children_map.get(p, []):
                                if c_pid == active_str:
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

            # If not running, check if it stopped due to depleted quota
            is_quota_depleted = False
            reset_time = None
            if not is_running:
                is_quota_depleted, reset_time = await self.check_pid_quota_depleted(int(inst_pid))

            inst["is_quota_depleted"] = is_quota_depleted
            inst["reset_time"] = reset_time

            if is_running:
                inst["status"] = "running"
            elif is_quota_depleted:
                inst["status"] = "quota_depleted"
            else:
                inst["status"] = "ready"

        return instances

    async def check_pid_quota_depleted(self, pid: int) -> Tuple[bool, Optional[str]]:
        """Checks if the instance with PID stopped due to depleted quota."""
        check_script = f"""
import os, glob, re

pid = {pid}
child_pids = [str(pid)]
try:
    with open(f"/proc/{{pid}}/task/{{pid}}/children", "r") as f:
        child_pids.extend(f.read().split())
except Exception:
    pass

logfile = None
for p in child_pids:
    for fd in (1, 2):
        link = f"/proc/{{p}}/fd/{{fd}}"
        if os.path.exists(link):
            try:
                target = os.readlink(link)
                if "/log/cli-" in target:
                    logfile = target
                    break
            except Exception:
                pass
    if logfile:
        break

if not logfile:
    files = sorted(glob.glob(os.path.expanduser("~/.gemini/antigravity-cli/log/cli-*.log")), key=os.path.getmtime, reverse=True)
    logfile = files[0] if files else None

if logfile and os.path.exists(logfile):
    try:
        with open(logfile, "r", errors="replace") as f:
            lines = f.readlines()
        for line in reversed(lines[-80:]):
            if "RESOURCE_EXHAUSTED" in line or "quota reached" in line:
                m = re.search(r"Resets in ([0-9a-zA-Z]+)", line)
                res_val = m.group(1) if m else "soon"
                print(f"DEPLETED:{{res_val}}")
                exit(0)
    except Exception:
        pass
print("OK")
"""
        code, out, _ = await self.run_raw(f"python3 -c '{check_script.strip()}'", timeout=5)
        if code == 0 and out.strip().startswith("DEPLETED:"):
            res_str = out.strip().split(":", 1)[1]
            return True, res_str
        return False, None

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

    async def get_imported_conversations(self) -> List[Dict[str, Any]]:
        """Scans for conversations imported from external archives (.conversation_summary.json)."""
        def _scan_sync() -> List[Dict[str, Any]]:
            results = []
            pattern = os.path.expanduser("~/.gemini/antigravity-cli/brain/*/.conversation_summary.json")
            seen_ids = set()
            for sum_path in glob.glob(pattern):
                try:
                    conv_dir = os.path.dirname(sum_path)
                    conv_id = os.path.basename(conv_dir)
                    seen_ids.add(conv_id)
                    with open(sum_path, "r", encoding="utf-8") as f:
                        data = json.load(f)

                    db_path = os.path.expanduser(f"~/.gemini/antigravity-cli/conversations/{conv_id}.db")
                    db_size = os.path.getsize(db_path) if os.path.exists(db_path) else 0
                    brain_size = sum(
                        os.path.getsize(os.path.join(r, file))
                        for r, d, files in os.walk(conv_dir)
                        for file in files
                    )
                    total_size = db_size + brain_size

                    title = data.get("title") or data.get("custom_name") or ""
                    if not title:
                        t_path = os.path.join(conv_dir, ".system_generated", "logs", "transcript.jsonl")
                        if os.path.exists(t_path):
                            try:
                                with open(t_path, "r", encoding="utf-8") as tf:
                                    for line in tf:
                                        step = json.loads(line)
                                        if step.get("type") == "USER_INPUT":
                                            c = step.get("content", "")
                                            c = re.sub(r"<USER_REQUEST>|<!--.*?-->|</USER_REQUEST>", "", c).strip()
                                            lines_c = c.splitlines()
                                            first_l = lines_c[0].strip() if lines_c else ""
                                            if first_l:
                                                title = (first_l[:50] + "...") if len(first_l) > 50 else first_l
                                                break
                            except Exception:
                                pass
                    if not title:
                        title = "(untitled)"

                    steps = data.get("step_count") or 0

                    results.append({
                        "id": conv_id,
                        "title": title,
                        "step_count": steps,
                        "total_size_mb": round(total_size / (1024 * 1024), 1),
                        "last_modified": data.get("last_modified_time", ""),
                    })
                except Exception:
                    pass

            # Explicitly include known/imported conversations if not yet matched
            known_ids = ["04a5d409-d7f6-4abd-9b40-ce321bc65d35", "bfdee43a-3ed6-4213-9bc1-860d2e9697d3"]
            for kid in known_ids:
                if kid not in seen_ids:
                    k_brain = os.path.expanduser(f"~/.gemini/antigravity-cli/brain/{kid}")
                    k_db = os.path.expanduser(f"~/.gemini/antigravity-cli/conversations/{kid}.db")
                    if os.path.exists(k_brain) or os.path.exists(k_db):
                        seen_ids.add(kid)
                        dbs = os.path.getsize(k_db) if os.path.exists(k_db) else 0
                        bs = sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(k_brain) for f in fs) if os.path.exists(k_brain) else 0
                        title = ""
                        steps = 0
                        tp = os.path.join(k_brain, ".system_generated", "logs", "transcript.jsonl")
                        if os.path.exists(tp):
                            try:
                                with open(tp, "r", encoding="utf-8") as tf:
                                    for line in tf:
                                        step = json.loads(line)
                                        steps = max(steps, step.get("step_index", 0))
                                        if not title and step.get("type") == "USER_INPUT":
                                            c = re.sub(r"<USER_REQUEST>|<!--.*?-->|</USER_REQUEST>", "", step.get("content", "")).strip()
                                            first_l = c.splitlines()[0].strip() if c.splitlines() else ""
                                            if first_l:
                                                title = (first_l[:50] + "...") if len(first_l) > 50 else first_l
                            except Exception:
                                pass
                        results.append({
                            "id": kid,
                            "title": title or "(untitled)",
                            "step_count": steps,
                            "total_size_mb": round((dbs + bs) / (1024 * 1024), 1),
                            "last_modified": "",
                        })

            results.sort(key=lambda x: x.get("total_size_mb", 0), reverse=True)
            return results

        if self.mode == "local":
            return await asyncio.to_thread(_scan_sync)

        # SSH mode execution
        script_code = (
            "import glob, json, os, re\n"
            "results = []\n"
            "seen = set()\n"
            "for s in glob.glob(os.path.expanduser('~/.gemini/antigravity-cli/brain/*/.conversation_summary.json')):\n"
            "    try:\n"
            "        cdir = os.path.dirname(s); cid = os.path.basename(cdir)\n"
            "        seen.add(cid)\n"
            "        with open(s, 'r', encoding='utf-8') as f: d = json.load(f)\n"
            "        db = os.path.expanduser(f'~/.gemini/antigravity-cli/conversations/{cid}.db')\n"
            "        dbs = os.path.getsize(db) if os.path.exists(db) else 0\n"
            "        bs = sum(os.path.getsize(os.path.join(r, fl)) for r, _, fs in os.walk(cdir) for fl in fs)\n"
            "        t = d.get('title') or d.get('custom_name') or ''\n"
            "        if not t:\n"
            "            tp = os.path.join(cdir, '.system_generated', 'logs', 'transcript.jsonl')\n"
            "            if os.path.exists(tp):\n"
            "                with open(tp, 'r', encoding='utf-8') as tf:\n"
            "                    for line in tf:\n"
            "                        st = json.loads(line)\n"
            "                        if st.get('type') == 'USER_INPUT':\n"
            "                            c = re.sub(r'<USER_REQUEST>|<!--.*?-->|</USER_REQUEST>', '', st.get('content', '')).strip()\n"
            "                            t = (c.splitlines()[0][:50] + '...') if c.splitlines() else '(untitled)'\n"
            "                            break\n"
            "        results.append({'id': cid, 'title': t or '(untitled)', 'step_count': d.get('step_count', 0), 'total_size_mb': round((dbs+bs)/(1024*1024), 1)})\n"
            "    except: pass\n"
            "for kid in ['04a5d409-d7f6-4abd-9b40-ce321bc65d35', 'bfdee43a-3ed6-4213-9bc1-860d2e9697d3']:\n"
            "    if kid not in seen:\n"
            "        kb = os.path.expanduser(f'~/.gemini/antigravity-cli/brain/{kid}')\n"
            "        kd = os.path.expanduser(f'~/.gemini/antigravity-cli/conversations/{kid}.db')\n"
            "        if os.path.exists(kb) or os.path.exists(kd):\n"
            "            seen.add(kid)\n"
            "            dbs = os.path.getsize(kd) if os.path.exists(kd) else 0\n"
            "            bs = sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(kb) for f in fs) if os.path.exists(kb) else 0\n"
            "            t = ''; steps = 0\n"
            "            tp = os.path.join(kb, '.system_generated', 'logs', 'transcript.jsonl')\n"
            "            if os.path.exists(tp):\n"
            "                try:\n"
            "                    with open(tp, 'r', encoding='utf-8') as tf:\n"
            "                        for l in tf:\n"
            "                            st = json.loads(l); steps = max(steps, st.get('step_index', 0))\n"
            "                            if not t and st.get('type') == 'USER_INPUT':\n"
            "                                c = re.sub(r'<USER_REQUEST>|<!--.*?-->|</USER_REQUEST>', '', st.get('content', '')).strip()\n"
            "                                t = (c.splitlines()[0][:50] + '...') if c.splitlines() else '(untitled)'\n"
            "                except: pass\n"
            "            results.append({'id': kid, 'title': t or '(untitled)', 'step_count': steps, 'total_size_mb': round((dbs+bs)/(1024*1024), 1)})\n"
            "results.sort(key=lambda x: x.get('total_size_mb', 0), reverse=True)\n"
            "print(json.dumps(results))\n"
        )
        b64 = base64.b64encode(script_code.encode("utf-8")).decode("ascii")
        code, out, _ = await self.run_raw(f"python3 -c \"import base64; exec(base64.b64decode('{b64}').decode('utf-8'))\"", timeout=20)
        if code == 0 and out.strip():
            try:
                data = json.loads(out.strip())
                if isinstance(data, list):
                    return data
            except Exception:
                pass
        return []

    async def get_quotas(self) -> str:
        """Fetches quota details across profiles via 'mgy quotas'."""
        code, out, err = await self.run_raw(f"{self.mgy_bin} quotas", timeout=60)
        raw = out if code == 0 else (out + "\n" + err)
        cleaned = re.sub(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])", "", raw)
        return cleaned.strip() or "No quota data returned."

    async def has_interactive_tty(self, pid: int) -> bool:
        """Checks if a process (or any of its child processes) is attached to an interactive terminal."""
        if pid <= 0:
            return False
        code, out, _ = await self.run_raw(f"ps -p {pid} -o tty=", timeout=5)
        if code == 0:
            tty = out.strip()
            if tty and tty != "?":
                return True

        # Check child processes
        code2, out2, _ = await self.run_raw(f"pgrep -P {pid} 2>/dev/null", timeout=5)
        if code2 == 0 and out2.strip():
            for c_pid in out2.strip().split():
                c_code, c_out, _ = await self.run_raw(f"ps -p {c_pid} -o tty=", timeout=5)
                if c_code == 0:
                    c_tty = c_out.strip()
                    if c_tty and c_tty != "?":
                        return True
        return False

    async def inject_input_to_pid(self, pid: int, text: str) -> Tuple[bool, str]:
        """
        Injects input text (keystrokes) directly into the interactive terminal prompt of PID.
        Uses GDB + TIOCSTI to push characters into the TTY input queue without spawning new processes.
        """
        if pid <= 0:
            return False, "Invalid PID."

        # Flatten multiline text for single-line terminal prompt (prevents premature Enter submission)
        cleaned_text = " ".join(line.strip() for line in text.splitlines() if line.strip())
        if not cleaned_text:
            return False, "Prompt is empty."

        # Ensure text ends with carriage return (\r) for terminal prompt submission
        if not cleaned_text.endswith("\r"):
            cleaned_text += "\r"

        has_tty = await self.has_interactive_tty(pid)
        if not has_tty:
            return False, f"Process {pid} is not attached to an interactive terminal."

        b64_data = base64.b64encode(cleaned_text.encode("utf-8")).decode("ascii")

        gdb_script = """import gdb, os, base64, sys
try:
    b64_data = os.environ.get("INJECT_DATA", "")
    raw = base64.b64decode(b64_data)
    inf = gdb.selected_inferior()
    sp = int(gdb.parse_and_eval("$sp"))
    scratch = sp - 128
    for b in raw:
        inf.write_memory(scratch, bytes([b]), 1)
        res = int(gdb.parse_and_eval(f"(int)ioctl(0, 0x5412, {scratch})"))
        if res != 0:
            sys.exit(1)
    sys.exit(0)
except Exception as e:
    sys.stderr.write(str(e) + chr(10))
    sys.exit(1)
"""
        b64_script = base64.b64encode(gdb_script.strip().encode("utf-8")).decode("ascii")

        runner_script = f"""import base64, tempfile, subprocess, os, sys
code = base64.b64decode('{b64_script}')
f = tempfile.NamedTemporaryFile('wb', suffix='.py', delete=False)
f.write(code)
f.close()
env = os.environ.copy()
env['INJECT_DATA'] = '{b64_data}'
res = subprocess.run(['gdb', '-p', '{pid}', '-batch', '-x', f.name], env=env, capture_output=True)
os.unlink(f.name)
if res.returncode != 0:
    sys.stderr.write(res.stderr.decode('utf-8', errors='replace'))
sys.exit(res.returncode)
"""
        b64_runner = base64.b64encode(runner_script.strip().encode("utf-8")).decode("ascii")

        cmd = f"python3 -c \"import base64; exec(base64.b64decode('{b64_runner}'))\""
        code, out, err = await self.run_raw(cmd, timeout=20)
        if code == 0:
            return True, f"Successfully typed prompt into PID {pid}."
        err_msg = err.strip() or out.strip() or f"gdb exited with code {code}"
        return False, err_msg

    async def kill_process(self, pid: int) -> Tuple[bool, str]:
        """Kills a process and all its child/descendant processes (e.g. agy under mgy)."""
        if pid <= 1:
            return False, "Invalid PID: cannot kill system processes."

        # Check if process exists first
        code, _, _ = await self.run_raw(f"kill -0 {pid} 2>/dev/null", timeout=5)
        if code != 0:
            return False, f"Process {pid} does not exist or has already terminated."

        # Collect all descendant PIDs recursively so child processes (like agy) aren't orphaned
        all_to_kill = [pid]
        stack = [pid]
        while stack:
            curr = stack.pop()
            c_code, c_out, _ = await self.run_raw(f"pgrep -P {curr} 2>/dev/null", timeout=5)
            if c_code == 0 and c_out.strip():
                for cp in c_out.strip().split():
                    if cp.isdigit() and int(cp) not in all_to_kill:
                        c_val = int(cp)
                        all_to_kill.append(c_val)
                        stack.append(c_val)

        pids_str = " ".join(str(p) for p in all_to_kill)

        # Send SIGTERM first to entire tree
        await self.run_raw(f"kill -15 {pids_str} 2>/dev/null", timeout=5)
        await asyncio.sleep(0.4)

        # Check if root process is gone
        code2, _, _ = await self.run_raw(f"kill -0 {pid} 2>/dev/null", timeout=5)
        if code2 != 0:
            # Clean up any lingering children with SIGKILL just in case
            lingering = [p for p in all_to_kill if p != pid]
            if lingering:
                await self.run_raw(f"kill -9 {' '.join(str(p) for p in lingering)} 2>/dev/null", timeout=5)
            return True, f"Process {pid} and its process tree terminated."

        # Force kill with SIGKILL to all
        await self.run_raw(f"kill -9 {pids_str} 2>/dev/null", timeout=5)
        await asyncio.sleep(0.3)
        code3, _, _ = await self.run_raw(f"kill -0 {pid} 2>/dev/null", timeout=5)
        if code3 != 0:
            return True, f"Process {pid} and its process tree terminated (SIGKILL)."

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

                    await asyncio.wait_for(asyncio.gather(t_out, t_err, proc.wait()), timeout=TASK_TIMEOUT)
                    code = proc.returncode if proc.returncode is not None else 1
                    return code, "".join(stdout_parts), "".join(stderr_parts)
                except asyncio.TimeoutError:
                    if self._active_proc:
                        try:
                            self._active_proc.kill()
                        except ProcessLookupError:
                            pass
                    return 124, "", f"Error: Command timed out after {TASK_TIMEOUT}s"
                except Exception as e:
                    return 1, "", f"Execution error: {str(e)}"
                finally:
                    self._active_proc = None
                    self._active_pid = None
            else:
                # SSH mode
                code, out, err = await self.run_raw(full_cmd, timeout=TASK_TIMEOUT)
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
            has_tty = await self.has_interactive_tty(target_pid)
            if has_tty:
                # For interactive terminal sessions, send SIGINT (Ctrl+C) to cancel current turn without killing shell
                await self.run_raw(f"kill -2 {target_pid} 2>/dev/null", timeout=5)
                code_c, out_c, _ = await self.run_raw(f"pgrep -P {target_pid} 2>/dev/null", timeout=5)
                if code_c == 0 and out_c.strip():
                    for c_pid in out_c.strip().split():
                        await self.run_raw(f"kill -2 {c_pid} 2>/dev/null", timeout=5)
                self._is_busy = False
                self._current_task_description = None
                self._active_pid = None
                return 0, target_pid, f"Sent cancel signal (Ctrl+C) to PID {target_pid}"
            else:
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
        cand_dbs = [os.path.expanduser("~/.gemini/antigravity-cli/conversation_summaries.db")] + glob.glob(os.path.expanduser("~/.config/multigravity-profiles/*/.gemini/antigravity-cli/conversation_summaries.db"))
        for db in cand_dbs:
            if os.path.exists(db):
                conn = None
                try:
                    import sqlite3
                    conn = sqlite3.connect(db)
                    if found_cid:
                        r = conn.execute("SELECT title, not_fully_idle FROM conversation_summaries WHERE conversation_id = ?", (found_cid,)).fetchone()
                        if r:
                            found_title = r[0]
                            break
                    else:
                        r = conn.execute("SELECT conversation_id, title, not_fully_idle FROM conversation_summaries ORDER BY last_modified_time DESC LIMIT 1").fetchone()
                        if r:
                            found_cid = r[0]
                            found_title = r[1]
                            break
                except Exception:
                    pass
                finally:
                    if conn:
                        try:
                            conn.close()
                        except Exception:
                            pass

        # Extract rich dialogue context from transcript
        content = ""
        status = "unknown"
        latest_step = -1
        latest_model = None

        if found_cid:
            cand_t = [
                os.path.expanduser(f"~/.gemini/antigravity-cli/brain/{found_cid}/.system_generated/logs/transcript.jsonl"),
            ] + glob.glob(os.path.expanduser(f"~/.config/multigravity-profiles/*/.gemini/antigravity-cli/brain/{found_cid}/.system_generated/logs/transcript.jsonl"))
            tpath = next((p for p in cand_t if os.path.exists(p)), None)
            if tpath and os.path.exists(tpath):
                try:
                    with open(tpath, "r", errors="replace") as f:
                        lines = f.readlines()
                    turns = []
                    current_turn = {"user": None, "tools": [], "thinking": None, "model": None, "step_index": -1}
                    for line in lines:
                        try:
                            entry = json.loads(line)
                            mtype = entry.get("type")
                            s_idx = int(entry.get("step_index", -1))
                            if mtype == "USER_INPUT":
                                if current_turn["user"] is not None or current_turn["model"] is not None:
                                    turns.append(current_turn)
                                    current_turn = {"user": None, "tools": [], "thinking": None, "model": None, "step_index": -1}
                                raw = entry.get("content", "")
                                m = re.search(r"<USER_REQUEST>\s*([\s\S]*?)\s*</USER_REQUEST>", raw)
                                current_turn["user"] = m.group(1).strip() if m else raw.strip()
                                current_turn["step_index"] = s_idx
                            elif mtype == "PLANNER_RESPONSE":
                                c_text = entry.get("content", "").strip()
                                th_text = entry.get("thinking", "").strip()
                                t_calls = entry.get("tool_calls", [])
                                if c_text:
                                    if current_turn["model"] is not None:
                                        turns.append(current_turn)
                                        current_turn = {"user": None, "tools": [], "thinking": None, "model": None, "step_index": -1}
                                    current_turn["model"] = c_text
                                    current_turn["step_index"] = s_idx
                                elif th_text:
                                    current_turn["thinking"] = th_text
                                    current_turn["step_index"] = s_idx
                                if t_calls:
                                    for t in t_calls:
                                        current_turn["tools"].append(t.get("name", "tool"))
                                    current_turn["step_index"] = s_idx
                        except Exception:
                            pass

                    if current_turn["user"] is not None or current_turn["model"] is not None or current_turn["thinking"] or current_turn["tools"]:
                        turns.append(current_turn)

                    if turns:
                        latest_model = None
                        latest_model_step = -1
                        for t in reversed(turns):
                            if t.get("model"):
                                latest_model = t["model"]
                                latest_model_step = t.get("step_index", -1)
                                break

                        last_t = turns[-1]
                        latest_step = latest_model_step if latest_model_step != -1 else last_t.get("step_index", -1)
                        if last_t.get("model"):
                            status = "completed"
                        elif last_t.get("thinking"):
                            status = "thinking"
                        elif last_t.get("tools"):
                            status = "tool_call"
                        else:
                            status = "waiting"

                        formatted_blocks = []
                        for t in turns[-3:]:
                            parts = []
                            if t["user"]:
                                u = t["user"]
                                if len(u) > 250:
                                    u = u[:230] + "..."
                                parts.append(f"👤 User: {u}")
                            if t["tools"]:
                                u_tools = list(dict.fromkeys(t["tools"]))
                                parts.append(f"⚙️ Tools: {', '.join(u_tools[:5])}")
                            if t["thinking"] and not t["model"]:
                                parts.append(f"💭 Thinking: {t['thinking'][:180]}...")
                            if t["model"]:
                                m = t["model"]
                                if len(m) > 1200:
                                    m = m[:1100] + "\n... [truncated for peek - full response sent when finished]"
                                parts.append(f"🤖 Assistant:\n{m}")
                            if parts:
                                formatted_blocks.append("\n".join(parts))

                        content = "\n\n────────────────\n\n".join(formatted_blocks)
                        if len(content) > 2800:
                            content = "..." + content[-2700:]
                except Exception:
                    pass

        # Check if a quota exhaustion error occurred in CLI logs
        cand_logs = (
            glob.glob(os.path.expanduser("~/.gemini/antigravity-cli/log/cli-*.log")) +
            glob.glob(os.path.expanduser("~/.config/multigravity-profiles/*/.gemini/antigravity-cli/log/cli-*.log"))
        )
        log_files = sorted([f for f in cand_logs if os.path.exists(f)], key=os.path.getmtime, reverse=True)
        if log_files and (time.time() - os.path.getmtime(log_files[0])) < 480:
            try:
                with open(log_files[0], "r", errors="replace") as f:
                    log_lines = f.readlines()
                for l in reversed(log_lines[-80:]):
                    if "RESOURCE_EXHAUSTED" in l or "quota reached" in l:
                        m = re.search(r"(Individual quota reached[^\n\r]+)", l)
                        if m:
                            content = f"⚠️ {m.group(1).strip()}"
                            status = "quota_exhausted"
                            break
            except Exception:
                pass

        return {
            "found": True,
            "conversation_id": found_cid,
            "conversation_title": found_title,
            "content": content,
            "raw_latest_model": latest_model,
            "transcript_status": status,
            "step_index": latest_step,
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
            inspect_code = r"""
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
    conn = None
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
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass
content = ""
status = "unknown"
latest_step = -1
latest_model = None
if found_cid:
    tpath = os.path.expanduser(f"~/.gemini/antigravity-cli/brain/{found_cid}/.system_generated/logs/transcript.jsonl")
    if os.path.exists(tpath):
        try:
            with open(tpath, "r", errors="replace") as f:
                lines = f.readlines()
            turns = []
            cur_turn = {"user": None, "tools": [], "thinking": None, "model": None, "step_index": -1}
            for line in lines:
                try:
                    entry = json.loads(line)
                    mtype = entry.get("type")
                    s_idx = int(entry.get("step_index", -1))
                    if mtype == "USER_INPUT":
                        if cur_turn["user"] is not None or cur_turn["model"] is not None:
                            turns.append(cur_turn)
                            cur_turn = {"user": None, "tools": [], "thinking": None, "model": None, "step_index": -1}
                        raw = entry.get("content", "")
                        m = re.search(r"<USER_REQUEST>\s*([\s\S]*?)\s*</USER_REQUEST>", raw)
                        cur_turn["user"] = m.group(1).strip() if m else raw.strip()
                        cur_turn["step_index"] = s_idx
                    elif mtype == "PLANNER_RESPONSE":
                        c_text = entry.get("content", "").strip()
                        th_text = entry.get("thinking", "").strip()
                        t_calls = entry.get("tool_calls", [])
                        if c_text:
                            if cur_turn["model"] is not None:
                                turns.append(cur_turn)
                                cur_turn = {"user": None, "tools": [], "thinking": None, "model": None, "step_index": -1}
                            cur_turn["model"] = c_text
                            cur_turn["step_index"] = s_idx
                        elif th_text:
                            cur_turn["thinking"] = th_text
                            cur_turn["step_index"] = s_idx
                        if t_calls:
                            for t in t_calls:
                                cur_turn["tools"].append(t.get("name", "tool"))
                            cur_turn["step_index"] = s_idx
                except Exception:
                    pass
            if cur_turn["user"] is not None or cur_turn["model"] is not None or cur_turn["thinking"] or cur_turn["tools"]:
                turns.append(cur_turn)

            if turns:
                latest_model = None
                latest_model_step = -1
                for t in reversed(turns):
                    if t.get("model"):
                        latest_model = t["model"]
                        latest_model_step = t.get("step_index", -1)
                        break

                last_t = turns[-1]
                latest_step = latest_model_step if latest_model_step != -1 else last_t.get("step_index", -1)
                if last_t.get("model"):
                    status = "completed"
                elif last_t.get("thinking"):
                    status = "thinking"
                elif last_t.get("tools"):
                    status = "tool_call"
                else:
                    status = "waiting"

                blocks = []
                for t in turns[-3:]:
                    parts = []
                    if t["user"]:
                        u = t["user"]
                        if len(u) > 250:
                            u = u[:230] + "..."
                        parts.append(f"👤 User: {u}")
                    if t["tools"]:
                        parts.append(f"⚙️ Tools: {', '.join(list(dict.fromkeys(t['tools']))[:5])}")
                    if t["thinking"] and not t["model"]:
                        parts.append(f"💭 Thinking: {t['thinking'][:180]}...")
                    if t["model"]:
                        m = t["model"]
                        if len(m) > 1200:
                            m = m[:1100] + "\\n... [truncated for peek - full response sent when finished]"
                        parts.append(f"🤖 Assistant:\\n{m}")
                    if parts:
                        blocks.append("\\n".join(parts))
                content = "\\n\\n────────────────\\n\\n".join(blocks)
                if len(content) > 2800:
                    content = "..." + content[-2700:]
        except Exception:
            pass
print(json.dumps({"found": True, "conversation_id": found_cid, "conversation_title": found_title, "content": content, "raw_latest_model": latest_model, "transcript_status": status, "step_index": latest_step}))
"""
            b64_script = base64.b64encode(inspect_code.strip().encode()).decode()
            code_p, out_p, _ = await self.run_raw(f"PEEK_PID={pid} python3 -c \"import base64; exec(base64.b64decode('{b64_script}'))\"", timeout=8)
            res = json.loads(out_p.strip()) if code_p == 0 and out_p.strip() else {}

        if res.get("found"):
            res_content = res.get("content", "")
            if is_running and res_content:
                res_content = self._extract_last_sentences(res_content, max_sentences=5, max_chars=1200)

            is_depleted = (res.get("transcript_status") == "quota_exhausted") or ("Individual quota reached" in res_content)

            return {
                "pid": pid,
                "found": True,
                "cmd": cmd,
                "is_running": is_running,
                "is_quota_depleted": is_depleted,
                "source": "transcript",
                "conversation_id": res.get("conversation_id"),
                "conversation_title": res.get("conversation_title"),
                "content": res_content or ("(Waiting for first output from prompt...)" if is_running else "(No recent answer found)"),
                "raw_latest_model": res.get("raw_latest_model"),
                "step_index": res.get("step_index", -1),
                "transcript_status": res.get("transcript_status", "unknown"),
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
