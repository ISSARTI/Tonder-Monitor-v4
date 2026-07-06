"""
toender_watch_v4.py — Version 4 of the Tønder vielse slot monitor.
This version acts as the Cloud Monitor (to be hosted on Railway).
It:
  1. Scrapes slots headlessly on Railway.
  2. Performs server-side autobook using a pre-warmed page (no cold-start browser).
  3. Sends Telegram alerts.
  4. Implements robots.txt compliance, TTL caching, and endpoint circuit breakers.

V4 fixes applied:
  - wait_for_selector now targets button#submit-btn (visible) — root cause of v3 failures
  - slot selection uses direct page.evaluate(selectTime()) instead of DOM click chain
  - phone field filled with last-8-digit national DK number; jQuery intlTelInput API used first
  - JS_TEMPLATE handles "Fulde navn" / "full name" single-field forms
  - FAST MODE only blocks image/media (preserves scripts+CSS needed for intlTelInput)
  - Parallel booking attempts reduced from 6 to 3
  - Each category keeps a pre-warmed booking_page to eliminate cold-start latency
"""

import asyncio
import hashlib
import json
import os
import random
import re
import sys
import time
import urllib.parse
import http
from dataclasses import dataclass, field
from datetime import date, datetime, time as dttime, timedelta
from typing import List, Optional, Set, Tuple
import aiohttp
import requests
from bs4 import BeautifulSoup
from dateutil import parser as dtparser
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError, Browser
import websockets
import traceback

def clean_date_string(s: str) -> str:
    # Replace unicode superscript characters used in ordinals (e.g. ˢᵗ, ⁿᵈ, ʳᵈ, ᵗʰ)
    superscript_map = {
        "ˢ": "s", "ᵗ": "t", "ⁿ": "n", "ᵈ": "d", "ʳ": "r", "ʰ": "h"
    }
    cleaned = "".join(superscript_map.get(c, c) for c in s)
    return cleaned

def log_error(context: str, exc: Optional[Exception] = None) -> None:
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_line = f"[{now_str}] CONTEXT: {context}\n"
    if exc:
        log_line += f"EXCEPTION: {type(exc).__name__}: {exc}\n"
        log_line += f"TRACEBACK:\n{''.join(traceback.format_exception(type(exc), exc, exc.__traceback__))}\n"
    log_line += "-" * 80 + "\n"
    
    # Print to console
    print(f"❌ {context}" + (f" ({exc})" if exc else ""))
    
    # Save to error_log.txt
    try:
        data_dir = os.environ.get("DATA_DIR", ".")
        log_file_path = os.path.join(data_dir, "error_log.txt")
        with open(log_file_path, "a", encoding="utf-8") as f:
            f.write(log_line)
    except Exception as le:
        print(f"⚠️ Failed to write to error log file: {le}")

# Reconfigure stdout/stderr to use UTF-8 to prevent encoding errors on Windows
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# ── Load env.txt or .env if present ──────────────────────────────────────────
def load_env_file():
    search_dirs = [os.getcwd(), os.path.dirname(os.path.abspath(__file__))]
    filenames = ["env.txt", ".env"]
    for directory in search_dirs:
        for filename in filenames:
            path = os.path.join(directory, filename)
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if not line or line.startswith("#"):
                                continue
                            if line.startswith("set "):
                                line = line[4:].strip()
                            elif line.startswith("export "):
                                line = line[7:].strip()
                            if "=" in line:
                                key, val = line.split("=", 1)
                                key = key.strip()
                                val = val.strip().strip("'\"")
                                if key:
                                    os.environ[key] = val
                    return
                except Exception as e:
                    print(f"Warning: Failed to read env file {path}: {e}")

load_env_file()

# ---------------------------------------------------------------------------
# Config v3
# ---------------------------------------------------------------------------
@dataclass
class ConfigV4:
    url_without_witness: str = (
        "https://reservation.frontdesksuite.com/toender/vielse/ReserveTime/StartReservation"
        "?pageId=8d47364a-5e21-4e40-892d-e9f46878e18b"
        "&buttonId=9d98558f-9d2e-4a50-8124-adf00b4abfb0"
        "&culture=en"
    )
    url_with_witness: str = (
        "https://reservation.frontdesksuite.com/toender/vielse/ReserveTime/StartReservation"
        "?pageId=8d47364a-5e21-4e40-892d-e9f46878e18b"
        "&buttonId=073d59ae-ab0d-484a-90b1-e1f9b68a8843"
        "&culture=en"
    )

    check_intervals: List[int] = field(default_factory=lambda: [
        10, 12, 15, 18, 21, 24, 27, 30, 33
    ])

    cutoff_year: int = field(default_factory=lambda: int(os.getenv("CUTOFF_YEAR", "2027")))
    cutoff_month: int = field(default_factory=lambda: int(os.getenv("CUTOFF_MONTH", "12")))
    cutoff_day: int = field(default_factory=lambda: int(os.getenv("CUTOFF_DAY", "31")))

    seen_file: str = os.path.join(os.getenv("DATA_DIR", "."), "seen_slots.json")
    telegram_min_interval_seconds: int = 30 * 60
    telegram_max_items: int = 100
    heartbeat_interval_seconds: int = 3600
    headless: bool = True

    bot_user_agent: str = "TonderWeddingMonitorBot/3.0 (+mailto:ikoissa@gmail.com; Slot changes notifier)"
    respect_robots_txt: bool = False

    stealth_user_agents: List[str] = field(default_factory=lambda: [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    ])

    viewports: List[dict] = field(default_factory=lambda: [
        {"width": 1920, "height": 1080},
        {"width": 1440, "height": 900},
    ])


# ---------------------------------------------------------------------------
# Caching Layer (TTL Cache)
# ---------------------------------------------------------------------------
class TTLCache:
    def __init__(self, ttl_seconds: int = 300):
        self.ttl = ttl_seconds
        self.cache = {}

    def get(self, url: str) -> Optional[Tuple[List[datetime], List[datetime]]]:
        if url in self.cache:
            ts, clickable, locked = self.cache[url]
            if time.time() - ts < self.ttl:
                print(f"  [CACHE HIT] Serving cached data (age: {int(time.time() - ts)}s) for: {url[:60]}...")
                return clickable, locked
        return None

    def set(self, url: str, clickable: List[datetime], locked: List[datetime]):
        self.cache[url] = (time.time(), clickable, locked)
        print(f"  [CACHE MISS] Stored new upstream fetch results in cache for: {url[:60]}...")


# ---------------------------------------------------------------------------
# Robots.txt Parser
# ---------------------------------------------------------------------------
def check_robots_allowance(url: str, user_agent: str) -> Tuple[bool, Optional[int]]:
    try:
        parsed_url = urllib.parse.urlparse(url)
        robots_url = f"{parsed_url.scheme}://{parsed_url.netloc}/robots.txt"
        
        print(f"🔍 Auditing robots.txt: Fetching {robots_url}...")
        r = requests.get(robots_url, headers={"User-Agent": user_agent}, timeout=10)
        if r.status_code != 200:
            print(f"  No active robots.txt found (Status {r.status_code}). Access allowed by default.")
            return True, None
            
        content = r.text
        is_allowed = True
        crawl_delay = None
        
        user_agent_section_matches = False
        for line in content.split("\n"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
                
            parts = line.split(":", 1)
            if len(parts) < 2:
                continue
            key = parts[0].strip().lower()
            val = parts[1].strip()
            
            if key == "user-agent":
                user_agent_section_matches = (val == "*" or val.lower() in user_agent.lower())
                
            if user_agent_section_matches:
                if key == "disallow":
                    disallowed_path = val
                    if disallowed_path == "/" or parsed_url.path.startswith(disallowed_path):
                        is_allowed = False
                elif key == "crawl-delay":
                    try:
                        crawl_delay = int(val)
                    except ValueError:
                        pass
                        
        return is_allowed, crawl_delay
    except Exception as e:
        print(f"⚠️ Warning: Could not complete robots.txt audit: {e}")
        return True, None


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------
def get_telegram_credentials() -> tuple[Optional[str], Optional[str]]:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token or token == "your_token_here":
        token = None
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not chat_id or chat_id == "your_chat_id_here":
        chat_id = None
    return token, chat_id

def telegram_send(message: str) -> None:
    def _send():
        token, chat_id = get_telegram_credentials()
        if not token or not chat_id:
            return
        try:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": message,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True
                },
                timeout=10,
            )
        except Exception:
            pass
            
    import threading
    threading.Thread(target=_send, daemon=True).start()

def telegram_pin(message_id: int) -> bool:
    token, chat_id = get_telegram_credentials()
    if not token or not chat_id or not message_id:
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/pinChatMessage",
            json={
                "chat_id": chat_id,
                "message_id": message_id,
                "disable_notification": False
            },
            timeout=10,
        )
        data = r.json()
        if data.get("ok"):
            return True
        else:
            print(f"⚠️ Warning: Failed to pin message: {data.get('description')}")
            return False
    except Exception as e:
        print(f"⚠️ Error pinning message: {e}")
        return False

def telegram_urgent_alert(message: str) -> None:
    def _send_and_pin():
        token, chat_id = get_telegram_credentials()
        if not token or not chat_id:
            return
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": message,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True
                },
                timeout=10,
            )
            data = r.json()
            if data.get("ok"):
                msg_id = data.get("result", {}).get("message_id")
                if msg_id:
                    requests.post(
                        f"https://api.telegram.org/bot{token}/pinChatMessage",
                        json={
                            "chat_id": chat_id,
                            "message_id": msg_id,
                            "disable_notification": False
                        },
                        timeout=10,
                    )
        except Exception:
            pass

    import threading
    threading.Thread(target=_send_and_pin, daemon=True).start()

def telegram_send_photo(photo_bytes: bytes, caption: str) -> None:
    def _send_photo():
        token, chat_id = get_telegram_credentials()
        if not token or not chat_id:
            return
        try:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendPhoto",
                data={"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"},
                files={"photo": ("image.png", photo_bytes, "image/png")},
                timeout=15
            )
        except Exception as e:
            print(f"Failed to send Telegram photo: {e}")

    import threading
    threading.Thread(target=_send_photo, daemon=True).start()

def telegram_test() -> bool:
    token, chat_id = get_telegram_credentials()
    if not token or not chat_id:
        print("❌ TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set.")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": "✅ <b>Tønder V4 Cloud Monitor connected!</b>", "parse_mode": "HTML"},
            timeout=10,
        )
        return r.json().get("ok", False)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Browser Setup (Stealth Script)
# ---------------------------------------------------------------------------
_STEALTH_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
Object.defineProperty(navigator, 'languages', { get: () => ['en-DK', 'en', 'da'] });
window.chrome = { runtime: {} };
"""

def parse_slots(html: str) -> Tuple[List[datetime], List[datetime]]:
    soup = BeautifulSoup(html, "lxml")
    clickable: List[datetime] = []
    locked: List[datetime] = []

    for day_div in soup.select("div.date.one-queue"):
        header = day_div.select_one("span.header-text")
        if not header:
            continue
        day_text = header.get_text(strip=True)
        day_text_cleaned = clean_date_string(day_text)
        try:
            day = dtparser.parse(day_text_cleaned, fuzzy=True).date()
        except Exception:
            continue

        for ts in day_div.select("span.available-time"):
            parent = ts.parent
            is_clickable = False
            if parent and parent.name == "a":
                onclick = parent.get("onclick", "")
                if "selectTime" in onclick:
                    is_clickable = True
            try:
                dt = dtparser.parse(f"{day.isoformat()} {ts.get_text(strip=True)}", fuzzy=True)
                dt_clean = dt.replace(second=0, microsecond=0)
                if is_clickable:
                    clickable.append(dt_clean)
                else:
                    locked.append(dt_clean)
            except Exception:
                continue

    return sorted(list(set(clickable))), sorted(list(set(locked)))

def slots_fingerprint(slots: List[datetime]) -> str:
    payload = "|".join(s.isoformat(timespec="minutes") for s in slots)
    return hashlib.sha256(payload.encode()).hexdigest()


# ---------------------------------------------------------------------------
# aiohttp-based fast slot fetcher
# ---------------------------------------------------------------------------
_AIOHTTP_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-DK,en;q=0.9,da;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Referer": "https://www.toender.dk/",
    "Cache-Control": "no-cache",
}
_AIOHTTP_TIMEOUT = aiohttp.ClientTimeout(total=8, connect=4)


def _build_aiohttp_session(playwright_cookies: list, user_agent: str) -> aiohttp.ClientSession:
    """
    Create a lightweight aiohttp session pre-loaded with the same cookies and
    UA that the Playwright scraper context uses.  This lets subsequent monitoring
    requests skip the full browser round-trip and complete in ~300 ms instead of
    ~3-4 s.
    """
    # Flatten Playwright cookie dicts to a plain "name=value" string so the
    # server sees an identical Cookie header to what the browser would send.
    cookie_str = "; ".join(
        f"{ck['name']}={ck['value']}"
        for ck in playwright_cookies
        if ck.get("name") and ck.get("value")
    )
    headers = dict(_AIOHTTP_HEADERS)
    headers["User-Agent"] = user_agent
    if cookie_str:
        headers["Cookie"] = cookie_str

    connector = aiohttp.TCPConnector(ssl=True, limit=4)
    return aiohttp.ClientSession(
        headers=headers,
        connector=connector,
        timeout=_AIOHTTP_TIMEOUT,
        cookie_jar=aiohttp.DummyCookieJar(),  # we manage cookies via the header
    )


async def fetch_slots_aiohttp(
    session: aiohttp.ClientSession,
    ts_url: str,
) -> Tuple[List[datetime], List[datetime], str]:
    """
    Fetch the TimeSelection page via a plain HTTP GET and parse slots from
    the returned HTML.  ~10× faster than page.reload() because there is no
    browser overhead.  Returns (clickable, locked, html).
    """
    async with session.get(ts_url, allow_redirects=True) as resp:
        resp.raise_for_status()
        html = await resp.text(encoding="utf-8", errors="replace")

    if "one-queue" not in html and "reservation" not in html.lower():
        raise ValueError("aiohttp: calendar HTML not found — possible Cloudflare challenge or session expired")

    clickable, locked = parse_slots(html)
    return clickable, locked, html

from collections import defaultdict
def format_monthly_summary(clickable: List[datetime], locked: List[datetime]) -> str:
    clickable_by_month = defaultdict(int)
    locked_by_month = defaultdict(int)
    months = set()
    for s in clickable:
        month_str = s.strftime("%B %Y")
        clickable_by_month[month_str] += 1
        months.add((s.year, s.month, month_str))
    for s in locked:
        month_str = s.strftime("%B %Y")
        locked_by_month[month_str] += 1
        months.add((s.year, s.month, month_str))
    sorted_months = sorted(list(months), key=lambda x: (x[0], x[1]))
    return "\n".join(f"  • 📅 <b>{m[2]}</b>: <b>{clickable_by_month[m[2]]}</b> clickable / <b>{locked_by_month[m[2]]}</b> locked" for m in sorted_months)

def select_best_slot(slots: List[datetime]) -> datetime:
    """
    Selects the best slot according to preferences:
      1. Groups slots by date.
      2. For the earliest available date, prioritizes slots between 10:00 AM and 11:15 AM (inclusive).
      3. If no slot fits the time range on that day, falls back to the earliest slot on that day.
    """
    if not slots:
        return None
    # Sort slots chronologically
    sorted_slots = sorted(slots)
    
    # Group by date
    slots_by_date = defaultdict(list)
    for s in sorted_slots:
        slots_by_date[s.date()].append(s)
        
    # Get the earliest date
    earliest_date = min(slots_by_date.keys())
    earliest_day_slots = slots_by_date[earliest_date]
    
    # Try to find a slot between 10:00 and 11:15 on the earliest day
    pref_start = dttime(10, 0)
    pref_end = dttime(11, 15)
    for s in earliest_day_slots:
        if pref_start <= s.time() <= pref_end:
            return s
            
    # Fallback to the absolute earliest slot on that day
    return earliest_day_slots[0]

async def launch_browser(p, cfg: ConfigV4) -> Browser:
    launch_args = [
        "--no-sandbox",
        "--disable-blink-features=AutomationControlled",
        "--disable-infobars",
        "--disable-dev-shm-usage",
        "--disable-gpu"
    ]
    try:
        browser = await p.chromium.launch(headless=cfg.headless, channel="chrome", args=launch_args)
    except Exception:
        browser = await p.chromium.launch(headless=cfg.headless, args=launch_args)
    return browser

async def fetch_slots(cfg: ConfigV4, url: str, browser: Browser) -> Tuple[List[datetime], List[datetime]]:
    ua = cfg.bot_user_agent if cfg.respect_robots_txt else random.choice(cfg.stealth_user_agents)
    vp = random.choice(cfg.viewports)

    context = await browser.new_context(user_agent=ua, viewport=vp, locale="en-DK", timezone_id="Europe/Copenhagen")
    await context.add_init_script(_STEALTH_SCRIPT)
    
    try:
        page = await context.new_page()
        await page.set_extra_http_headers({
            "Accept-Language": "en-DK,en;q=0.9,da;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": "https://www.toender.dk/",
        })
        
        response = await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        
        if response:
            headers = response.headers
            if "retry-after" in headers:
                retry_val = headers["retry-after"]
                print(f"⚠️ Server returned Retry-After header: {retry_val} seconds.")
                try:
                    await asyncio.sleep(int(retry_val))
                except ValueError:
                    await asyncio.sleep(60)

        try:
            await page.wait_for_selector("div.date.one-queue", timeout=20_000)
        except PlaywrightTimeoutError:
            pass

        html = await page.content()
        
        if "one-queue" not in html and "reservation" not in html.lower():
            raise ValueError("Endpoint structure altered or Cloudflare bot challenge triggered.")

        return parse_slots(html)

    finally:
        await context.close()


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Server-side Booking Helpers & JS Template
# ---------------------------------------------------------------------------
def get_booking_details_from_env() -> dict:
    path = "booking_details.json"
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "partner1_first_name": os.getenv("PARTNER1_FIRST_NAME", ""),
        "partner1_last_name": os.getenv("PARTNER1_LAST_NAME", ""),
        "partner1_email": os.getenv("PARTNER1_EMAIL", ""),
        "partner1_phone": os.getenv("PARTNER1_PHONE", ""),
        "partner1_dob": os.getenv("PARTNER1_DOB", ""),
        "partner1_nationality": os.getenv("PARTNER1_NATIONALITY", ""),
        "partner1_birth_city": os.getenv("PARTNER1_BIRTH_CITY", ""),
        "partner1_birth_country": os.getenv("PARTNER1_BIRTH_COUNTRY", ""),
        "partner1_residence_place": os.getenv("PARTNER1_RESIDENCE_PLACE", ""),
        "partner1_address": os.getenv("PARTNER1_ADDRESS", ""),
        "partner1_gender": os.getenv("PARTNER1_GENDER", ""),
        "partner1_marital_status": os.getenv("PARTNER1_MARITAL_STATUS", ""),
        "partner1_cpr": os.getenv("PARTNER1_CPR", ""),
        "partner1_passport_ID_number": os.getenv("PARTNER1_PASSPORT_ID_NUMBER", ""),
        "partner1_passport_issue_place": os.getenv("PARTNER1_PASSPORT_ISSUE_PLACE", ""),
        "partner1_passport_issue_date": os.getenv("PARTNER1_PASSPORT_ISSUE_DATE", ""),
        "partner1_passport_expiry_date": os.getenv("PARTNER1_PASSPORT_EXPIRY_DATE", ""),
        
        "partner2_first_name": os.getenv("PARTNER2_FIRST_NAME", ""),
        "partner2_last_name": os.getenv("PARTNER2_LAST_NAME", ""),
        "partner2_email": os.getenv("PARTNER2_EMAIL", ""),
        "partner2_phone": os.getenv("PARTNER2_PHONE", ""),
        "partner2_dob": os.getenv("PARTNER2_DOB", ""),
        "partner2_nationality": os.getenv("PARTNER2_NATIONALITY", ""),
        "partner2_birth_city": os.getenv("PARTNER2_BIRTH_CITY", ""),
        "partner2_birth_country": os.getenv("PARTNER2_BIRTH_COUNTRY", ""),
        "partner2_residence_place": os.getenv("PARTNER2_RESIDENCE_PLACE", ""),
        "partner2_address": os.getenv("PARTNER2_ADDRESS", ""),
        "partner2_gender": os.getenv("PARTNER2_GENDER", ""),
        "partner2_marital_status": os.getenv("PARTNER2_MARITAL_STATUS", ""),
        "partner2_cpr": os.getenv("PARTNER2_CPR", ""),
        "partner2_passport_number": os.getenv("PARTNER2_PASSPORT_NUMBER", ""),
        "partner2_passport_issue_place": os.getenv("PARTNER2_PASSPORT_ISSUE_PLACE", ""),
        "partner2_passport_issue_date": os.getenv("PARTNER2_PASSPORT_ISSUE_DATE", ""),
        "partner2_passport_expiry_date": os.getenv("PARTNER2_PASSPORT_EXPIRY_DATE", ""),
        
        "sagsnummer": os.getenv("SAGSNUMMER", ""),
        "witness1_name": os.getenv("WITNESS1_NAME", ""),
        "witness1_address": os.getenv("WITNESS1_ADDRESS", ""),
        "witness2_name": os.getenv("WITNESS2_NAME", ""),
        "witness2_address": os.getenv("WITNESS2_ADDRESS", ""),
        
        "ceremony_language": os.getenv("CEREMONY_LANGUAGE", "English"),
        "ceremony_needed": os.getenv("CEREMONY_NEEDED", "No"),
        "invitees_count": os.getenv("INVITEES_COUNT", "0"),
        "password": os.getenv("PASSWORD", ""),
        "enable_autobook": True
    }

async def detect_dob_format(page) -> str:
    """
    Inspect the live ContactInfo page to determine the correct DOB format
    before injecting the autofill template.  Runs a single page.evaluate()
    so it adds < 5 ms of latency.

    Detection priority (most → least reliable):
      1. input type="date"        → YYYY-MM-DD
      2. maxlength=6              → DDMMYY
      3. maxlength=8              → DDMMYYYY
      4. data-val-regex-pattern   → pattern-based detection
      5. placeholder format hint  → e.g. "dd/mm/yyyy"
      6. fallback                 → DD-MM-YYYY (confirmed working on Tønder vielse)
    """
    try:
        fmt = await page.evaluate("""
            () => {
                var result = null;
                var inputs = document.querySelectorAll('input[type="text"], input[type="date"]');
                for (var i = 0; i < inputs.length; i++) {
                    var inp = inputs[i];
                    var lbl = '';
                    if (inp.id) {
                        var el = document.querySelector("label[for='" + inp.id + "']");
                        if (el) lbl = el.innerText.toLowerCase();
                    }
                    if (!lbl) {
                        var wrap = inp.closest('.form-group, .mdc-text-field');
                        if (wrap) {
                            var le = wrap.querySelector('label, .mdc-floating-label');
                            if (le) lbl = le.innerText.toLowerCase();
                        }
                    }
                    var ariaId = inp.getAttribute('aria-labelledby');
                    if (!lbl && ariaId) {
                        var ale = document.getElementById(ariaId);
                        if (ale) lbl = ale.innerText.toLowerCase();
                    }
                    var isdobField = lbl.includes('date of birth') || lbl.includes('dob') ||
                                     lbl.includes('fødselsdato') || lbl.includes('birthday') ||
                                     lbl.includes('birth date') || (inp.name||'').toLowerCase().includes('birth');
                    if (!isdobField) continue;

                    // Signal 1: HTML5 date input
                    if (inp.type === 'date') return 'YYYY-MM-DD';

                    // Signal 2: maxlength
                    var ml = parseInt(inp.getAttribute('maxlength'), 10);
                    if (ml === 6)  return 'DDMMYY';
                    if (ml === 8)  return 'DDMMYYYY';
                    if (ml === 10) {
                        // 10 chars covers DD/MM/YYYY, DD-MM-YYYY, DD.MM.YYYY — use placeholder
                        // to disambiguate; fall through to placeholder check below
                    }

                    // Signal 3: regex validator pattern (use string includes — avoids escaping complexity)
                    var rx = (inp.getAttribute('data-val-regex-pattern') || inp.getAttribute('pattern') || '').toLowerCase();
                    if (rx.includes('\\\\d{6}') || rx === '^\\\\d{6}$') return 'DDMMYY';
                    if (rx.includes('\\\\d{8}') || rx === '^\\\\d{8}$') return 'DDMMYYYY';
                    if (rx.includes('dd/mm/yyyy') || rx.includes('dd\\\\/mm\\\\/yyyy')) return 'DD/MM/YYYY';
                    if (rx.includes('dd-mm-yyyy')) return 'DD-MM-YYYY';

                    // Signal 4: placeholder hint
                    var ph = (inp.placeholder || '').toLowerCase();
                    if (ph.includes('yyyy-mm-dd')) return 'YYYY-MM-DD';
                    if (ph.includes('dd/mm/yyyy')) return 'DD/MM/YYYY';
                    if (ph.includes('dd-mm-yyyy')) return 'DD-MM-YYYY';
                    if (ph.includes('dd.mm.yyyy')) return 'DD.MM.YYYY';
                    if (ph.includes('mm/dd/yyyy')) return 'MM/DD/YYYY';
                    if (ph.includes('ddmmyy') && !ph.includes('yyyy')) return 'DDMMYY';
                    if (ph.includes('ddmmyyyy')) return 'DDMMYYYY';

                    // Found a DOB field but no format signal — return null to use fallback
                    result = 'UNKNOWN';
                    break;
                }
                return result;
            }
        """)
        if fmt and fmt != 'UNKNOWN':
            print(f"🗓️ DOB format detected from live form: {fmt}")
            return fmt
        elif fmt == 'UNKNOWN':
            print("🗓️ DOB field found but no format signal — using fallback: DD-MM-YYYY")
        else:
            print("🗓️ No DOB field found on page — using fallback: DD-MM-YYYY")
    except Exception as e:
        print(f"🗓️ DOB format detection failed ({e}) — using fallback: DD-MM-YYYY")
    return "DD-MM-YYYY"


# ---------------------------------------------------------------------------
# Page inspection snapshots — save + Telegram when calendar/form loads
# ---------------------------------------------------------------------------
def _snapshots_dir() -> str:
    path = os.path.join(os.getenv("DATA_DIR", "."), "snapshots")
    os.makedirs(path, exist_ok=True)
    return path


def _snapshot_telegram_enabled() -> bool:
    return os.getenv("SNAPSHOT_SEND_TELEGRAM", "1").strip().lower() not in ("0", "false", "no")


INSPECT_FORM_JS = """
() => {
    var result = {
        visible_fields: [],
        hidden_fields: [],
        section_headers: [],
        iti_config: null,
        submit_btn: null,
    };

    document.querySelectorAll('h2, h3, h4, legend, .section-header, .part-header').forEach(function(el) {
        var t = (el.innerText || '').trim();
        if (t && t.length < 100) result.section_headers.push(t);
    });

    document.querySelectorAll('input, select, textarea').forEach(function(inp) {
        var labelText = '';
        if (inp.id) {
            var lbl = document.querySelector("label[for='" + inp.id + "']");
            if (lbl) labelText = lbl.innerText.trim();
        }
        if (!labelText) {
            var wrap = inp.closest('.form-group, .section, .field-wrapper, .mdc-text-field');
            if (wrap) {
                var le = wrap.querySelector('label, .mdc-floating-label, .reservation-field-name');
                if (le) labelText = le.innerText.trim();
            }
        }
        var field = {
            tag: inp.tagName.toLowerCase(),
            type: inp.type || '',
            id: inp.id || '',
            name: inp.name || '',
            label: labelText,
            placeholder: inp.placeholder || '',
            maxlength: inp.getAttribute('maxlength') || '',
            pattern: inp.getAttribute('pattern') || '',
            data_val_regex_pattern: inp.getAttribute('data-val-regex-pattern') || '',
            required: !!inp.required,
            hidden: inp.type === 'hidden' || inp.offsetParent === null,
        };
        if (field.hidden || field.type === 'hidden') {
            if (['__requestverificationtoken', 'timehash', 'phonenumbercountrycallingcode',
                 'phonenumberiso2countrycode', 'sessionid'].some(function(k) {
                     return (field.name || '').toLowerCase().includes(k) || (field.id || '').toLowerCase().includes(k);
                 }) || field.name || field.id) {
                result.hidden_fields.push(field);
            }
        } else {
            result.visible_fields.push(field);
        }
    });

    var iti = { jquery_plugin: false, globals: false, phone_inputs: [] };
    if (window.jQuery && window.jQuery.fn && window.jQuery.fn.intlTelInput) iti.jquery_plugin = true;
    if (window.intlTelInputGlobals) iti.globals = true;
    document.querySelectorAll('input[type="tel"], input[name*="Phone"], #telephone, #PhoneNumber').forEach(function(inp) {
        var container = inp.closest('.iti, .intl-tel-input');
        var dialCode = container ? (container.querySelector('.iti__selected-dial-code, .selected-dial-code') || {}).innerText : '';
        var entry = {
            id: inp.id, name: inp.name, placeholder: inp.placeholder || '',
            maxlength: inp.getAttribute('maxlength') || '', dial_code: (dialCode || '').trim(),
        };
        if (window.jQuery && iti.jquery_plugin) {
            try {
                entry.iti_country_data = window.jQuery(inp).intlTelInput('getSelectedCountryData');
            } catch (e) {}
        }
        iti.phone_inputs.push(entry);
    });
    result.iti_config = iti;

    var submit = document.querySelector('button#submit-btn, button[type="submit"], input[type="submit"]');
    if (submit) {
        result.submit_btn = {
            id: submit.id || '', tag: submit.tagName.toLowerCase(),
            text: (submit.innerText || submit.value || '').trim(),
            visible: submit.offsetParent !== null,
        };
    }
    return result;
}
"""


def inspect_calendar_from_html(
    html: str,
    url: str,
    clickable: List[datetime],
    locked: List[datetime],
) -> dict:
    """Build a structured snapshot of the TimeSelection calendar page."""
    soup = BeautifulSoup(html, "lxml")
    days = []
    for day_div in soup.select("div.date.one-queue"):
        header_el = day_div.select_one("span.header-text")
        day_text = header_el.get_text(strip=True) if header_el else ""
        clickable_times: List[str] = []
        locked_times: List[str] = []
        sample_onclick = None
        for ts in day_div.select("span.available-time"):
            time_text = ts.get_text(strip=True)
            parent = ts.parent
            if parent and parent.name == "a":
                onclick = parent.get("onclick", "")
                if "selectTime" in onclick:
                    clickable_times.append(time_text)
                    if not sample_onclick:
                        sample_onclick = onclick[:300]
                else:
                    locked_times.append(time_text)
            else:
                locked_times.append(time_text)
        days.append({
            "header": day_text,
            "clickable_times": clickable_times,
            "locked_times": locked_times,
            "sample_onclick": sample_onclick,
        })

    time_hash = None
    for script in soup.find_all("script"):
        text = script.string or ""
        if "timeHash" in text:
            m = re.search(r"timeHash['\"]?\s*[:=]\s*['\"]([^'\"]+)['\"]", text)
            if m:
                time_hash = m.group(1)
                break

    hidden_names = [
        inp.get("name") or inp.get("id") or ""
        for inp in soup.select('input[type="hidden"]')
        if (inp.get("name") or inp.get("id"))
    ]

    return {
        "kind": "calendar",
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "url": url,
        "page_title": (soup.title.string or "").strip() if soup.title and soup.title.string else "",
        "summary": {
            "clickable_count": len(clickable),
            "locked_count": len(locked),
            "clickable_slots": [s.isoformat(timespec="minutes") for s in clickable[:30]],
            "locked_slots_sample": [s.isoformat(timespec="minutes") for s in locked[:15]],
        },
        "days": days[:20],
        "signals": {
            "has_one_queue": "one-queue" in html,
            "has_select_time": "selectTime" in html,
            "time_hash_present": bool(time_hash),
            "time_hash_prefix": (time_hash[:24] + "...") if time_hash and len(time_hash) > 24 else time_hash,
            "hidden_field_names": hidden_names[:20],
            "day_count": len(days),
        },
    }


async def inspect_contact_form_page(page) -> dict:
    """Inspect the live ContactInfo form (fields, phone widget, DOB hints)."""
    form_data = await page.evaluate(INSPECT_FORM_JS)
    dob_format = await detect_dob_format(page)

    dob_fields = [
        f for f in form_data.get("visible_fields", [])
        if any(k in (f.get("label") or "").lower() for k in (
            "birth", "fødselsdato", "dob", "født"
        )) or "birth" in (f.get("name") or "").lower()
    ]
    phone_fields = form_data.get("iti_config", {}).get("phone_inputs", [])

    return {
        "kind": "contact_form",
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "url": page.url,
        "dob_format_detected": dob_format,
        "dob_fields": dob_fields,
        "phone_fields": phone_fields,
        "section_headers": form_data.get("section_headers", [])[:15],
        "visible_fields": form_data.get("visible_fields", []),
        "hidden_fields": form_data.get("hidden_fields", []),
        "submit_btn": form_data.get("submit_btn"),
        "iti_config": {
            "jquery_plugin": form_data.get("iti_config", {}).get("jquery_plugin"),
            "globals": form_data.get("iti_config", {}).get("globals"),
        },
    }


def inspection_snapshot_fingerprint(snapshot: dict) -> str:
    """Hash structural content so we only re-alert when something meaningful changed."""
    payload = {
        "kind": snapshot.get("kind"),
        "url": snapshot.get("url"),
        "summary": snapshot.get("summary"),
        "signals": snapshot.get("signals"),
        "dob_format": snapshot.get("dob_format_detected"),
        "field_count": len(snapshot.get("visible_fields", [])),
        "field_labels": [f.get("label") for f in snapshot.get("visible_fields", [])[:40]],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def save_inspection_snapshot(snapshot: dict, category: str) -> str:
    """Persist snapshot JSON locally (latest + timestamped archive)."""
    kind = snapshot.get("kind", "unknown")
    snap_dir = _snapshots_dir()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    latest_path = os.path.join(snap_dir, f"{kind}_{category}_latest.json")
    archive_path = os.path.join(snap_dir, f"{kind}_{category}_{ts}.json")
    snapshot["category"] = category
    html_extra = snapshot.pop("_raw_html", None)
    body = json.dumps(snapshot, indent=2, ensure_ascii=False, default=str)
    for path in (latest_path, archive_path):
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
    if html_extra and os.getenv("SNAPSHOT_SAVE_HTML", "0").strip().lower() in ("1", "true", "yes"):
        html_path = os.path.join(snap_dir, f"{kind}_{category}_{ts}.html")
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html_extra)
    return latest_path


def format_inspection_telegram(snapshot: dict, saved_path: str = "") -> str:
    """Human-readable Telegram summary of an inspection snapshot."""
    kind = snapshot.get("kind", "page")
    cat = snapshot.get("category_name") or snapshot.get("category", "")
    lines = [f"📸 <b>{kind.upper().replace('_', ' ')} INSPECTION</b>"]
    if cat:
        lines.append(f"Category: <b>{cat}</b>")
    lines.append(f"🕐 {snapshot.get('captured_at', '')}")
    lines.append(f"🔗 <code>{snapshot.get('url', '')[:120]}</code>")

    if kind == "calendar":
        summary = snapshot.get("summary", {})
        lines.append("")
        lines.append(
            f"📊 <b>{summary.get('clickable_count', 0)}</b> clickable / "
            f"<b>{summary.get('locked_count', 0)}</b> locked"
        )
        for slot in summary.get("clickable_slots", [])[:8]:
            lines.append(f"  ✅ <code>{slot}</code>")
        if summary.get("clickable_count", 0) > 8:
            lines.append(f"  … +{summary['clickable_count'] - 8} more")
        for slot in summary.get("locked_slots_sample", [])[:3]:
            lines.append(f"  🔒 <code>{slot}</code>")
        sig = snapshot.get("signals", {})
        lines.append("")
        lines.append(
            f"🔑 selectTime: {'yes' if sig.get('has_select_time') else 'no'} | "
            f"timeHash: {'yes' if sig.get('time_hash_present') else 'no'} | "
            f"days: {sig.get('day_count', 0)}"
        )
        if sig.get("time_hash_prefix"):
            lines.append(f"   timeHash: <code>{sig['time_hash_prefix']}</code>")
        sample = None
        for day in snapshot.get("days", []):
            if day.get("sample_onclick"):
                sample = day["sample_onclick"]
                break
        if sample:
            lines.append(f"   onclick sample: <code>{sample[:80]}…</code>")

    elif kind == "contact_form":
        lines.append("")
        lines.append(f"🗓️ DOB format: <b>{snapshot.get('dob_format_detected', '?')}</b>")
        iti = snapshot.get("iti_config", {})
        lines.append(
            f"📞 intlTelInput: jQuery={'yes' if iti.get('jquery_plugin') else 'no'} | "
            f"globals={'yes' if iti.get('globals') else 'no'}"
        )
        for pf in snapshot.get("phone_fields", [])[:2]:
            lines.append(
                f"   phone: maxlength={pf.get('maxlength')} dial={pf.get('dial_code')} "
                f"placeholder=<code>{pf.get('placeholder', '')}</code>"
            )
        lines.append("")
        lines.append(f"<b>Visible fields ({len(snapshot.get('visible_fields', []))}):</b>")
        for f in snapshot.get("visible_fields", [])[:18]:
            lbl = f.get("label") or f.get("name") or f.get("id") or "?"
            extra = []
            if f.get("placeholder"):
                extra.append(f"ph={f['placeholder']}")
            if f.get("maxlength"):
                extra.append(f"max={f['maxlength']}")
            if f.get("data_val_regex_pattern"):
                extra.append(f"rx={f['data_val_regex_pattern'][:30]}")
            suffix = f" ({', '.join(extra)})" if extra else ""
            lines.append(f"  • {lbl} [{f.get('type', f.get('tag'))}]{suffix}")
        if len(snapshot.get("visible_fields", [])) > 18:
            lines.append(f"  … +{len(snapshot['visible_fields']) - 18} more (see JSON)")
        submit = snapshot.get("submit_btn")
        if submit:
            lines.append(f"\n✅ Submit: <code>{submit.get('text') or submit.get('id')}</code> visible={submit.get('visible')}")

    if saved_path:
        lines.append(f"\n💾 Saved: <code>{saved_path}</code>")
    return "\n".join(lines)


async def report_page_inspection(
    snapshot: dict,
    category: str,
    category_name: str = "",
    *,
    state: Optional[dict] = None,
    urgent: bool = False,
    page=None,
    send_photo: bool = False,
) -> str:
    """
    Save inspection snapshot and optionally notify Telegram.
    Uses fingerprint dedup via state['last_{kind}_inspection_fp'] when state is provided.
    Returns path to saved JSON file.
    """
    snapshot["category"] = category
    if category_name:
        snapshot["category_name"] = category_name

    fp = inspection_snapshot_fingerprint(snapshot)
    kind = snapshot.get("kind", "page")
    fp_key = f"last_{kind}_inspection_fp"

    if state is not None:
        is_first = fp_key not in state or state.get(fp_key) is None
        changed = state.get(fp_key) != fp
        if not is_first and not changed and not urgent:
            return ""
        state[fp_key] = fp

    saved_path = save_inspection_snapshot(snapshot, category)
    print(f"📸 Inspection saved ({kind}/{category}): {saved_path}")

    if _snapshot_telegram_enabled():
        msg = format_inspection_telegram(snapshot, saved_path)
        if send_photo and page is not None:
            try:
                shot = await page.screenshot(full_page=True)
                telegram_send_photo(
                    shot,
                    msg[:1020],
                )
            except Exception as e:
                print(f"⚠️ Inspection screenshot failed: {e}")
                if urgent:
                    telegram_urgent_alert(msg)
                else:
                    telegram_send(msg)
        elif urgent:
            telegram_urgent_alert(msg)
        else:
            telegram_send(msg)

    return saved_path


async def report_calendar_inspection(
    html: str,
    url: str,
    category: str,
    category_name: str,
    clickable: List[datetime],
    locked: List[datetime],
    state: dict,
    *,
    urgent: bool = False,
) -> str:
    """Inspect calendar HTML, save, and notify if new or changed."""
    snapshot = inspect_calendar_from_html(html, url, clickable, locked)
    if os.getenv("SNAPSHOT_SAVE_HTML", "0").strip().lower() in ("1", "true", "yes"):
        snapshot["_raw_html"] = html
    has_clickable = bool(clickable)
    return await report_page_inspection(
        snapshot, category, category_name,
        state=state,
        urgent=urgent or has_clickable,
    )


async def report_contact_form_inspection(
    page,
    category: str,
    category_name: str = "",
    target_slot: Optional[datetime] = None,
) -> str:
    """Inspect ContactInfo form before autofill; always saves + sends (booking-critical)."""
    snapshot = await inspect_contact_form_page(page)
    if target_slot:
        snapshot["target_slot"] = target_slot.isoformat(timespec="minutes")
    return await report_page_inspection(
        snapshot, category, category_name,
        state=None,
        urgent=True,
        page=page,
        send_photo=True,
    )


JS_TEMPLATE = """(function(){
  var details = %s;
  
  function formatDateForInput(dateStr, placeholder, inputEl) {
    if (!dateStr) return "";
    var parts = dateStr.split(/[-/.]/);
    if (parts.length !== 3) return dateStr;
    var day = parts[0], month = parts[1], year = parts[2];
    if (day.length === 4) {
      year = parts[0]; month = parts[1]; day = parts[2];
    }
    var yy = year.slice(-2);

    // Priority 0: explicit format detected from live form by detect_dob_format()
    var detected = details._dob_format || "";
    if (detected === "YYYY-MM-DD") return year + "-" + month + "-" + day;
    if (detected === "DD/MM/YYYY") return day + "/" + month + "/" + year;
    if (detected === "DD-MM-YYYY") return day + "-" + month + "-" + year;
    if (detected === "DD.MM.YYYY") return day + "." + month + "." + year;
    if (detected === "MM/DD/YYYY") return month + "/" + day + "/" + year;
    if (detected === "DDMMYY")     return day + month + yy;
    if (detected === "DDMMYYYY")   return day + month + year;

    // HTML5 date inputs expect YYYY-MM-DD
    if (inputEl && inputEl.type === 'date') {
      return year + "-" + month + "-" + day;
    }
    
    // Signal: maxlength attribute (most reliable for FrontDeskSuite forms)
    var maxLen = inputEl ? parseInt(inputEl.getAttribute('maxlength'), 10) : 0;
    if (maxLen === 6)  return day + month + year.slice(-2);  // DDMMYY (6 chars)
    if (maxLen === 8)  return day + month + year;             // DDMMYYYY (8 chars)

    // Signal: placeholder format hint
    var ph = (placeholder || "").toLowerCase();
    if (ph.includes("yyyy-mm-dd")) return year + "-" + month + "-" + day;
    if (ph.includes("dd/mm/yyyy")) return day + "/" + month + "/" + year;
    if (ph.includes("dd-mm-yyyy")) return day + "-" + month + "-" + year;
    if (ph.includes("dd.mm.yyyy")) return day + "." + month + "." + year;
    if (ph.includes("mm/dd/yyyy")) return month + "/" + day + "/" + year;
    if (ph.includes("mm-dd-yyyy")) return month + "-" + day + "-" + year;
    if (ph.includes("ddmmyy"))     return day + month + year.slice(-2);
    if (ph.includes("ddmmyyyy"))   return day + month + year;

    // Fallback: DD-MM-YYYY — confirmed working on Tønder vielse form
    return day + "-" + month + "-" + year;
  }

  function parsePhone(phoneStr) {
    if (!phoneStr) return { countryCode: "", localNumber: "" };
    var cleaned = phoneStr.replace(/[\\s\\(\\)\\-\\.]/g, "");
    
    var commonPrefixes = ["+32", "0032", "+45", "0045", "+49", "0049", "+971", "00971"];
    for (var i = 0; i < commonPrefixes.length; i++) {
      var pref = commonPrefixes[i];
      if (cleaned.startsWith(pref)) {
        var cc = pref.startsWith("00") ? "+" + pref.substring(2) : pref;
        var local = cleaned.substring(pref.length);
        return { countryCode: cc, localNumber: local };
      }
    }
    
    if (cleaned.startsWith("+")) {
      var match = cleaned.match(/^\\+(\\d{1,3})(.*)$/);
      if (match) {
        return { countryCode: "+" + match[1], localNumber: match[2].trim() };
      }
    } else if (cleaned.startsWith("00")) {
      var match = cleaned.match(/^00(\\d{1,3})(.*)$/);
      if (match) {
        return { countryCode: "+" + match[1], localNumber: match[2].trim() };
      }
    }
    return { countryCode: "", localNumber: cleaned };
  }

  function triggerSelect(selectEl, index) {
    if (!selectEl || index < 0 || index >= selectEl.options.length) return;
    try {
      selectEl.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
      selectEl.selectedIndex = index;
      selectEl.dispatchEvent(new Event('change', { bubbles: true }));
      selectEl.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
      selectEl.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    } catch(e) {
      selectEl.selectedIndex = index;
    }
  }

  function formatPhoneForInput(rawPhone, inputEl, defaultCountryCode) {
    if (!rawPhone) return "";
    var digitsOnly = rawPhone.replace(/\\D/g, "");
    var maxLen = inputEl ? parseInt(inputEl.getAttribute('maxlength'), 10) : 0;
    
    if (defaultCountryCode === "+32") {
      if (digitsOnly.startsWith("32")) {
        digitsOnly = digitsOnly.substring(2);
      } else if (digitsOnly.startsWith("0032")) {
        digitsOnly = digitsOnly.substring(4);
      }
      if (digitsOnly.startsWith("0")) {
        digitsOnly = digitsOnly.substring(1);
      }
    }
    
    if (maxLen && digitsOnly.length > maxLen) {
      digitsOnly = digitsOnly.substring(0, maxLen);
    }
    return digitsOnly;
  }

  function selectOptionFuzzy(inp, targetVal) {
    if (!inp || !targetVal) return false;
    var target = targetVal.toLowerCase().trim();
    
    var translations = {
      "belgium": ["belgien", "belgique", "be", "belgisk"],
      "denmark": ["danmark", "dk", "dansk", "+45", "45", "0045"],
      "germany": ["tyskland", "de", "deutschland", "tysk", "+49", "49", "0049"],
      "male": ["mand", "m"],
      "female": ["kvinde", "f"],
      "single": ["ugift", "enlig"],
      "married": ["gift"],
      "divorced": ["fraskilt"],
      "widowed": ["enke", "enkemand"]
    };
    
    var searchTerms = [target];
    if (translations[target]) {
      searchTerms = searchTerms.concat(translations[target]);
    }
    
    for (var i = 0; i < inp.options.length; i++) {
      var optVal = inp.options[i].value.toLowerCase().trim();
      var optText = inp.options[i].text.toLowerCase().trim();
      
      var isMatch = searchTerms.some(function(term) {
        return optVal === term || optText === term || optText.includes(term) || optVal.includes(term);
      });
      
      if (isMatch) {
        triggerSelect(inp, i);
        return true;
      }
    }
    return false;
  }

  function setCustomPhoneFlag(inputEl, targetCountryCode) {
    try {
      // v4: jQuery plugin API checked first — the live site uses
      // $(phone).intlTelInput({...}) (jQuery plugin), not the modern singleton API.
      if (window.jQuery && typeof window.jQuery(inputEl).intlTelInput === 'function') {
        try {
          window.jQuery(inputEl).intlTelInput("setCountry", targetCountryCode);
          console.log("☎️ Set country via jQuery intlTelInput plugin:", targetCountryCode);
          return;
        } catch(e) {
          console.log("⚠️ jQuery intlTelInput setCountry failed, trying singleton:", e);
        }
      }
      // Modern singleton API fallback
      if (window.intlTelInputGlobals && typeof window.intlTelInputGlobals.getInstance === 'function') {
        var iti = window.intlTelInputGlobals.getInstance(inputEl);
        if (iti && typeof iti.setCountry === 'function') {
          iti.setCountry(targetCountryCode);
          console.log("☎️ Set country via intlTelInput singleton:", targetCountryCode);
          return;
        }
      }
      // DOM click fallback
      var parent = inputEl.parentElement;
      if (parent) {
        var flagBtn = parent.querySelector(".iti__selected-flag, .selected-flag, .iti__flag-container, .flag-container");
        if (flagBtn) {
          flagBtn.click();
          var list = parent.querySelector("ul.iti__country-list, ul.country-list");
          if (!list) {
            list = document.querySelector("ul.iti__country-list, ul.country-list");
          }
          if (list) {
            var item = list.querySelector('li[data-country-code="' + targetCountryCode + '"], li[data-dial-code="' + (targetCountryCode === "dk" ? "45" : "49") + '"]');
            if (item) {
              item.click();
              console.log("☎️ Clicked custom country flag list item:", targetCountryCode);
            }
          }
        }
      }
    } catch (e) {
      console.log("⚠️ Error setting custom flag dropdown:", e);
    }
  }

  var fnameCount = 0;
  var lnameCount = 0;
  var dobCount = 0;
  var addrCount = 0;
  var emailCount = 0;
  var phoneCount = 0;
  var birthCityCount = 0;
  var birthCountryCount = 0;
  var resPlaceCount = 0;
  var natCount = 0;
  var genderCount = 0;
  var maritalCount = 0;
  var cprCount = 0;
  var passportCount = 0;
  var passportIssueCount = 0;
  var passportIssueDateCount = 0;
  var passportExpiryDateCount = 0;

  var inputs = document.querySelectorAll("input[type='text'], input[type='email'], input[type='tel'], input[type='checkbox'], input:not([type]), select, textarea, input[type='date']");
  inputs.forEach(function(inp) {
    if (inp.type === 'hidden' || inp.style.display === 'none') return;
    
    var labelText = "";
    var id = inp.id;
    if (id) {
      var lbl = document.querySelector("label[for='" + id + "']");
      if (lbl) labelText = lbl.innerText.toLowerCase();
    }
    if (!labelText) {
      var parentLabel = inp.closest('label');
      if (parentLabel) labelText = parentLabel.innerText.toLowerCase();
    }
    if (!labelText) {
      var closest = inp.closest('.form-group, .mdc-text-field');
      if (closest) {
        var lbl = closest.querySelector('label');
        if (lbl) labelText = lbl.innerText.toLowerCase();
      }
    }
    var placeholder = (inp.placeholder || "").toLowerCase();
    var name = (inp.name || "").toLowerCase();
    var search = (labelText + " " + name + " " + placeholder).trim();
    
    // Witnesses (matched first to exclude from general address/name list)
    if (search.includes("witness 1") || search.includes("vidne 1")) {
      if (search.includes("address") || search.includes("adresse") || search.includes("adress")) {
        inp.value = details.witness1_address || '';
      } else {
        inp.value = details.witness1_name || '';
      }
      return;
    } 
    if (search.includes("witness 2") || search.includes("vidne 2")) {
      if (search.includes("address") || search.includes("adresse") || search.includes("adress")) {
        inp.value = details.witness2_address || '';
      } else {
        inp.value = details.witness2_name || '';
      }
      return;
    }

    // First Name
    if (search.includes("first name") || search.includes("given name") || search.includes("fornavn")) {
      if (search.includes("partner 2") || search.includes("spouse") || search.includes("co-applicant") || search.includes("applicant 2") || fnameCount > 0) {
        inp.value = details.partner2_first_name || '';
        fnameCount++;
      } else {
        inp.value = details.partner1_first_name || '';
        fnameCount++;
      }
    } 
    // Last Name
    else if (search.includes("last name") || search.includes("surname") || search.includes("efternavn")) {
      if (search.includes("partner 2") || search.includes("spouse") || search.includes("co-applicant") || search.includes("applicant 2") || lnameCount > 0) {
        inp.value = details.partner2_last_name || '';
        lnameCount++;
      } else {
        inp.value = details.partner1_last_name || '';
        lnameCount++;
      }
    }
    // Full name (single combined field — confirmed present as "Fulde navn *" on tidsbestilling ContactInfo)
    else if (search.includes("fulde navn") || search.includes("fuldt navn") || search.includes("full name") || search.includes("full navn")) {
      inp.value = ((details.partner1_first_name || '') + ' ' + (details.partner1_last_name || '')).trim();
    } 
    // Email
    else if (search.includes("email") || search.includes("e-mail")) {
      if (search.includes("partner 2") || search.includes("spouse") || search.includes("co-applicant") || search.includes("applicant 2") || emailCount >= 2) {
        inp.value = details.partner2_email || '';
      } else {
        inp.value = details.partner1_email || '';
      }
      emailCount++;
    } 
    // Phone
    else if (search.includes("phone") || search.includes("mobile") || search.includes("telefon") || search.includes("mobil")) {
      var isPartner2 = search.includes("partner 2") || search.includes("spouse") || search.includes("co-applicant") || search.includes("applicant 2") || phoneCount > 0;
      
      if (inp.tagName.toLowerCase() === 'select') {
        if (!isPartner2) {
          selectOptionFuzzy(inp, "denmark");
        } else {
          var rawPhone = details.partner2_phone || '';
          var parsed = parsePhone(rawPhone);
          selectOptionFuzzy(inp, parsed.countryCode || "belgium");
        }
      } else {
        var targetCountry = "dk"; // Default for Partner 1
        if (!isPartner2) {
          var rawPhone = details.partner1_phone || '';
          var digitsOnly = rawPhone.replace(/\\D/g, "");
          // v4: use last 8 digits as the national DK number (intlTelInput shows +45 separately)
          inp.value = digitsOnly.slice(-8);
          targetCountry = "dk";
          phoneCount++;
        } else {
          var rawPhone = details.partner2_phone || '';
          var parsed = parsePhone(rawPhone);
          inp.value = formatPhoneForInput(parsed.localNumber || '', inp, parsed.countryCode);
          if (parsed.countryCode === "+49" || parsed.countryCode === "0049" || parsed.countryCode === "49") {
            targetCountry = "de";
          }
          phoneCount++;
        }
        
        // Trigger custom dropdown click/selection for this input field!
        setCustomPhoneFlag(inp, targetCountry);

        // v4: explicitly populate hidden phone meta-fields regardless of submit-btn click timing
        var ccInput = document.getElementById('PhoneNumberCountryCallingCode');
        var isoInput = document.getElementById('PhoneNumberIso2CountryCode');
        if (ccInput) ccInput.value = (targetCountry === 'dk') ? '45' : '49';
        if (isoInput) isoInput.value = targetCountry;
        
        // Dispatch input events to force form validation framework to update
        inp.dispatchEvent(new Event('input', { bubbles: true }));
        inp.dispatchEvent(new Event('change', { bubbles: true }));
      }
    }
    // DOB
    else if (search.includes("date of birth") || search.includes("dob") || search.includes("fødselsdato")) {
      if (search.includes("partner 2") || search.includes("co-applicant") || search.includes("applicant 2") || dobCount > 0) {
        inp.value = formatDateForInput(details.partner2_dob || '', placeholder, inp);
        dobCount++;
      } else {
        inp.value = formatDateForInput(details.partner1_dob || '', placeholder, inp);
        dobCount++;
      }
    } 
    // City of Birth
    else if (search.includes("city of birth") || search.includes("birthplace") || search.includes("place of birth") || search.includes("fødeby")) {
      if (search.includes("partner 2") || search.includes("co-applicant") || search.includes("applicant 2") || birthCityCount > 0) {
        inp.value = details.partner2_birth_city || '';
        birthCityCount++;
      } else {
        inp.value = details.partner1_birth_city || '';
        birthCityCount++;
      }
    } 
    // Country of Birth
    else if (search.includes("country of birth") || search.includes("fødeland")) {
      if (search.includes("partner 2") || search.includes("co-applicant") || search.includes("applicant 2") || birthCountryCount > 0) {
        inp.value = details.partner2_birth_country || '';
        birthCountryCount++;
      } else {
        inp.value = details.partner1_birth_country || '';
        birthCountryCount++;
      }
    } 
    // Address (excluding witness fields)
    else if (search.includes("address") || search.includes("adress") || search.includes("street") || search.includes("road") || search.includes("adresse") || search.includes("vej")) {
      if (search.includes("partner 2") || search.includes("co-applicant") || search.includes("applicant 2") || addrCount > 0) {
        inp.value = details.partner2_address || '';
        addrCount++;
      } else {
        inp.value = details.partner1_address || '';
        addrCount++;
      }
    } 
    // Residence place (excluding witness and birth fields)
    else if ((search.includes("residence") || search.includes("bopæl") || search.includes("city") || search.includes("town") || search.includes("postal") || search.includes("zip") || search.includes("postnummer") || search.includes("by")) && !search.includes("birth") && !search.includes("passport") && !search.includes("card") && !search.includes("document")) {
      if (search.includes("partner 2") || search.includes("co-applicant") || search.includes("applicant 2") || resPlaceCount > 0) {
        inp.value = details.partner2_residence_place || '';
        resPlaceCount++;
      } else {
        inp.value = details.partner1_residence_place || '';
        resPlaceCount++;
      }
    } 
    // Gender
    else if (search.includes("gender") || search.includes("sex") || search.includes("køn")) {
      var isPartner2 = search.includes("partner 2") || search.includes("spouse") || search.includes("co-applicant") || search.includes("applicant 2") || genderCount > 0;
      var val = isPartner2 ? details.partner2_gender : details.partner1_gender;
      genderCount++;
      if (inp.tagName.toLowerCase() === 'select') {
        selectOptionFuzzy(inp, val);
      } else {
        inp.value = val || '';
      }
    } 
    // Marital Status
    else if (search.includes("marital status") || search.includes("civilstand") || search.includes("status") || search.includes("divorced") || search.includes("single") || search.includes("widowed")) {
      var isPartner2 = search.includes("partner 2") || search.includes("spouse") || search.includes("co-applicant") || search.includes("applicant 2") || maritalCount > 0;
      var val = isPartner2 ? details.partner2_marital_status : details.partner1_marital_status;
      maritalCount++;
      if (inp.tagName.toLowerCase() === 'select') {
        selectOptionFuzzy(inp, val);
      } else {
        inp.value = val || '';
      }
    } 
    // CPR Number
    else if (search.includes("cpr") || search.includes("social security") || search.includes("cpr-nummer")) {
      if (search.includes("partner 2") || search.includes("co-applicant") || search.includes("applicant 2") || cprCount > 0) {
        inp.value = details.partner2_cpr || '';
        cprCount++;
      } else {
        inp.value = details.partner1_cpr || '';
        cprCount++;
      }
    } 
    // Passport Number
    else if (search.includes("passport number") || search.includes("passport no") || search.includes("pasnummer") || search.includes("passnummer") || search.includes("pas nr") || search.includes("id card number") || search.includes("id number") || search.includes("id-kort") || search.includes("id card no") || search.includes("document number")) {
      if (search.includes("partner 2") || search.includes("spouse") || search.includes("co-applicant") || search.includes("applicant 2") || passportCount > 0) {
        inp.value = details.partner2_passport_number || '';
        passportCount++;
      } else {
        inp.value = details.partner1_passport_ID_number || details.partner1_passport_number || '';
        passportCount++;
      }
    } 
    // Passport Location
    else if (search.includes("passport location") || search.includes("place of issue") || search.includes("udstedelsessted") || search.includes("issued by") || search.includes("passport place") || search.includes("id card issued") || search.includes("id issued") || search.includes("issuing authority") || search.includes("issuing country") || search.includes("udstedt af")) {
      if (search.includes("partner 2") || search.includes("spouse") || search.includes("co-applicant") || search.includes("applicant 2") || passportIssueCount > 0) {
        inp.value = details.partner2_passport_issue_place || '';
        passportIssueCount++;
      } else {
        inp.value = details.partner1_passport_issue_place || '';
        passportIssueCount++;
      }
    } 
    // Passport Issue Date
    else if ((search.includes("issue date") || search.includes("udstedelsesdato") || search.includes("id card issue") || search.includes("document issue")) && (search.includes("passport") || search.includes("id card") || search.includes("id-kort") || search.includes("document") || search.includes("issued"))) {
      if (search.includes("partner 2") || search.includes("spouse") || search.includes("co-applicant") || search.includes("applicant 2") || passportIssueDateCount > 0) {
        inp.value = formatDateForInput(details.partner2_passport_issue_date || '', placeholder, inp);
        passportIssueDateCount++;
      } else {
        inp.value = formatDateForInput(details.partner1_passport_issue_date || '', placeholder, inp);
        passportIssueDateCount++;
      }
    } 
    // Passport Expiration Date
    else if ((search.includes("expiry date") || search.includes("expiration date") || search.includes("udløbsdato") || search.includes("valid until") || search.includes("id card expiry") || search.includes("id expires") || search.includes("document expiry") || search.includes("gyldig til")) && (search.includes("passport") || search.includes("id card") || search.includes("id-kort") || search.includes("document") || search.includes("valid") || search.includes("expir"))) {
      if (search.includes("partner 2") || search.includes("spouse") || search.includes("co-applicant") || search.includes("applicant 2") || passportExpiryDateCount > 0) {
        inp.value = formatDateForInput(details.partner2_passport_expiry_date || '', placeholder, inp);
        passportExpiryDateCount++;
      } else {
        inp.value = formatDateForInput(details.partner1_passport_expiry_date || '', placeholder, inp);
        passportExpiryDateCount++;
      }
    }
    // Nationality
    else if (search.includes("nationality") || search.includes("citizenship") || search.includes("statsborgerskab")) {
      var isPartner2 = search.includes("partner 2") || search.includes("spouse") || search.includes("co-applicant") || search.includes("applicant 2") || natCount > 0;
      var val = isPartner2 ? details.partner2_nationality : details.partner1_nationality;
      natCount++;
      if (inp.tagName.toLowerCase() === 'select') {
        selectOptionFuzzy(inp, val);
      } else {
        inp.value = val || '';
      }
    } 
    // Sagsnummer
    else if (search.includes("sagsnummer") || search.includes("file number") || search.includes("case number")) {
      inp.value = details.sagsnummer || '';
    } 
    // Ceremony Language
    else if (search.includes("ceremony language") || search.includes("ceremonisprog") || search.includes("language") || search.includes("sprog")) {
      var val = details.ceremony_language || '';
      if (inp.tagName.toLowerCase() === 'select') {
        selectOptionFuzzy(inp, val);
      } else {
        inp.value = val || '';
      }
    } 
    // Ceremony Needed
    else if (search.includes("ceremony") || search.includes("ceremoni") || search.includes("vielsesceremoni")) {
      var val = details.ceremony_needed || 'No';
      var isYes = val.toLowerCase() === 'yes' || val.toLowerCase() === 'ja' || val.toLowerCase() === 'true' || val === '1';
      if (inp.type === 'checkbox') {
        inp.checked = isYes;
      } else if (inp.tagName.toLowerCase() === 'select') {
        selectOptionFuzzy(inp, isYes ? "yes" : "no");
      } else {
        inp.value = isYes ? 'Yes' : 'No';
      }
    }
    // Privacy Policy / GDPR / Terms & Conditions
    else if (
      search.includes("privacy") || search.includes("policy") || search.includes("gdpr") || search.includes("terms") ||
      search.includes("conditions") || search.includes("consent") || search.includes("data protection") ||
      search.includes("accept") || search.includes("agree") || search.includes("betingelser") || search.includes("vilkår") ||
      search.includes("persondata") || search.includes("fortrolighed") || search.includes("samtykke") ||
      search.includes("godkender") || search.includes("accepterer")
    ) {
      if (inp.type === 'checkbox') {
        inp.checked = true;
        console.log("Checked Privacy Policy / GDPR / Terms checkbox");
      }
    }
    // Invitees Count / Guests
    else if (search.includes("invitee") || search.includes("guest") || search.includes("attendee") || search.includes("gæst") || search.includes("inviteret")) {
      var val = (details.invitees_count || '0').toLowerCase();
      if (inp.tagName.toLowerCase() === 'select') {
        selectOptionFuzzy(inp, val);
      } else {
        inp.value = val === 'minimum' ? '0' : val;
      }
    }
    // Card Holder Name
    else if (search.includes("cardholder") || search.includes("holder") || search.includes("kortholder") || search.includes("navn på kort") || name.includes("cardholder") || (search.includes("card") && search.includes("name"))) {
      inp.value = details.card_holder_name || '';
    }
    // Card Number
    else if (search.includes("card number") || search.includes("cardno") || search.includes("kortnummer") || search.includes("credit card") || search.includes("kortnr") || name.includes("cardnumber")) {
      inp.value = details.card_number || '';
    }
    // Card CVC / CVV
    else if (search.includes("cvc") || search.includes("cvv") || search.includes("verification code") || search.includes("sikkerhedskode") || name.includes("cvc") || name.includes("cvv")) {
      inp.value = details.card_cvc || '';
    }
    // Card Expiry Month
    else if (search.includes("expiry month") || search.includes("expiration month") || search.includes("udløbsmåned") || (search.includes("month") && (search.includes("exp") || search.includes("card")))) {
      var val = details.card_expiry_month || '';
      if (inp.tagName.toLowerCase() === 'select') {
        selectOptionFuzzy(inp, val);
      } else {
        inp.value = val;
      }
    }
    // Card Expiry Year
    else if (search.includes("expiry year") || search.includes("expiration year") || search.includes("udløbsår") || (search.includes("year") && (search.includes("exp") || search.includes("card")))) {
      var val = details.card_expiry_year || '';
      if (inp.tagName.toLowerCase() === 'select') {
        selectOptionFuzzy(inp, val);
      } else {
        inp.value = val;
      }
    }
    // Password
    else if (search.includes("password") || search.includes("kodeord") || search.includes("adgangskode")) {
      inp.value = details.password || '';
    }
    
    var event = new Event('input', { bubbles: true });
    inp.dispatchEvent(event);
    var changeEvent = new Event('change', { bubbles: true });
    inp.dispatchEvent(changeEvent);
    var focusEvent = new Event('focus', { bubbles: true });
    inp.dispatchEvent(focusEvent);
    var blurEvent = new Event('blur', { bubbles: true });
    inp.dispatchEvent(blurEvent);
  });
  console.log("Autofill completed!");
})();"""

async def perform_server_autobook(cfg: ConfigV4, url: str, target_slot: datetime, category: str, fast_mode: bool = False, warm_page=None) -> Tuple[bool, str]:
    mode_label = "FAST MODE" if fast_mode else "STANDARD BACKUP MODE"
    print(f"⚡ Starting SERVER-SIDE HEADLESS AUTOBOOK ({mode_label}) for slot: {target_slot.strftime('%Y-%m-%d %H:%M')} ({category})")
    
    # 1. Get booking details from environment/file
    details = get_booking_details_from_env()
    if not details.get("partner1_email"):
        return False, "Skipping server-side booking: Environment variables or booking_details.json not configured on server."

    # js_code is compiled after DOB format detection (see below, after wait_for_selector)
    js_code = None

    # 3. Determine whether to use pre-warmed page (v4) or cold-start a new browser (fallback).
    _using_warm_page = False
    _cold_browser = None
    _cold_context = None

    try:
        if warm_page is not None and not warm_page.is_closed():
            _using_warm_page = True
            page = warm_page
            page.on("dialog", lambda dialog: asyncio.create_task(dialog.accept()))
            print(f"🔥 Using PRE-WARMED booking_page (url={page.url}) — reloading for fresh timeHash...")
            try:
                await page.reload(wait_until="domcontentloaded", timeout=20_000)
            except Exception as reload_err:
                print(f"⚠️ Warm page reload failed ({reload_err}), falling back to cold-start...")
                _using_warm_page = False

    except Exception as warm_err:
        print(f"⚠️ Warm page check failed ({warm_err}), falling back to cold-start...")
        _using_warm_page = False

    if not _using_warm_page:
        # Cold-start path (original v3 approach, kept as fallback)
        async with async_playwright() as p:
            ua = cfg.bot_user_agent if cfg.respect_robots_txt else random.choice(cfg.stealth_user_agents)
            vp = random.choice(cfg.viewports)

            _cold_browser = await launch_browser(p, cfg)
            _cold_context = await _cold_browser.new_context(user_agent=ua, viewport=vp, locale="en-DK", timezone_id="Europe/Copenhagen")
            await _cold_context.add_init_script(_STEALTH_SCRIPT)

            page = await _cold_context.new_page()
            page.on("dialog", lambda dialog: asyncio.create_task(dialog.accept()))

            if fast_mode:
                await page.route("**/*", lambda route:
                    route.abort() if route.request.resource_type in ["image", "media"]
                    else route.continue_()
                )

            return await _perform_booking_on_page(page, cfg, url, target_slot, category, js_code, fast_mode,
                                                   cleanup=lambda: _cold_context.close())

    # Warm-page path: proceed directly with existing page (already at TimeSelection)
    return await _perform_booking_on_page(page, cfg, url, target_slot, category, js_code, fast_mode, cleanup=None)


async def _perform_booking_on_page(page, cfg, url: str, target_slot: datetime, category: str,
                                    js_code: str, fast_mode: bool, cleanup) -> Tuple[bool, str]:
    """Shared booking logic used by both the warm-page and cold-start paths.
    
    For warm-page calls the page is already at TimeSelection after a reload.
    For cold-start calls the page is a fresh blank page that needs to goto(url).
    """
    mode_label = "FAST MODE (warm)" if fast_mode else "STANDARD MODE (cold)"
    print(f"⚡ _perform_booking_on_page [{mode_label}] for slot {target_slot.strftime('%Y-%m-%d %H:%M')} ({category})")

    try:
        # Navigate to the calendar URL only when the page has not already loaded it.
        if "timeselection" not in page.url.lower() and "contactinfo" not in page.url.lower():
            await page.set_extra_http_headers({
                "Accept-Language": "en-DK,en;q=0.9,da;q=0.8",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Referer": "https://www.toender.dk/",
            })
            print(f"🔗 Navigating to: {url}")
            await page.goto(url, wait_until="domcontentloaded", timeout=45_000)

        print("⌛ Waiting for calendar slots to load...")
        try:
            await page.wait_for_selector("div.date.one-queue", timeout=15000)
        except Exception as e:
            try:
                content = (await page.content()).lower()
                if "no times" in content or "ingen ledige tider" in content or "no slots" in content:
                    return False, "Calendar page loaded, but there are NO slots currently available."
            except Exception:
                pass
            return False, f"Calendar slots ('div.date.one-queue') failed to load: {e}"

        # Clear cookie banner if present
        try:
            cookie_btn = await page.query_selector("button.btn-warning, button:has-text('Accept necessary cookies')")
            if cookie_btn:
                print("🍪 Clearing cookie consent banner on server...")
                try:
                    await cookie_btn.click(timeout=3000)
                except Exception:
                    await page.evaluate("el => el.click()", cookie_btn)
                if not fast_mode:
                    await asyncio.sleep(1)
        except Exception:
            pass

        # Search for the target slot on the calendar
        print(f"🔍 Searching calendar for slot: {target_slot.strftime('%Y-%m-%d %H:%M')}")
        day_divs = await page.query_selector_all("div.date.one-queue")
        clicked = False
        for day_div in day_divs:
            header_el = await day_div.query_selector("span.header-text")
            if not header_el:
                continue
            day_text = await header_el.inner_text()
            day_text_cleaned = clean_date_string(day_text)
            try:
                day_val = dtparser.parse(day_text_cleaned, fuzzy=True).date()
            except Exception:
                continue

            if day_val != target_slot.date():
                continue

            # Expand accordion panel for this date
            accordion_header = await day_div.query_selector("a.title")
            if accordion_header:
                print("📖 Expanding date accordion panel on server...")
                try:
                    await accordion_header.click(timeout=3000)
                except Exception:
                    print("⚠️ Normal accordion click failed, retrying via JavaScript click...")
                    await page.evaluate("el => el.click()", accordion_header)
                await asyncio.sleep(1)

            time_spans = await day_div.query_selector_all("span.available-time")
            for ts in time_spans:
                ts_text = await ts.inner_text()
                try:
                    ts_time = dtparser.parse(ts_text, fuzzy=True).time()
                except Exception:
                    continue

                if ts_time.hour == target_slot.hour and ts_time.minute == target_slot.minute:
                    parent_el = await ts.evaluate_handle("el => el.parentElement")
                    parent_name = await parent_el.evaluate("el => el.tagName.toLowerCase()")
                    if parent_name == "a":
                        onclick_val = await parent_el.evaluate("el => el.getAttribute('onclick') || ''")
                        if "selectTime" in onclick_val:
                            # v4: call selectTime() directly — no DOM click delay
                            clean_call = onclick_val.replace("return false;", "").strip().rstrip(";")
                            print(f"⚡ Calling selectTime() directly for {target_slot.isoformat(sep=' ')}...")
                            try:
                                async with page.expect_navigation(timeout=15000):
                                    await page.evaluate(clean_call)
                            except Exception as ce:
                                if "contactinfo" not in page.url.lower():
                                    print(f"⚠️ selectTime() navigation exception: {ce}")
                                    raise ce
                                print("Navigation succeeded despite exception (already on ContactInfo).")
                            clicked = True
                            break
            if clicked:
                break

        if not clicked:
            return False, f"Target slot {target_slot.isoformat()} was not found or not clickable on the calendar page."

        # v4: wait for button#submit-btn which is only visible when the ContactInfo form
        # is fully rendered and jQuery has initialised (not the hidden #sessionid inputs).
        print("⌛ Waiting for ContactInfo form submit button to appear...")
        try:
            await page.wait_for_selector("button#submit-btn", state="visible", timeout=15000)
        except Exception as e:
            log_error(f"ContactInfo form failed to load for slot {target_slot.isoformat()} ({category})", e)
            return False, f"Form fields failed to load after selecting slot: {e}"

        # Detect DOB format from the live form before injecting — corrects the fallback
        # assumption if the form uses a different format (e.g. DDMMYY, YYYY-MM-DD, etc.)
        try:
            await report_contact_form_inspection(
                page, category, category_name=category, target_slot=target_slot
            )
        except Exception as insp_err:
            print(f"⚠️ Contact form inspection failed (non-fatal): {insp_err}")

        detected_dob_fmt = await detect_dob_format(page)
        details["_dob_format"] = detected_dob_fmt
        js_code = JS_TEMPLATE % json.dumps(details)

        # Inject autofill
        print("👉 Injecting Autofill Bookmarklet code...")
        await page.evaluate(js_code)

        if not fast_mode:
            try:
                filled_screenshot = await page.screenshot(full_page=True)
                telegram_send_photo(
                    filled_screenshot,
                    f"📋 <b>SERVER AUTOFILL PROOF SCREENSHOT</b>\n"
                    f"Slot: <b>{target_slot.strftime('%A %d %b %Y %H:%M')}</b> ({category})\n"
                    f"Form fields successfully populated on the server."
                )
            except Exception as se:
                print(f"Failed to capture filled form screenshot: {se}")

        # Click Submit button
        print("🚀 Clicking Confirm/Submit button...")
        submit_selectors = [
            "button#submit-btn",
            "button[type='submit']",
            "input[type='submit']",
            "button:has-text('Submit')",
            "button:has-text('Next')",
            "button:has-text('Confirm')",
            "button:has-text('Næste')",
            "button:has-text('Bekræft')",
            "button:has-text('Godkend')",
            "button.btn-confirm"
        ]

        submit_btn = None
        for sel in submit_selectors:
            try:
                submit_btn = await page.query_selector(sel)
                if submit_btn:
                    break
            except Exception:
                continue

        if not submit_btn:
            return False, "Could not locate a submit or confirm button on the page."

        try:
            await submit_btn.click(timeout=5000)
        except Exception:
            print("⚠️ Native submit click failed, retrying via JavaScript click...")
            await page.evaluate("el => el.click()", submit_btn)

        print("⌛ Waiting 5 seconds for page submission action...")
        await asyncio.sleep(5)

        try:
            post_submit_screenshot = await page.screenshot(full_page=True)
            telegram_send_photo(
                post_submit_screenshot,
                f"📋 <b>POST-SUBMIT STEP 1 SCREENSHOT</b>\n"
                f"Slot: <b>{target_slot.strftime('%A %d %b %Y %H:%M')}</b> ({category})\n"
                f"Current URL: {page.url}\n"
                f"Sent to verify result of first confirmation click."
            )
        except Exception as se:
            print(f"Failed to capture post-submit step 1 screenshot: {se}")

        page_content = (await page.content()).lower()
        page_url = page.url.lower()

        success_keywords = [
            "confirmed", "confirmation", "bekræftet", "bekræftelse",
            "reservation complete", "booking complete", "thank you",
            "tak", "receipt", "kvittering", "your appointment",
            "din aftale", "successfully booked"
        ]
        payment_keywords = [
            "payment", "checkout", "betal", "stripe", "nets", "quickpay", "creditcard", "kortnummer", "cardnumber"
        ]
        taken_keywords = [
            "no longer available", "ikke længere ledig", "optaget",
            "already booked", "taken", "valgte tidspunkt", "ikke længere ledigt"
        ]

        booking_confirmed = any(kw in page_content for kw in success_keywords)
        payment_redirected = any(kw in page_content or kw in page_url for kw in payment_keywords)
        slot_taken = any(kw in page_content for kw in taken_keywords)

        # Handle intermediate/multi-step forms
        if not booking_confirmed and not slot_taken and not payment_redirected:
            has_inputs = False
            try:
                inputs = await page.query_selector_all("input:not([type='hidden']), select, textarea")
                has_inputs = len(inputs) > 0
            except Exception:
                pass

            has_submit = False
            next_submit_btn = None
            for sel in submit_selectors:
                try:
                    next_submit_btn = await page.query_selector(sel)
                    if next_submit_btn:
                        has_submit = True
                        break
                except Exception:
                    continue

            if has_inputs or has_submit:
                print("⚠️ Intermediate step / confirmation review page detected! Performing secondary autofill and submit...")
                log_error(f"Multi-step intermediate screen detected at {page.url} for slot {target_slot.isoformat()}", ValueError("Multi-step intermediate page"))

                try:
                    await page.evaluate(js_code)
                except Exception as ae:
                    print(f"Failed to inject autofill on secondary page: {ae}")

                if next_submit_btn:
                    print("🚀 Clicking Secondary Confirm/Submit button...")
                    try:
                        await next_submit_btn.click(timeout=5000)
                    except Exception:
                        try:
                            await page.evaluate("el => el.click()", next_submit_btn)
                        except Exception:
                            pass

                    print("⌛ Waiting 5 seconds for secondary submission response...")
                    await asyncio.sleep(5)

                    page_content = (await page.content()).lower()
                    page_url = page.url.lower()
                    booking_confirmed = any(kw in page_content for kw in success_keywords)
                    payment_redirected = any(kw in page_content or kw in page_url for kw in payment_keywords)
                    slot_taken = any(kw in page_content for kw in taken_keywords)

                    try:
                        secondary_screenshot = await page.screenshot(full_page=True)
                        telegram_send_photo(
                            secondary_screenshot,
                            f"📋 <b>POST-SUBMIT STEP 2 SCREENSHOT</b>\n"
                            f"Slot: <b>{target_slot.strftime('%A %d %b %Y %H:%M')}</b> ({category})\n"
                            f"Current URL: {page.url}\n"
                            f"Sent after clicking secondary confirmation."
                        )
                    except Exception as se:
                        print(f"Failed to capture secondary screenshot: {se}")

        if booking_confirmed:
            try:
                success_screenshot = await page.screenshot(full_page=True)
                telegram_send_photo(
                    success_screenshot,
                    f"🎉 <b>BOOKING CONFIRMED SUCCESS SCREENSHOT</b>\n"
                    f"Slot: <b>{target_slot.strftime('%A %d %b %Y %H:%M')}</b> ({category})\n"
                    f"Congratulations! The wedding slot is fully booked."
                )
            except Exception as se:
                print(f"Failed to capture success screenshot: {se}")
            return True, "Booking confirmed successfully headlessly on server."
        elif slot_taken:
            try:
                taken_screenshot = await page.screenshot(full_page=True)
                telegram_send_photo(
                    taken_screenshot,
                    f"🚨 <b>SLOT BOOKED BY SOMEONE ELSE</b> 🚨\n"
                    f"Slot: <b>{target_slot.strftime('%A %d %b %Y %H:%M')}</b> ({category})\n"
                    f"This slot was taken by another user during the booking attempt."
                )
            except Exception:
                pass
            return False, "This slot was booked by someone else."
        elif payment_redirected:
            return False, "Redirected to payment gateway. Headless server cannot complete payment. Please use local bridge client or check phone."
        else:
            try:
                photo = await page.screenshot(full_page=True)
                telegram_send_photo(
                    photo,
                    f"⚠️ <b>Unconfirmed Submission State</b>\n"
                    f"Url: {page.url}\n"
                    f"Page Content was scanned but no success/payment keyword matched."
                )
            except Exception:
                pass
            return False, "Submission completed but page did not show confirmation or payment redirect."

    except Exception as e:
        log_error(f"Error in server-side booking for slot {target_slot.isoformat()} ({category})", e)
        try:
            page_content = (await page.content()).lower()
            taken_keywords_ex = [
                "no longer available", "ikke længere ledig", "optaget",
                "already booked", "taken", "valgte tidspunkt", "ikke længere ledigt"
            ]
            if any(kw in page_content for kw in taken_keywords_ex):
                screenshot_bytes = await page.screenshot(full_page=True)
                telegram_send_photo(
                    screenshot_bytes,
                    f"🚨 <b>SLOT BOOKED BY SOMEONE ELSE</b> 🚨\n"
                    f"Slot: <b>{target_slot.strftime('%A %d %b %Y %H:%M')}</b> ({category})\n"
                    f"This slot was taken by another user during the booking attempt."
                )
                return False, "This slot was booked by someone else."
        except Exception:
            pass

        try:
            screenshot_bytes = await page.screenshot(full_page=True)
            telegram_send_photo(
                screenshot_bytes,
                f"🚨 <b>SERVER HEADLESS AUTOBOOK FAILURE</b> 🚨\n"
                f"Slot: {target_slot.strftime('%Y-%m-%d %H:%M')} ({category})\n"
                f"Error: <code>{str(e)[:300]}</code>"
            )
        except Exception as se:
            print(f"Could not capture failure screenshot: {se}")
        return False, f"Error in server-side booking: {e}"
    finally:
        if cleanup is not None:
            try:
                await cleanup()
            except Exception:
                pass

# ---------------------------------------------------------------------------
# Booked Status Helpers & State
# ---------------------------------------------------------------------------
BOOKED_DATES = {
    "without_witness": None,
    "with_witness": None
}

def load_booked_appointments() -> dict:
    global BOOKED_DATES
    
    # 1. Load from environment variables (Critical for Railway ephemeral persistence)
    env_keys = {
        "without_witness": "BOOKED_WITHOUT_WITNESS",
        "with_witness": "BOOKED_WITH_WITNESS"
    }
    for k, env_var in env_keys.items():
        val = os.getenv(env_var)
        if val:
            try:
                BOOKED_DATES[k] = datetime.strptime(val.strip(), "%Y-%m-%d").date()
                print(f"📌 Loaded {k} booked date from environment variable: {val}")
            except Exception:
                pass

    # 2. Load from local JSON (as fallback/override)
    path = os.path.join(os.getenv("DATA_DIR", "."), "booked_appointments_v4.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                for k in ["without_witness", "with_witness"]:
                    # Only override if it wasn't already set by environment variable
                    if not BOOKED_DATES.get(k):
                        val = data.get(k)
                        if val:
                            BOOKED_DATES[k] = datetime.strptime(val, "%Y-%m-%d").date()
        except Exception:
            pass
    return BOOKED_DATES

def save_booked_appointment(category_key: str, booked_date: date):
    global BOOKED_DATES
    BOOKED_DATES[category_key] = booked_date
    path = os.path.join(os.getenv("DATA_DIR", "."), "booked_appointments_v4.json")
    try:
        serializable = {k: (v.isoformat() if v else None) for k, v in BOOKED_DATES.items()}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(serializable, f, indent=2)
        print(f"🎉 Saved booked appointment for {category_key} to local memory: {booked_date.isoformat()}")
        print(f"💡 [Railway Alert] Please add '{'BOOKED_WITHOUT_WITNESS' if category_key == 'without_witness' else 'BOOKED_WITH_WITNESS'}={booked_date.isoformat()}' to your Railway Environment Variables to persist this choice permanently.")
    except Exception as e:
        print(f"⚠️ Error saving booked appointment: {e}")

# Initialize state from disk at startup
try:
    load_booked_appointments()
    print(f"📌 Loaded existing secured bookings: {BOOKED_DATES}")
except Exception:
    pass


# ---------------------------------------------------------------------------
# WebSocket Communication Server
# ---------------------------------------------------------------------------
CONNECTED_CLIENTS: Set[websockets.WebSocketServerProtocol] = set()

async def ws_handler(websocket, path=None):
    global BOOKING_SECURED
    print(f"🔌 Local PC bridge client connected from {websocket.remote_address}")
    CONNECTED_CLIENTS.add(websocket)
    try:
        async for message in websocket:
            try:
                data = json.loads(message)
                event = data.get("event")
                if event == "log":
                    print(f"  [PC Client LOG]: {data.get('message')}")
                elif event == "alert":
                    print(f"  [PC Client ALERT]: {data.get('message')}")
                    telegram_urgent_alert(data.get("message"))
                elif event == "info":
                    print(f"  [PC Client INFO]: {data.get('message')}")
                    telegram_send(data.get("message"))
                elif event == "booking_secured":
                    booked_date_str = data.get("booked_date")
                    category = data.get("category")
                    print(f"🎉 Cloud received BOOKING_SECURED event: {booked_date_str} ({category})")
                    try:
                        dt_val = datetime.strptime(booked_date_str, "%Y-%m-%d").date()
                        save_booked_appointment(dt_val, category)
                        BOOKING_SECURED = True
                        telegram_send(
                            f"🎉 <b>BOOKING SECURED & CONFIRMED!</b>\n"
                            f"📅 Date: <b>{dt_val.strftime('%A %d %b %Y')}</b> ({category})\n\n"
                            f"🚫 Automated booking trigger broadcasts are now disabled. Monitor will remain active for alerts only."
                        )
                    except Exception as e:
                        print(f"❌ Failed to process booking secured message: {e}")
            except Exception as e:
                print(f"❌ Error decoding message from local bridge client: {e}")
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        CONNECTED_CLIENTS.remove(websocket)
        print(f"🔌 Local PC bridge client disconnected")

async def process_http_request(path: str, request_headers: websockets.Headers) -> Optional[Tuple[http.HTTPStatus, websockets.Headers, bytes]]:
    if path in ("/", "/health"):
        return http.HTTPStatus.OK, [("Content-Type", "text/plain")], b"OK"
    return None

async def broadcast_booking_trigger(url: str, target_slot: datetime, category: str):
    if not CONNECTED_CLIENTS:
        warning_msg = (
            "🚨 <b>WARNING: Slot Detected but Local PC Bridge is Offline!</b> 🚨\n"
            f"Slot: {target_slot.strftime('%A %d %b %Y %H:%M')} ({category})\n\n"
            "Please turn on your local PC bridge agent immediately so it can book automatically!"
        )
        print(warning_msg)
        telegram_urgent_alert(warning_msg)
        return

    payload = json.dumps({
        "action": "book_slot",
        "url": url,
        "target_slot": target_slot.isoformat(),
        "category": category
    })
    
    print(f"🔥 Broadcasting booking command for slot {target_slot.isoformat()} to {len(CONNECTED_CLIENTS)} client(s)...")
    await asyncio.gather(*(client.send(payload) for client in CONNECTED_CLIENTS))


# ---------------------------------------------------------------------------
# Slot Scraper Loop
# ---------------------------------------------------------------------------
def load_seen(path: str) -> Set[str]:
    if not os.path.exists(path):
        return set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return set(data) if isinstance(data, list) else set()
    except Exception:
        return set()

def save_seen(path: str, seen: Set[str]) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(sorted(seen), f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def is_high_frequency_window() -> bool:
    try:
        from zoneinfo import ZoneInfo
        cph_now = datetime.now(ZoneInfo("Europe/Copenhagen"))
    except Exception:
        cph_now = datetime.now()
        
    hour = cph_now.hour
    minute = cph_now.minute
    
    # Window 1: 11:30 PM to 12:30 AM CET (23:30 to 00:30 CET)
    in_window1 = (hour == 23 and minute >= 30) or (hour == 0 and minute <= 30)
    # Window 2: 04:00 AM to 05:30 AM CET (04:00 to 05:30 CET)
    in_window2 = (hour == 4) or (hour == 5 and minute <= 30)
    
    return in_window1 or in_window2

async def scraper_loop(cfg: ConfigV4):
    cutoff = date(cfg.cutoff_year, cfg.cutoff_month, cfg.cutoff_day)
    seen = load_seen(cfg.seen_file)
    cache = TTLCache(ttl_seconds=0)

    categories = {
        "without_witness": {
            "name": "Without Witness 🚫👥",
            "url": cfg.url_without_witness,
            "had_slots": False,
            "earliest_known": None,
            "last_fingerprint": "",
            "last_notified_at": 0.0,
            "failed_attempts": 0,
            "circuit_broken": False,
        },
        "with_witness": {
            "name": "With Witness 👥",
            "url": cfg.url_with_witness,
            "had_slots": False,
            "earliest_known": None,
            "last_fingerprint": "",
            "last_notified_at": 0.0,
            "failed_attempts": 0,
            "circuit_broken": False,
        }
    }

    last_status_sent_at = -cfg.heartbeat_interval_seconds
    backoff_factor = 1.0

    print("=== Tønder V4 Cloud Monitor Scraper Started ===")
    
    for key, state in categories.items():
        is_allowed, delay = check_robots_allowance(state["url"], cfg.bot_user_agent)
        if not is_allowed:
            print(f"🔴 ROBOTS.TXT: Path is DISALLOWED by the operator for {state['name']}!")
            if cfg.respect_robots_txt:
                print("⛔ Configuration blocks execution. Disabling monitoring.")
                state["circuit_broken"] = True
            else:
                print("⚠️ Configuration permits ignoring robots.txt (Running in Stealth UA mode). Proceeding...")
        if delay:
            print(f"⏱️ Robots.txt suggests a Crawl-delay of {delay} seconds.")

    if not telegram_test():
        print("Telegram credentials invalid. Stop.")
        return

    async with async_playwright() as p:
        browser = await launch_browser(p, cfg)
        print("🆕 Launched persistent Chromium browser for cloud scraping.")
        
        # Setup context and pages
        scraper_context = None
        aiohttp_session: Optional[aiohttp.ClientSession] = None
        _last_aiohttp_ua: str = ""
        
        async def _refresh_aiohttp_session():
            """Re-build the aiohttp session from the current scraper_context cookies."""
            nonlocal aiohttp_session, _last_aiohttp_ua
            try:
                if aiohttp_session and not aiohttp_session.closed:
                    await aiohttp_session.close()
            except Exception:
                pass
            if not scraper_context:
                aiohttp_session = None
                return
            try:
                cookies = await scraper_context.cookies()
                aiohttp_session = _build_aiohttp_session(cookies, _last_aiohttp_ua)
                print(f"⚡ aiohttp session built with {len(cookies)} cookies.")
            except Exception as ae:
                print(f"⚠️ Could not build aiohttp session: {ae}")
                aiohttp_session = None

        async def init_scraper_sessions():
            nonlocal scraper_context, _last_aiohttp_ua
            print("👉 Initializing persistent scraper browser context and pages...")
            try:
                if scraper_context:
                    await scraper_context.close()
            except Exception:
                pass
            
            ua = cfg.bot_user_agent if cfg.respect_robots_txt else random.choice(cfg.stealth_user_agents)
            _last_aiohttp_ua = ua
            vp = random.choice(cfg.viewports)
            scraper_context = await browser.new_context(user_agent=ua, viewport=vp, locale="en-DK", timezone_id="Europe/Copenhagen")
            await scraper_context.add_init_script(_STEALTH_SCRIPT)
            
            for key, state in categories.items():
                page = await scraper_context.new_page()
                await page.set_extra_http_headers({
                    "Accept-Language": "en-DK,en;q=0.9,da;q=0.8",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Referer": "https://www.toender.dk/",
                })
                # Block heavy assets on the scraper pages to load faster.
                # (Only image/media — keep scripts+CSS intact for intlTelInput.)
                await page.route("**/*", lambda route:
                    route.abort() if route.request.resource_type in ["image", "media"]
                    else route.continue_()
                )
                state["scraper_page"] = page
                state["initial_loaded"] = False
                state["ts_url"] = None  # Will be set after first page.goto() resolves redirect

                # v4: pre-warmed booking page — stays at TimeSelection so booking can
                # call selectTime() directly without a cold browser launch (~900 ms saved).
                try:
                    bp = await scraper_context.new_page()
                    await bp.set_extra_http_headers({
                        "Accept-Language": "en-DK,en;q=0.9,da;q=0.8",
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "Referer": "https://www.toender.dk/",
                    })
                    # Keep scripts+CSS so jQuery/intlTelInput work on the ContactInfo page.
                    await bp.route("**/*", lambda route:
                        route.abort() if route.request.resource_type in ["image", "media"]
                        else route.continue_()
                    )
                    await bp.goto(state["url"], wait_until="domcontentloaded", timeout=30_000)
                    state["booking_page"] = bp
                    state["booking_page_ready"] = True
                    print(f"🔥 Pre-warmed booking_page for '{key}' at: {bp.url}")
                except Exception as bp_err:
                    print(f"⚠️ Could not pre-warm booking page for '{key}': {bp_err}")
                    state["booking_page"] = None
                    state["booking_page_ready"] = False

            # Build the aiohttp session from the freshly established browser cookies.
            # (The scraper_page hasn't done its first goto yet, so cookies may be sparse —
            #  the session is refreshed again after the first successful Playwright load.)
            await _refresh_aiohttp_session()

        await init_scraper_sessions()
        cycle_counter = 0

        try:
            while True:
                cycle_failed = False
                cycle_counter += 1

                recycle_threshold = 500 if is_high_frequency_window() else 50
                if cycle_counter > recycle_threshold:
                    print(f"♻️ Recycling Chromium browser after {cycle_counter} cycles...")
                    try:
                        await browser.close()
                    except Exception:
                        pass
                    browser = await launch_browser(p, cfg)
                    await init_scraper_sessions()
                    cycle_counter = 1

                try:
                    if not browser.is_connected():
                        print("⚠️ Browser disconnected. Reconnecting...")
                        try:
                            await browser.close()
                        except Exception:
                            pass
                        browser = await launch_browser(p, cfg)
                        await init_scraper_sessions()
                except Exception as be:
                    print(f"❌ Failed browser check: {be}")

                try:
                    async def process_category(key, state):
                        nonlocal cycle_failed
                        if state["circuit_broken"]:
                            return None

                        print(f"Fetching slots for {state['name']}...")
                        
                        cached_data = cache.get(state["url"])
                        fetched_html = None
                        fetched_url = state.get("ts_url") or state["url"]
                        if cached_data is not None:
                            clickable_all, locked_all = cached_data
                        else:
                            try:
                                page = state.get("scraper_page")
                                if not page or page.is_closed():
                                    await init_scraper_sessions()
                                    page = state["scraper_page"]

                                if not state.get("initial_loaded", False):
                                    print(f"👉 Initial page load for {state['name']}...")
                                    await page.goto(state["url"], wait_until="domcontentloaded", timeout=30_000)
                                    state["ts_url"] = page.url  # capture the final URL after redirect
                                    state["initial_loaded"] = True
                                    # Refresh aiohttp session after the first real page load so it
                                    # picks up session cookies set by StartReservation.
                                    await _refresh_aiohttp_session()
                                    # Use the already-loaded content for this first cycle.
                                    html = await page.content()
                                    fetched_html = html
                                    fetched_url = page.url
                                    _aiohttp_direct = False
                                else:
                                    ts_url = state.get("ts_url") or state["url"]
                                    if aiohttp_session and not aiohttp_session.closed:
                                        # ── FAST PATH: plain HTTP GET via aiohttp ──────────────────────
                                        t0 = time.monotonic()
                                        try:
                                            clickable_all, locked_all, html = await fetch_slots_aiohttp(aiohttp_session, ts_url)
                                            fetched_html = html
                                            fetched_url = ts_url
                                            elapsed_ms = int((time.monotonic() - t0) * 1000)
                                            print(f"⚡ [aiohttp] {state['name']} fetched in {elapsed_ms} ms")
                                            cache.set(state["url"], clickable_all, locked_all)
                                            state["failed_attempts"] = 0
                                            _aiohttp_direct = True
                                        except Exception as aio_err:
                                            elapsed_ms = int((time.monotonic() - t0) * 1000)
                                            print(f"⚠️ [aiohttp] {state['name']} failed in {elapsed_ms} ms: {aio_err}  → falling back to Playwright")
                                            # Fall back to Playwright reload on aiohttp failure
                                            await page.reload(wait_until="domcontentloaded", timeout=30_000)
                                            html = await page.content()
                                            fetched_html = html
                                            fetched_url = page.url
                                            _aiohttp_direct = False
                                            # Refresh aiohttp session from updated cookies
                                            await _refresh_aiohttp_session()
                                    else:
                                        # ── SLOW PATH: Playwright reload (fallback when aiohttp not ready) ──
                                        print(f"👉 Reloading page (Playwright fallback) for {state['name']}...")
                                        await page.reload(wait_until="domcontentloaded", timeout=30_000)
                                        html = await page.content()
                                        fetched_html = html
                                        fetched_url = page.url
                                        _aiohttp_direct = False

                                if not _aiohttp_direct:
                                    try:
                                        await page.wait_for_selector("div.date.one-queue", timeout=3000)
                                    except Exception:
                                        pass

                                    html = await page.content()
                                    
                                    # Verify basic page integrity
                                    if "one-queue" not in html and "reservation" not in html.lower():
                                        state["initial_loaded"] = False
                                        raise ValueError("Page HTML structure missing slots container.")

                                    clickable_all, locked_all = parse_slots(html)
                                    cache.set(state["url"], clickable_all, locked_all)
                                    state["failed_attempts"] = 0
                                    if not fetched_html:
                                        fetched_html = html
                                        fetched_url = page.url
                            except Exception as e:
                                log_error(f"Error fetching slots for {state['name']}", e)
                                state["failed_attempts"] += 1
                                cycle_failed = True
                                # Reset initial loaded state on failure to trigger clean reload next cycle
                                state["initial_loaded"] = False
                                
                                if state["failed_attempts"] >= 15:
                                    state["circuit_broken"] = True
                                    err_msg = f"🔌 <b>CIRCUIT BREAKER TRIGGERED</b>\nCategory: {state['name']}\n15 consecutive failures. Suspension activated."
                                    telegram_send(err_msg)
                                return None

                        good = [s for s in clickable_all if s.date() < cutoff]
                        locked = [s for s in locked_all if s.date() < cutoff]
                        now_str = datetime.now().isoformat(sep=" ", timespec="seconds")

                        print(f"{now_str}  —  [{state['name']}] {len(good)} clickable / {len(locked)} locked before {cutoff}")

                        if fetched_html is not None:
                            try:
                                await report_calendar_inspection(
                                    fetched_html,
                                    fetched_url,
                                    key,
                                    state["name"],
                                    clickable_all,
                                    locked_all,
                                    state,
                                    urgent=bool(good),
                                )
                            except Exception as cal_insp_err:
                                print(f"⚠️ Calendar inspection failed (non-fatal): {cal_insp_err}")
                        
                        # ── Auto Booking WebSocket Broadcast Trigger ──
                        if good:
                            # 1. Calculate minimum allowed date (at least 2 days in the future in Copenhagen)
                            try:
                                from zoneinfo import ZoneInfo
                                cph_today = datetime.now(ZoneInfo("Europe/Copenhagen")).date()
                            except Exception:
                                cph_today = datetime.now().date()
                            
                            min_allowed_date = cph_today + timedelta(days=2)
                            
                            # Filter slots that have at least 2 days buffer
                            valid_slots = [s for s in good if s.date() >= min_allowed_date]
                            
                            if not valid_slots:
                                print(f"  [AUTOBOOK SKIPPED] All available slots are too close (before {min_allowed_date.strftime('%Y-%m-%d')}).")
                            else:
                                # 2. Check if booking for this specific category was already secured
                                booked_info = load_booked_appointments()
                                booked_date = booked_info.get(key)
                                
                                # If we already have a booking, filter valid_slots to only those that are upgrades (earlier than currently booked)
                                if booked_date is not None:
                                    valid_slots = [s for s in valid_slots if s.date() < booked_date]
                                    if valid_slots:
                                        print(f"🔥 Upgrade slots found! Currently booked: {booked_date.strftime('%Y-%m-%d')}. Better dates: {[s.strftime('%Y-%m-%d') for s in valid_slots]}")
                                    else:
                                        print(f"  [AUTOBOOK BYPASSED] No slots found earlier than currently booked {booked_date.strftime('%Y-%m-%d')} for {key}.")
                                
                                if valid_slots:
                                    # Select the best slots to try in parallel (cap to 3 to prevent cloud container memory exhaustion)
                                    # We sort them by date (earlier is better) so we attempt the best ones first
                                    sorted_slots = sorted(valid_slots)
                                    slots_to_try = sorted_slots[:3]
                                    
                                    print(f"⚡ Launching PARALLEL BOOKING ATTEMPTS (v4: 3 slots × FAST MODE only) for slots: {[s.strftime('%Y-%m-%d %H:%M') for s in slots_to_try]}")
                                    
                                    # Define wrapper task for parallel execution
                                    # v4: the best (first) slot gets the pre-warmed booking_page;
                                    # remaining slots cold-start their own browser.
                                    _warm = state.get("booking_page")
                                    if _warm and _warm.is_closed():
                                        _warm = None
                                    # Consume the warm page for the first slot only
                                    state["booking_page"] = None

                                    async def attempt_autobook(slot, fast_mode, warm_page=None):
                                        success, message = await perform_server_autobook(
                                            cfg, state["url"], slot, key,
                                            fast_mode=fast_mode, warm_page=warm_page
                                        )
                                        return slot, success, message, fast_mode

                                    tasks = []
                                    for idx, s in enumerate(slots_to_try):
                                        wp = _warm if idx == 0 else None
                                        tasks.append(asyncio.create_task(attempt_autobook(s, fast_mode=True, warm_page=wp)))
                                    
                                    booking_secured = False
                                    secured_slot = None
                                    
                                    try:
                                        for future in asyncio.as_completed(tasks):
                                            slot, success, message, fast_mode = await future
                                            mode_lbl = "FAST MODE" if fast_mode else "STANDARD BACKUP"
                                            if success:
                                                print(f"🎉 SUCCESS! Secured slot: {slot.strftime('%Y-%m-%d %H:%M')} via {mode_lbl}")
                                                booking_secured = True
                                                secured_slot = slot
                                                # Cancel all other pending booking tasks immediately
                                                for t in tasks:
                                                    if not t.done():
                                                        t.cancel()
                                                break
                                            else:
                                                print(f"❌ Attempt failed for slot {slot.strftime('%Y-%m-%d %H:%M')} ({mode_lbl}): {message}")
                                    except Exception as pe:
                                        print(f"⚠️ Error in parallel booking task runner: {pe}")
                                        
                                    if booking_secured and secured_slot:
                                        save_booked_appointment(key, secured_slot.date())
                                        telegram_send(
                                            f"🎉 <b>SERVER HEADLESS AUTOBOOK SUCCESS!</b>\n"
                                            f"📅 Date: <b>{secured_slot.strftime('%A %d %b %Y')}</b> at <code>{secured_slot.strftime('%H:%M')}</code> ({key})\n\n"
                                            f"🚫 Automated booking triggers for {key} are now disabled. Monitor will remain active for alerts/upgrades only."
                                        )
                                    else:
                                        # If all parallel attempts failed, report to Telegram and trigger local fallback for the best slot
                                        best_fallback_slot = select_best_slot(valid_slots)
                                        telegram_urgent_alert(
                                            f"🚨 <b>ALL PARALLEL AUTOBOOK ATTEMPTS FAILED!</b> 🚨\n"
                                            f"Category: {key}\n"
                                            f"Attempted: {', '.join(s.strftime('%H:%M') for s in slots_to_try)}\n\n"
                                            f"👉 Broadcasting trigger to local bridge client for {best_fallback_slot.strftime('%d-%m %H:%M')} as fallback..."
                                        )
                                        await broadcast_booking_trigger(state["url"], best_fallback_slot, key)

                        # ── Notification alerts ──
                        window_just_opened = bool(good) and not state["had_slots"]
                        if window_just_opened:
                            lines = [f"🔥 <b>[{state['name']}] BOOKING WINDOW JUST OPENED!</b> 🔥", f"🟢 <b>{len(good)}</b> slot(s) now available:", ""]
                            lines += [f"📅 <b>{s.strftime('%A %d %b %Y')}</b> at <code>{s.strftime('%H:%M')}</code>" for s in good[:cfg.telegram_max_items]]
                            lines.append(f"\n👉 <a href='{state['url']}'><b>Click here to book now!</b></a>")
                            telegram_urgent_alert("\n".join(lines))
                            state["last_notified_at"] = time.time()
                            state["last_fingerprint"] = slots_fingerprint(good)

                        # ── Earlier slot appeared ──
                        earlier_alert_sent = False
                        if good:
                            current_earliest = good[0]
                            if state["earliest_known"] is not None and current_earliest < state["earliest_known"]:
                                delta_days = (state["earliest_known"].date() - current_earliest.date()).days
                                lines = [
                                    f"🚨 <b>[{state['name']}] EARLIER SLOT APPEARED!</b> (<b>{delta_days}</b> days sooner)",
                                    f"✨ New earliest: <b>{current_earliest.strftime('%A %d %b %Y')}</b> at <code>{current_earliest.strftime('%H:%M')}</code>",
                                    f"📅 Previous was: {state['earliest_known'].strftime('%A %d %b %Y %H:%M')}",
                                    "",
                                    "<b>All open slots:</b>",
                                ]
                                lines += [f"📅 <b>{s.strftime('%A %d %b %Y')}</b> at <code>{s.strftime('%H:%M')}</code>" for s in good[:cfg.telegram_max_items]]
                                lines.append(f"\n👉 <a href='{state['url']}'><b>Click here to book now!</b></a>")
                                telegram_urgent_alert("\n".join(lines))
                                state["last_notified_at"] = time.time()
                                state["last_fingerprint"] = slots_fingerprint(good)
                                earlier_alert_sent = True
                            state["earliest_known"] = current_earliest
                        else:
                            state["earliest_known"] = None

                        # Track newly seen
                        newly_found = [s for s in good if f"{key}:{s.isoformat()}" not in seen]
                        if newly_found:
                            for s in newly_found:
                                seen.add(f"{key}:{s.isoformat()}")
                            save_seen(cfg.seen_file, seen)

                        # Reminder alerts
                        fp = slots_fingerprint(good)
                        changed = bool(good) and fp != state["last_fingerprint"] and not window_just_opened and not earlier_alert_sent
                        reminder = bool(good) and fp == state["last_fingerprint"] and (
                            time.time() - state["last_notified_at"] >= cfg.telegram_min_interval_seconds
                        )
                        if changed or reminder:
                            header = f"📅 <b>[{state['name']}] New slots available:</b>" if changed else f"📅 <b>[{state['name']}] Slots still open:</b>"
                            lines = [header, ""]
                            lines += [f"📅 <b>{s.strftime('%A %d %b %Y')}</b> at <code>{s.strftime('%H:%M')}</code>" for s in good[:cfg.telegram_max_items]]
                            lines.append(f"\n👉 <a href='{state['url']}'><b>Click here to book now!</b></a>")
                            if changed:
                                telegram_urgent_alert("\n".join(lines))
                            else:
                                telegram_send("\n".join(lines))
                            state["last_notified_at"] = time.time()
                            state["last_fingerprint"] = fp

                        state["had_slots"] = bool(good)
                        
                        cat_status = f"ℹ️ <b>{state['name']}</b>: <b>{len(good)}</b> clickable / <b>{len(locked)}</b> locked\n"
                        cat_status += format_monthly_summary(good, locked)
                        cat_status += f"\n👉 <a href='{state['url']}'><b>Go to category portal</b></a>\n"
                        return cat_status

                    # Execute all active categories concurrently in parallel
                    tasks = [
                        asyncio.create_task(process_category(k, s))
                        for k, s in categories.items()
                        if not s["circuit_broken"]
                    ]
                    
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                    
                    active_checks = 0
                    status_reports = []
                    for r in results:
                        if isinstance(r, Exception):
                            log_error("Parallel category task encountered error", r)
                            cycle_failed = True
                        elif r is not None:
                            active_checks += 1
                            status_reports.append(r)

                    if active_checks == 0 and all(s["circuit_broken"] for s in categories.values()):
                        print("All circuits are broken. Stopping scraper.")
                        telegram_urgent_alert("🔌 <b>Cloud Scraper stopped:</b> All municipality endpoints triggered the circuit breaker!")
                        sys.exit(1)

                except Exception as e:
                    log_error("Scraper Loop Error", e)
                    cycle_failed = True

                if cycle_failed:
                    backoff_factor = min(5.0, backoff_factor + 1.0)
                    # If failed, retry quickly (2.0s to 4.0s, capped at max 5s)
                    sleep_for = min(5.0, 1.5 + (0.5 * backoff_factor))
                    print(f"⚠️ Scrape cycle failed. Retrying quickly in: {sleep_for:.2f}s\n")
                else:
                    backoff_factor = 1.0
                    # Normal cycle check interval: random between 0.1 and 0.35 seconds to achieve ~4 check attempts per second for maximum competitive edge
                    sleep_for = random.uniform(0.1, 0.35)
                    print(f"⚡ Scrape cycle succeeded. Next check in: {sleep_for:.2f}s\n")
                
                await asyncio.sleep(sleep_for)
        finally:
            print("🛑 Closing Chromium scraper browser...")
            try:
                if aiohttp_session and not aiohttp_session.closed:
                    await aiohttp_session.close()
            except Exception:
                pass
            try:
                await browser.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Main Async Entrypoint
# ---------------------------------------------------------------------------
async def main():
    cfg = ConfigV4()
    
    port = int(os.environ.get("PORT", 8080))
    print(f"🔌 Starting WebSocket server on 0.0.0.0:{port}...")
    ws_server = await websockets.serve(
        ws_handler, 
        "0.0.0.0", 
        port, 
        process_request=process_http_request
    )
    
    await asyncio.gather(
        scraper_loop(cfg),
        ws_server.wait_closed()
    )


if __name__ == "__main__":
    if "--test-alert" in sys.argv:
        print("Testing urgent alert system...")
        load_env_file()
        test_msg = (
            "🔥 <b>TEST V4 URGENT NOTIFICATION FLOW</b> 🔥\n\n"
            "This is a simulation of a newly detected slot!\n"
            "📅 <b>Friday 10 Jul 2026</b> at <code>10:00</code>\n\n"
            "👉 <a href='https://reservation.frontdesksuite.com/toender/vielse'><b>Click here to book now!</b></a>"
        )
        telegram_urgent_alert(test_msg)
        print("Test alert sent. Check your group chat!")
    elif "--test-inspection" in sys.argv:
        async def _test_inspection():
            load_env_file()
            os.environ.setdefault("SNAPSHOT_SEND_TELEGRAM", "0")
            cfg = ConfigV4()
            from playwright.async_api import async_playwright as _pw
            async with _pw() as p:
                browser = await p.chromium.launch(headless=True)
                ctx = await browser.new_context(locale="en-DK")
                page = await ctx.new_page()
                await page.goto(cfg.url_without_witness, wait_until="domcontentloaded", timeout=30_000)
                html = await page.content()
                clickable, locked = parse_slots(html)
                state = {}
                path = await report_calendar_inspection(
                    html, page.url, "without_witness", "Without Witness 🚫👥",
                    clickable, locked, state, urgent=False,
                )
                print("\n--- Telegram preview ---")
                snap = inspect_calendar_from_html(html, page.url, clickable, locked)
                print(format_inspection_telegram(snap, path or "(deduped — no new save)"))
                await browser.close()
        asyncio.run(_test_inspection())
    else:
        asyncio.run(main())
