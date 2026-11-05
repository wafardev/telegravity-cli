import asyncio
import os
from typing import Optional, Tuple
from config import SSH_USER, SSH_HOST, SSH_PORT, SSH_KEY_PATH, SSH_TIMEOUT


class SSHRunner:
    """Handles execution of remote commands over SSH."""

    def __init__(
        self,
        user: str = SSH_USER,
        host: str = SSH_HOST,
        port: int = SSH_PORT,
        key_path: Optional[str] = SSH_KEY_PATH,
        timeout: int = SSH_TIMEOUT,
    ):
        self.user = user
        self.host = host
        self.port = port
        self.key_path = os.path.expanduser(key_path) if key_path else None
        self.timeout = timeout
        self.current_dir: Optional[str] = None

    def _build_ssh_cmd(self, remote_command: str) -> list[str]:
        args = ["ssh", "-p", str(self.port)]

        # Common SSH options to avoid infinite hangs on headless runs
        args.extend([
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=10",
            "-o", "StrictHostKeyChecking=accept-new",
        ])

        if self.key_path and os.path.exists(self.key_path):
            args.extend(["-i", self.key_path])

        destination = f"{self.user}@{self.host}"
        args.append(destination)

        # Prepend directory context if set
        if self.current_dir:
            exec_command = f"cd {self.current_dir} && {remote_command}"
        else:
            exec_command = remote_command

        args.append(exec_command)
        return args

    async def execute(self, command: str) -> Tuple[int, str, str]:
        """
        Executes a command asynchronously via SSH.
        Returns a tuple: (returncode, stdout, stderr)
        """
        # Handle cd command state
        stripped = command.strip()
        if stripped.startswith("cd ") or stripped == "cd":
            target = stripped[3:].strip() or "~"
            # Verify directory exists remotely
            test_cmd = f"cd {target} && pwd"
            code, stdout, stderr = await self._run_raw(test_cmd)
            if code == 0:
                self.current_dir = stdout.strip()
                return 0, f"Changed directory to {self.current_dir}", ""
            return code, stdout, stderr

        return await self._run_raw(command)

    async def _run_raw(self, remote_command: str) -> Tuple[int, str, str]:
        cmd_args = self._build_ssh_cmd(remote_command)
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd_args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_data, stderr_data = await asyncio.wait_for(
                proc.communicate(),
                timeout=self.timeout,
            )
            stdout = stdout_data.decode("utf-8", errors="replace")
            stderr = stderr_data.decode("utf-8", errors="replace")
            return proc.returncode if proc.returncode is not None else 1, stdout, stderr
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            return 124, "", f"Error: Command timed out after {self.timeout}s"
        except Exception as e:
            return 1, "", f"SSH execution error: {str(e)}"
