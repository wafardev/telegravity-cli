<div align="center">

<img src="assets/telegravity-cli-logo.jpeg" alt="telegravity-cli logo" width="320" style="border-radius: 16px; margin-bottom: 16px;">

# telegravity-cli

Control and interact with **Antigravity CLI (`agy`)** and **Multigravity CLI (`mgy`)** instances remotely over Telegram from any device.

</div>

---

## Overview

`telegravity-cli` is a permanent, asynchronous daemon and Telegram bot. It lets you:
1. **Dispatch coding tasks** from Telegram to your `agy` / `mgy` instances on a remote device (or local host).
2. **Auto-detect task completion**: As soon as the agent finishes writing code, analyzing logs, or editing files, the daemon automatically sends the formatted summary and diff to your phone.
3. **Multi-Account Profile Management (`mgy`)**: Bind chat sessions to specific accounts/profiles (`/new <profile> [conv]`, `/quotas`).
4. **Passive External Session Monitoring**: Even if you start a task from your computer terminal or IDE and walk away, the daemon detects when it completes and pushes the output to Telegram.

---

## Architecture

```mermaid
flowchart TD
    User["📱 Telegram User"]
    Bot["🤖 Bot Router & Auth"]
    Dispatcher["⚡ Task Dispatcher (mgy_runner)"]
    Agent["🧠 Antigravity Agent (mgy + agy)"]
    Cloud["☁️ Google AI Cloud (Gemini)"]
    DB[("💾 Session DB & Transcripts")]
    Monitor["👁️ Passive Session Monitor"]

    User -->|"1. Prompt / Command"| Bot
    Bot -->|"2. Dispatch Task"| Dispatcher
    Dispatcher -->|"3. Execute Turn (Local or SSH)"| Agent
    Agent <-->|"4. Tools & Cloud Reasoning"| Cloud
    Agent -->|"5. Write State & Transcripts"| DB
    DB -.->|"6. Detect IDLE (Finished)"| Monitor
    Monitor -->|"7. Push Output & Diff Alert"| User
```

```text
┌────────────────────────────────────────────────────────────────────────┐
│                            TELEGRAM CLIENT                             │
│                     (Mobile / Desktop / Web App)                       │
└──────────────────┬──────────────────────────────────▲──────────────────┘
                   │                                  │
     1. Prompt /   │                                  │ 7. Formatted Output
        Commands   │                                  │    & Diff Alert
                   ▼                                  │
┌───────────────────────────────────────┐  ┌──────────┴──────────────────┐
│          telegravity Daemon           │  │      telegravity Daemon     │
│          [Bot Router & Auth]          │  │   [Passive Session Monitor] │
└──────────────────┬────────────────────┘  └──────────▲──────────────────┘
                   │                                  │
     2. Validated  │                                  │ 6. Detect IDLE
        Task       │                                  │    (Finished)
                   ▼                                  │
┌───────────────────────────────────────┐  ┌──────────┴──────────────────┐
│       Task Dispatcher (mgy_runner)    │  │ SQLite DB & Transcript Logs │
│         (Async Local or SSH)          │  │  (conversation_summaries)  │
└──────────────────┬────────────────────┘  └──────────▲──────────────────┘
                   │                                  │
     3. Headless   │                                  │ 5. Record State
        Dispatch   │                                  │    & Transcripts
                   ▼                                  │
┌─────────────────────────────────────────────────────┴──────────────────┐
│                    TARGET MACHINE (Remote or Local)                    │
│                                                                        │
│   multigravity-cli (mgy)  ──►  antigravity-cli (agy)                   │
│   (Profile & Token Switcher)   (Autonomous Agent Engine)               │
└──────────────────────────────────┬─────────────────────────────────────┘
                                   │
                     4. Tool Calls │
                        & Prompts  ▼
                     ┌───────────────────────────┐
                     │   Google AI Cloud (LLM)   │
                     └───────────────────────────┘
```

### Architecture Highlights

1. **Telegram Interface & Security**:
   - Only whitelisted Telegram User IDs (`ALLOWED_USER_IDS`) can dispatch commands.
   - Responses are automatically split into `<3800` character chunks to respect Telegram's message limits.

2. **Asynchronous Task Dispatcher (`mgy_runner`)**:
   - Executes `mgy <profile> -c -p "<prompt>" --dangerously-skip-permissions` either locally or over SSH.
   - Fully non-blocking so the bot remains responsive while the agent is running.
   - Cancel running tasks anytime via `/cancel` or inline buttons.

3. **Passive Session Monitor (`session_monitor`)**:
   - Continuously watches `conversation_summaries.db` on the target machine.
   - Detects when tasks transition from `RUNNING` (`not_fully_idle=1`) to `IDLE` (`not_fully_idle=0`).
   - Retrieves the final `PLANNER_RESPONSE` from `transcript.jsonl` and delivers it to Telegram—even for sessions started outside Telegram (e.g., from your terminal or IDE).

4. **MultiGravity Profile Management**:
   - Seamlessly switches OAuth tokens per account while sharing conversation history, memory, and MCP servers.

---

## Features & Commands

| Command                      | Description                                                    |
| :--------------------------- | :------------------------------------------------------------- |
| **Any text message**         | Dispatches a task prompt to `mgy` using the session profile    |
| `/list`                      | Output raw `mgy list` from the target device                   |
| `/instances`                 | List running instances with 🟢 Ready (waiting) / 🟡 Running (in prompt) buttons |
| `/quotas`                    | Output raw `mgy quotas` from the target device                 |
| `/conversations`             | View recent sessions with live status (`⚡ RUNNING` / `🟢 IDLE`) |
| `/new <profile> [conv]`      | Bind chat session to a profile and optional conversation ID    |
| `/cancel [pid]`              | Abort/interrupt running task (shows PID and supports cancel by PID) |
| `/kill <pid>`                | Terminate any process by PID on the target device              |
| `/cd <path>`                 | Change working directory on target host                        |
| `/pwd`                       | View current working directory                                 |
| `/status`                    | View host health, load, session profile, and runner state      |

---

## Quickstart & Setup

### 1. Installation

```bash
git clone https://github.com/wafardev/telegravity-cli.git
cd telegravity-cli

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

> [!NOTE]
> `requirements.txt` automatically installs **`multigravity-cli`** (`mgy`) directly from GitHub into your virtual environment (`.venv/bin/mgy`).

### 2. Configuration

Copy the example configuration:
```bash
cp .env.example .env
```

Configure your `.env`:
* `TELEGRAM_BOT_TOKEN`: Bot token from [@BotFather](https://t.me/BotFather).
* `ALLOWED_USER_IDS`: Your Telegram user ID(s) (get from [@userinfobot](https://t.me/userinfobot)).
* `RUN_MODE`: `ssh` (if connecting to a remote device over SSH) or `local` (if running directly on the target machine).
* `MGY_PROFILE`: Default profile to dispatch to (e.g. `default`).
* `SSH_USER` & `SSH_HOST`: Target credentials (e.g. `user`, `192.168.1.50` or your remote hostname).

### 3. Launching the Daemon

```bash
python bot.py
```

### 4. Running as a Permanent 24/7 Service on Remote Device (Optional)

If running the daemon directly on your target Linux machine:

#### Option A: User Service (Recommended, No Sudo Required)
```bash
# Keep service running after logout and across reboots:
loginctl enable-linger $USER

mkdir -p ~/.config/systemd/user
cp telegravity.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now telegravity.service
systemctl --user status telegravity.service
```

#### Option B: System Service (With Sudo)
```bash
sudo cp telegravity.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now telegravity.service
sudo systemctl status telegravity.service
```
