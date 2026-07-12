#!/usr/bin/env python3
"""Open the public VK feed and print the currently visible BPLA signal."""

from __future__ import annotations

import asyncio
import os

from plishkin_parser import detect_latest_bpla_status, fetch_recent_vk_posts


async def main() -> int:
    token = os.environ.get("VK_PLISHKIN_TOKEN")
    if not token:
        print("VK_PLISHKIN_TOKEN is not set; cannot read the VK wall reliably.")
        return 2

    posts = await asyncio.to_thread(fetch_recent_vk_posts, token)
    status = detect_latest_bpla_status(posts)
    if status:
        print(f"Plishkin BPLA signal: {status}")
    else:
        print("No BPLA signal was found in the latest VK posts.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
