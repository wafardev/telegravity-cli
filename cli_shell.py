#!/usr/bin/env python3
"""
Interactive local CLI shell for testing SSH connection to the remote host.
Usage:
    python cli_shell.py
"""

import asyncio
import sys
from ssh_runner import SSHRunner
from config import SSH_USER, SSH_HOST, SSH_PORT


async def main():
    if not SSH_USER or not SSH_HOST:
        print("ERROR: SSH_USER and SSH_HOST are not configured in .env.")
        print("Please configure them in your .env file.")
        return

    runner = SSHRunner()
    print(f"=== telegravity-cli: Interactive Shell ===")
    print(f"Target: {SSH_USER}@{SSH_HOST}:{SSH_PORT}")
    print("Type commands to execute remotely. Type 'exit' or press Ctrl+C to quit.\n")

    # Quick connectivity check
    print("Checking connection...", end="", flush=True)
    code, stdout, stderr = await runner.execute("echo 'Connected: ' $(uname -a)")
    if code == 0:
        print(f" OK!\n{stdout}")
    else:
        print(f" FAILED!\n{stderr or stdout}")
        print("\nNote: Make sure your SSH keys are configured or check host reachability.")

    while True:
        try:
            cwd_display = runner.current_dir or "~"
            prompt = f"[{SSH_USER}@{SSH_HOST} {cwd_display}]$ "
            command = input(prompt).strip()

            if not command:
                continue
            if command.lower() in ("exit", "quit"):
                print("Exiting.")
                break

            code, out, err = await runner.execute(command)
            if out:
                print(out.rstrip())
            if err:
                print(f"[stderr]: {err.rstrip()}", file=sys.stderr)
            if code != 0:
                print(f"[exit status: {code}]")

        except (KeyboardInterrupt, EOFError):
            print("\nExiting.")
            break


if __name__ == "__main__":
    asyncio.run(main())
