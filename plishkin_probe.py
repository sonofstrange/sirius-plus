#!/usr/bin/env python3
"""Open the public VK feed and print the currently visible BPLA signal."""

from __future__ import annotations

import asyncio

from playwright.async_api import async_playwright

from plishkin_parser import PLISHKIN_URL, detect_bpla_status


async def main() -> int:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.goto(PLISHKIN_URL, wait_until="domcontentloaded", timeout=45_000)
            await page.wait_for_timeout(4_000)
            text = await page.locator("body").inner_text()
        finally:
            await browser.close()

    status = detect_bpla_status(text)
    if status:
        print(f"Plishkin BPLA signal: {status}")
    else:
        print("No current BPLA signal was found in the visible VK page text.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
