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
3. **Multi-Account Profile Management (`mgy`)**: Switch between isolated accounts and profiles directly from Telegram with inline buttons (`/profiles`, `/quotas`).
4. **Passive External Session Monitoring**: Even if you start a task from your computer terminal or IDE and walk away, the daemon detects when it completes and pushes the output to Telegram.

---

## Architecture

```mermaid
flowchart LR
    subgraph Client [Telegram Client]
        Msg["Send Task Prompt"]
        Notif["Receive Completion Alert"]
    end

    subgraph Service [telegravity-cli Daemon]
        Bot["Bot Router"]
        Dispatcher["Task Dispatcher"]
        Observer["Session Monitor"]
    end

    subgraph Target [Target Device]
        MGY["multigravity-cli (mgy)"]
        AGY["antigravity-cli (agy)"]
        DB[("Session DB & Transcripts")]
    end

    subgraph Cloud [Google AI Cloud]
        LLM["Gemini Models"]
    end

    Msg --> Bot
    Bot --> Dispatcher
    Dispatcher -->|Dispatch Turn| MGY
    MGY --> AGY
    AGY -->|Prompts & Tool Calls| LLM
    LLM --> AGY
    AGY -->|Record State| DB
    DB -.->|Detects IDLE| Observer
    Observer --> Notif
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

| Command              | Description                                                    |
| :------------------- | :------------------------------------------------------------- |
| **Any text message** | Dispatches a task prompt to `mgy` using the active profile     |
| `/profiles`          | List all multigravity profiles and switch with tap buttons     |
| `/quotas`            | View quota usage across all profiles                           |
| `/conversations`     | View recent sessions with live status (`⚡ RUNNING` / `🟢 IDLE`) |
| `/new`               | Start a fresh session (clears conversation context)            |
| `/cancel`            | Abort/interrupt the currently running task                     |
| `/cd <path>`         | Change working directory on target host                        |
| `/pwd`               | View current working directory                                 |
| `/status`            | View host health, load, active profile, and runner state       |

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

If running the daemon directly on the remote device:
1. Copy `telegravity.service` to `/etc/systemd/system/`:
   ```bash
   sudo cp telegravity.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now telegravity.service
   ```
2. Check logs:
   ```bash
   journalctl -u telegravity.service -f
   ```
