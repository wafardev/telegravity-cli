#!/usr/bin/env python3
"""
Test script to verify that the daemon spots when a task is finished in real time.
"""

import asyncio
import sys
from config import RUN_MODE, MGY_PROFILE
from mgy_runner import MgyRunner
from session_monitor import SessionMonitor


async def main():
    print("=== Testing Daemon Task Completion Detection ===")
    runner = MgyRunner(mode=RUN_MODE, default_profile=MGY_PROFILE)
    monitor = SessionMonitor(runner=runner, poll_interval=1)

    completion_event = asyncio.Event()

    async def on_completed(conv_info, output):
        print("\n" + "=" * 55)
        print("🔔 [DAEMON EVENT] Task completion detected in real time!")
        print(f"📌 Conversation ID : {conv_info.get('conversation_id')}")
        print(f"📌 Title           : {conv_info.get('title')}")
        print(f"📌 Status          : {conv_info.get('status')}")
        print(f"📌 Not Fully Idle  : {conv_info.get('not_fully_idle')}")
        print("-" * 55)
        print("📝 Output captured by daemon:")
        print(output.strip())
        print("=" * 55 + "\n")
        completion_event.set()

    monitor.on_task_completed(on_completed)

    # Start the monitor in the background
    monitor_task = asyncio.create_task(monitor.start())
    print("✅ SessionMonitor is actively watching conversation_summaries.db...")

    # Wait a moment for initial state snapshot
    await asyncio.sleep(2)

    # Now launch an agy/mgy task
    prompt = "Reply with 'Task Alpha complete' and nothing else."
    print(f"\n🚀 Launching task: {prompt}")

    # Run dispatch in background so monitor watches it in parallel
    dispatch_task = asyncio.create_task(
        runner.dispatch_task(prompt=prompt, continue_session=True)
    )

    # Wait for the monitor to catch the completion event
    try:
        await asyncio.wait_for(completion_event.wait(), timeout=30)
        print("🎉 SUCCESS: The daemon successfully caught the task finish event!")
    except asyncio.TimeoutError:
        print("❌ Timed out waiting for completion event.")
    finally:
        monitor.stop()
        monitor_task.cancel()
        await dispatch_task


if __name__ == "__main__":
    asyncio.run(main())
