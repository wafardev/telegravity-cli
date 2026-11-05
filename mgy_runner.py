import asyncio
import os
import re
import shlex
from typing import Optional, Tuple, List, Dict
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

    def __init__(self, mode: str = RUN_MODE, default_profile: str = MGY_PROFILE):
        self.mode = mode
        self.active_profile = default_profile
        self.ssh_runner = SSHRunner(
            user=SSH_USER,
            host=SSH_HOST,
            port=SSH_PORT,
            key_path=SSH_KEY_PATH,
            timeout=SSH_TIMEOUT,
        ) if mode == "ssh" else None
        self.current_dir: Optional[str] = None
        self._active_proc: Optional[asyncio.subprocess.Process] = None
        self._is_busy: bool = False
        self._current_task_description: Optional[str] = None

    @property
    def is_busy(self) -> bool:
        return self._is_busy

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

    async def get_instances(self) -> List[Dict[str, str]]:
        """
        Returns active mgy and agy instances running on the target device.
        Each entry contains: {'pid': str, 'cmd': str, 'type': 'mgy'|'agy'}
        """
        code, out, _ = await self.run_raw("ps -eo pid,ppid,args", timeout=15)
        if code != 0 or not out:
            return []

        lines = out.splitlines()
        instances: List[Dict[str, str]] = []
        mgy_pids = set()

        # First pass: find mgy processes
        for line in lines[1:]:
            parts = line.strip().split(None, 2)
            if len(parts) < 3:
                continue
            pid, ppid, args = parts[0], parts[1], parts[2]

            # Ignore daemon, grep, and editor processes
            if "telegravity" in args or "grep" in args:
                continue

            # Match mgy invocation (e.g. bash .../mgy <profile> ..., mgy <profile> ...)
            match = re.search(r"(?:^|[\s/])mgy(?:\.sh|\.py)?(?:\s+(.*))?$", args)
            if match:
                mgy_args = (match.group(1) or "").strip()
                cmd_clean = f"mgy {mgy_args}".strip()
                instances.append({"pid": pid, "cmd": cmd_clean, "type": "mgy"})
                mgy_pids.add(pid)

        # Second pass: find standalone agy processes whose parent is not mgy
        for line in lines[1:]:
            parts = line.strip().split(None, 2)
            if len(parts) < 3:
                continue
            pid, ppid, args = parts[0], parts[1], parts[2]
            if "telegravity" in args or "grep" in args:
                continue
            if ppid in mgy_pids:
                continue

            match = re.search(r"(?:^|[\s/])agy(?:\s+(.*))?$", args)
            if match:
                agy_args = (match.group(1) or "").strip()
                cmd_clean = f"agy {agy_args}".strip()
                instances.append({"pid": pid, "cmd": cmd_clean, "type": "agy"})

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
        """Fetches quota details across profiles."""
        code, out, err = await self.run_raw(f"{self.mgy_bin} quotas", timeout=30)
        return out if code == 0 else (out + "\n" + err)

    async def set_active_profile(self, profile: str) -> bool:
        """Sets the active profile for subsequent task dispatches."""
        profiles = await self.list_profiles()
        valid_names = [p["name"] for p in profiles]
        if profile in valid_names or not valid_names:
            self.active_profile = profile
            return True
        return False

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
        conversation_id: Optional[str] = None,
        continue_session: bool = True,
    ) -> Tuple[int, str, str]:
        """
        Dispatches an agy task via mgy using the active profile.
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
            args = [f"{self.mgy_bin} {self.active_profile}"]

            if conversation_id:
                args.append(f"--conversation={shlex.quote(conversation_id)}")
            elif continue_session:
                args.append("-c")

            args.append(f"-p {safe_prompt}")
            args.append("--dangerously-skip-permissions")

            full_cmd = " ".join(args)

            if self.mode == "ssh":
                # For SSH, pass through ssh runner
                code, out, err = await self.run_raw(full_cmd, timeout=SSH_TIMEOUT)
            else:
                code, out, err = await self.run_raw(full_cmd, timeout=SSH_TIMEOUT)

            return code, out, err
        finally:
            self._is_busy = False
            self._current_task_description = None

    async def cancel_active_task(self) -> Tuple[int, str]:
        """Cancels any running mgy / agy turn."""
        if self.mode == "ssh":
            # Kill agy/mgy processes on remote host
            kill_cmd = f"pkill -TERM -f 'mgy {self.active_profile}' || pkill -TERM -f 'agy'"
            code, out, err = await self.ssh_runner._run_raw(kill_cmd)
            self._is_busy = False
            self._current_task_description = None
            return code, "Sent termination signal to remote agy/mgy process."
        else:
            if self._active_proc:
                try:
                    self._active_proc.terminate()
                    await asyncio.sleep(0.5)
                    if self._active_proc.returncode is None:
                        self._active_proc.kill()
                except ProcessLookupError:
                    pass
            self._is_busy = False
            self._current_task_description = None
            return 0, "Terminated local task."
