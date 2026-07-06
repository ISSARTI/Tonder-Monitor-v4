"""
test_aiohttp_vs_playwright.py

Compares slot-detection speed and result parity between:
  A) Playwright page.goto() / page.content()   (old monitoring path)
  B) aiohttp async HTTP GET                    (new fast monitoring path)

Target URL: Tønder Ydelsesafdelingen (open service — gives real calendar HTML)
and the locked Tønder vielse URL (confirms HTML structure is present either way).

Run with:
    python test_aiohttp_vs_playwright.py
"""

import asyncio
import time
from typing import List, Tuple
from datetime import datetime

import aiohttp
from playwright.async_api import async_playwright

# ── reuse parse_slots and helpers from the main bot ───────────────────────────
from toender_watch_v4 import (
    parse_slots,
    fetch_slots_aiohttp,
    _build_aiohttp_session,
    _AIOHTTP_HEADERS,
)

# ── test targets ──────────────────────────────────────────────────────────────
TARGETS = {
    "ydelse_open": (
        "Ydelsesafdelingen (OPEN)",
        "https://reservation.frontdesksuite.com/toender/tidsbestilling/ReserveTime/StartReservation"
        "?buttonId=54433f5f-e31f-4408-bb05-740d45efca0a&pageId=54433f5f-e31f-4408-bb05-740d45efca0a"
        "&culture=da&uiCulture=da",
    ),
    "vielse_locked": (
        "Tønder Vielse WITHOUT WITNESS (locked)",
        "https://reservation.frontdesksuite.com/toender/vielse/ReserveTime/StartReservation"
        "?pageId=8d47364a-5e21-4e40-892d-e9f46878e18b"
        "&buttonId=9d98558f-9d2e-4a50-8124-adf00b4abfb0"
        "&culture=en",
    ),
}

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)


def _fmt_slots(clickable: List[datetime], locked: List[datetime]) -> str:
    return f"{len(clickable)} clickable / {len(locked)} locked"


async def run_playwright_fetch(url: str, label: str) -> Tuple[List[datetime], List[datetime], float]:
    """Single Playwright goto + content() — mirrors the old monitoring path."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(user_agent=UA, locale="en-DK")
        page = await ctx.new_page()
        await page.route(
            "**/*",
            lambda route: route.abort()
            if route.request.resource_type in ["image", "media"]
            else route.continue_(),
        )
        t0 = time.monotonic()
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        final_url = page.url
        html = await page.content()
        elapsed = time.monotonic() - t0

        # Extract cookies for aiohttp use
        cookies = await ctx.cookies()
        await browser.close()

    clickable, locked = parse_slots(html)
    print(f"\n{'='*60}")
    print(f"[PLAYWRIGHT]  {label}")
    print(f"  Final URL  : {final_url}")
    print(f"  Elapsed    : {elapsed*1000:.0f} ms")
    print(f"  Slots      : {_fmt_slots(clickable, locked)}")
    print(f"  Cookies    : {len(cookies)} cookies captured")
    if clickable:
        for s in clickable[:5]:
            print(f"    ✅ {s.strftime('%a %d %b %Y %H:%M')}")
    if locked:
        for s in locked[:5]:
            print(f"    🔒 {s.strftime('%a %d %b %Y %H:%M')}")

    return clickable, locked, elapsed, cookies, final_url


async def run_aiohttp_fetch(
    ts_url: str,
    label: str,
    playwright_cookies: list,
    n_repeats: int = 5,
) -> Tuple[List[datetime], List[datetime], float]:
    """
    N repeated aiohttp GETs to the TimeSelection URL — mirrors the fast
    monitoring path.  Reports timing for each and checks parity with
    Playwright results.
    """
    session = _build_aiohttp_session(playwright_cookies, UA)
    try:
        elapsed_times = []
        result_clickable = []
        result_locked = []
        for i in range(n_repeats):
            t0 = time.monotonic()
            try:
                clickable, locked, _html = await fetch_slots_aiohttp(session, ts_url)
                elapsed = time.monotonic() - t0
                elapsed_times.append(elapsed)
                result_clickable = clickable
                result_locked = locked
                print(
                    f"  [aiohttp #{i+1}]  {elapsed*1000:.0f} ms  →  "
                    f"{_fmt_slots(clickable, locked)}"
                )
            except Exception as e:
                elapsed = time.monotonic() - t0
                elapsed_times.append(elapsed)
                print(f"  [aiohttp #{i+1}]  {elapsed*1000:.0f} ms  →  ERROR: {e}")
    finally:
        await session.close()

    avg_ms = sum(elapsed_times) / len(elapsed_times) * 1000 if elapsed_times else 0
    print(f"\n{'='*60}")
    print(f"[AIOHTTP]  {label}")
    print(f"  Repeats    : {n_repeats}")
    print(f"  Avg latency: {avg_ms:.0f} ms")
    print(f"  Slots      : {_fmt_slots(result_clickable, result_locked)}")
    return result_clickable, result_locked, avg_ms


async def compare_target(key: str, label: str, start_url: str):
    print(f"\n{'#'*60}")
    print(f"TARGET: {label}")
    print(f"{'#'*60}")

    # 1. Playwright baseline — captures cookies + final TimeSelection URL
    pw_clickable, pw_locked, pw_elapsed, cookies, ts_url = await run_playwright_fetch(
        start_url, label
    )

    # 2. aiohttp fast path — uses cookies from Playwright session
    print(f"\n  aiohttp fetching: {ts_url}")
    aio_clickable, aio_locked, aio_avg_ms = await run_aiohttp_fetch(
        ts_url, label, cookies, n_repeats=5
    )

    # 3. Parity check
    print(f"\n{'─'*60}")
    print(f"PARITY CHECK  ({label})")
    match_c = sorted(pw_clickable) == sorted(aio_clickable)
    match_l = sorted(pw_locked) == sorted(aio_locked)
    print(f"  Clickable slots match : {'✅ YES' if match_c else '❌ NO'}")
    print(f"  Locked   slots match  : {'✅ YES' if match_l else '❌ NO'}")
    speedup = (pw_elapsed * 1000) / aio_avg_ms if aio_avg_ms > 0 else float("inf")
    print(f"  Speed-up              : {speedup:.1f}×  "
          f"({pw_elapsed*1000:.0f} ms Playwright  →  {aio_avg_ms:.0f} ms aiohttp avg)")


async def main():
    print("=" * 60)
    print("aiohttp vs Playwright  —  signal parity & speed test")
    print("=" * 60)

    # Test the open Ydelsesafdelingen URL first (real calendar, real slots)
    label, url = TARGETS["ydelse_open"]
    await compare_target("ydelse_open", label, url)

    # Test the locked vielse URL (confirms HTML structure present even when locked)
    label, url = TARGETS["vielse_locked"]
    await compare_target("vielse_locked", label, url)

    print("\n\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
