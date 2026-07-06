"""
run_uat_test_v4.py — UAT verification for the V4 bot fixes.

Tests performed (in order):
  PHASE 1 — Real Tønder "Without Witness" calendar:
    1. Navigate to without_witness vielse URL → TimeSelection calendar
    2. Confirm calendar loads, count clickable / locked slots
    3. List September date entries
    4. Highlight first slot span (even if locked)
    5. Screenshot + Telegram

  PHASE 2 — Full form pipeline on local dummy form (dummy_autofill_test.html):
    (runs when no clickable slot found on the real calendar)
    1. Open dummy_autofill_test.html via file:// URL
    2. Call detect_dob_format() — should detect "DD-MM-YYYY" from placeholder
    3. Compile JS_TEMPLATE with detected _dob_format
    4. Inject autofill → verify all fields: phone, email, sagsnummer,
       PART 1 first name/last name/dob/address/email,
       PART 2 first name/last name/dob/address/email
    5. Screenshot filled form
    6. DO NOT submit

This script stops short of submitting to avoid creating real bookings.
"""

import asyncio
import os
import sys
import json
import re
import requests
from pathlib import Path
from datetime import datetime
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

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

# Import from v4 bot
try:
    from toender_watch_v4 import JS_TEMPLATE, get_booking_details_from_env, load_env_file, detect_dob_format
    print("✅ Imported JS_TEMPLATE, detect_dob_format and helpers from toender_watch_v4")
except ImportError as ie:
    print(f"⚠️ Could not import from toender_watch_v4: {ie}")
    JS_TEMPLATE = "(function(){})()"
    async def detect_dob_format(page): return "DD-MM-YYYY"
    def get_booking_details_from_env(): return {}
    def load_env_file(): pass

load_env_file()

# ---------------------------------------------------------------------------
# Telegram helpers
# ---------------------------------------------------------------------------
def _tg_creds():
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    return token, chat_id

def tg_send(msg: str) -> bool:
    token, chat_id = _tg_creds()
    if not token or not chat_id:
        print(f"[TG] {msg[:120]}")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=15,
        )
        return r.json().get("ok", False)
    except Exception as e:
        print(f"❌ TG send failed: {e}")
        return False

def tg_photo(photo_bytes: bytes, caption: str) -> bool:
    token, chat_id = _tg_creds()
    if not token or not chat_id:
        print(f"[TG PHOTO] {caption[:80]}")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendPhoto",
            data={"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"},
            files={"photo": ("shot.png", photo_bytes, "image/png")},
            timeout=25,
        )
        return r.json().get("ok", False)
    except Exception as e:
        print(f"❌ TG photo failed: {e}")
        return False

# ---------------------------------------------------------------------------
# Stealth script
# ---------------------------------------------------------------------------
_STEALTH = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
Object.defineProperty(navigator, 'languages', { get: () => ['en-DK', 'en', 'da'] });
window.chrome = { runtime: {} };
"""

WITHOUT_WITNESS_URL = (
    "https://reservation.frontdesksuite.com/toender/vielse/ReserveTime/StartReservation"
    "?pageId=8d47364a-5e21-4e40-892d-e9f46878e18b"
    "&buttonId=9d98558f-9d2e-4a50-8124-adf00b4abfb0"
    "&culture=en"
)

DUMMY_FORM_PATH = Path(__file__).parent / "dummy_autofill_test.html"


# ---------------------------------------------------------------------------
# PHASE 1 — real Tønder calendar
# ---------------------------------------------------------------------------
async def phase1_toender_calendar(page, details: dict) -> dict:
    results = {
        "calendar_loaded": False,
        "clickable_count": 0,
        "locked_count": 0,
        "sep_dates": [],
        "first_clickable": None,
        "selecttime_called": False,
        "submit_btn_found": False,
        "dob_format_detected": None,
        "autofill_ok": False,
        "errors": [],
    }

    print(f"\n{'='*65}")
    print("  PHASE 1 — Tønder Without Witness calendar")
    print(f"{'='*65}")

    await page.goto(WITHOUT_WITNESS_URL, wait_until="domcontentloaded", timeout=45_000)
    await page.wait_for_timeout(2500)

    # Accept cookies
    for sel in ["button.btn-warning", "button:text('Accept necessary cookies')", "button:text('Accepter')"]:
        try:
            btn = await page.query_selector(sel)
            if btn and await btn.is_visible():
                await btn.click()
                await page.wait_for_timeout(1200)
                print("  Cookies accepted.")
                break
        except Exception:
            pass

    # Wait for calendar
    try:
        await page.wait_for_selector("div.date.one-queue", timeout=20_000)
        results["calendar_loaded"] = True
        print(f"  ✅ Calendar loaded — URL: {page.url}")
    except PlaywrightTimeoutError:
        print("  ⚠️  Calendar not loaded (no div.date.one-queue)")
        results["errors"].append("Calendar not loaded")

    cal_shot = await page.screenshot(full_page=True)
    tg_photo(cal_shot, "<b>UAT V4 [Without Witness]</b> — Calendar loaded")

    # Count slots + find September entries
    day_divs = await page.query_selector_all("div.date.one-queue")
    print(f"  Date blocks: {len(day_divs)}")
    first_clickable = None
    for dd in day_divs:
        hdr = await dd.query_selector("span.header-text")
        hdr_text = (await hdr.inner_text()).strip() if hdr else ""
        if "sep" in hdr_text.lower() or "3" in hdr_text:
            results["sep_dates"].append(hdr_text)
        spans = await dd.query_selector_all("span.available-time")
        for ts in spans:
            ph = await ts.evaluate_handle("el => el.parentElement")
            oc = await ph.evaluate("el => el.getAttribute('onclick') || ''")
            if "selectTime" in oc:
                results["clickable_count"] += 1
                if first_clickable is None:
                    first_clickable = oc
            else:
                results["locked_count"] += 1

    results["first_clickable"] = first_clickable
    print(f"  Slots: {results['clickable_count']} clickable / {results['locked_count']} locked")
    print(f"  September entries: {results['sep_dates'][:5]}")

    # Highlight first slot span (even if locked)
    if day_divs:
        first_day = day_divs[0]
        acc_hdr = await first_day.query_selector("a.title")
        if acc_hdr:
            try:
                await acc_hdr.click(timeout=3000)
                await page.wait_for_timeout(1000)
            except Exception:
                pass
        first_span = await first_day.query_selector("span.available-time")
        if first_span:
            await page.evaluate("""
                (el) => {
                    el.style.outline = '4px solid red';
                    el.style.backgroundColor = 'yellow';
                    el.scrollIntoView({ behavior: 'smooth', block: 'center' });
                }
            """, first_span)
            await page.wait_for_timeout(600)
            hi_shot = await page.screenshot(full_page=False)
            tg_photo(hi_shot, "<b>UAT V4 [Without Witness]</b> — First slot span highlighted")

    tg_send(
        f"<b>UAT V4 [Without Witness] — Phase 1</b>\n"
        f"  Calendar loaded: {'✅' if results['calendar_loaded'] else '❌'}\n"
        f"  Clickable: <b>{results['clickable_count']}</b> | Locked: <b>{results['locked_count']}</b>\n"
        f"  September dates: {', '.join(results['sep_dates'][:5]) or 'none visible yet'}"
    )

    # If a clickable slot exists on the real form, run the full pipeline there
    if first_clickable:
        print(f"\n  Clickable slot found on real form — running full pipeline here.")
        clean = first_clickable.replace("return false;", "").strip().rstrip(";")
        print(f"  Calling: {clean[:120]}")
        try:
            async with page.expect_navigation(timeout=15000):
                await page.evaluate(clean)
            results["selecttime_called"] = True
            print(f"  selectTime() OK — at: {page.url}")
        except Exception as nav_err:
            if "contactinfo" in page.url.lower():
                results["selecttime_called"] = True
            else:
                results["errors"].append(f"selectTime failed: {nav_err}")

        try:
            await page.wait_for_selector("button#submit-btn", state="visible", timeout=15000)
            results["submit_btn_found"] = True
            print("  ✅ button#submit-btn visible")
        except Exception as se:
            results["errors"].append(f"submit-btn not found: {se}")

        if results["submit_btn_found"]:
            fmt = await detect_dob_format(page)
            results["dob_format_detected"] = fmt
            details["_dob_format"] = fmt
            js_code = JS_TEMPLATE % json.dumps(details)
            try:
                await page.evaluate(js_code)
                await page.wait_for_timeout(600)
                results["autofill_ok"] = True
                print("  ✅ Autofill injected on real form")
            except Exception as ae:
                results["errors"].append(f"autofill failed: {ae}")
            filled_shot = await page.screenshot(full_page=True)
            tg_photo(filled_shot, f"<b>UAT V4</b> — Real form filled (DOB fmt: {fmt})\n<b>NOT submitting</b>")

    return results


# ---------------------------------------------------------------------------
# PHASE 2 — local dummy form full pipeline
# ---------------------------------------------------------------------------
async def phase2_dummy_form(page, details: dict) -> dict:
    results = {
        "form_loaded": False,
        "dob_format_detected": None,
        "autofill_ok": False,
        "fields": {},
        "errors": [],
    }

    print(f"\n{'='*65}")
    print("  PHASE 2 — Local dummy form full pipeline test")
    print(f"{'='*65}")

    if not DUMMY_FORM_PATH.exists():
        msg = f"dummy_autofill_test.html not found at {DUMMY_FORM_PATH}"
        print(f"  ❌ {msg}")
        results["errors"].append(msg)
        return results

    file_url = DUMMY_FORM_PATH.as_uri()
    print(f"  Opening: {file_url}")
    await page.goto(file_url, wait_until="domcontentloaded", timeout=15_000)
    await page.wait_for_timeout(1200)

    # Check form loaded
    try:
        await page.wait_for_selector("input, textarea", timeout=5000)
        results["form_loaded"] = True
        print("  ✅ Dummy form loaded")
    except Exception as e:
        results["errors"].append(f"Form not loaded: {e}")
        return results

    empty_shot = await page.screenshot(full_page=True)
    tg_photo(empty_shot, "<b>UAT V4</b> — Dummy form BEFORE autofill")

    # ── detect_dob_format on dummy form ──────────────────────────────────────
    print("  -> Running detect_dob_format()...")
    fmt = await detect_dob_format(page)
    results["dob_format_detected"] = fmt
    print(f"  DOB format detected: {fmt}")

    # Compile js_code with detected format baked in
    details["_dob_format"] = fmt
    js_code = JS_TEMPLATE % json.dumps(details)

    # ── Inject autofill ───────────────────────────────────────────────────────
    print("  -> Injecting JS_TEMPLATE autofill...")
    try:
        await page.evaluate(js_code)
        await page.wait_for_timeout(700)
        results["autofill_ok"] = True
        print("  ✅ Autofill injected")
    except Exception as ae:
        results["errors"].append(f"Autofill failed: {ae}")
        print(f"  ❌ Autofill error: {ae}")

    # ── Read back all filled values ───────────────────────────────────────────
    filled = await page.evaluate("""
        () => {
            var r = {};
            document.querySelectorAll('input:not([type=hidden]),textarea,select').forEach(function(el) {
                var k = el.name || el.id || el.placeholder || '?';
                r[k] = el.tagName === 'TEXTAREA' ? el.value : (el.type === 'checkbox' ? el.checked : el.value);
            });
            return r;
        }
    """)
    results["fields"] = {k: v for k, v in filled.items() if v is not None and str(v).strip()}

    print("\n  Filled field values:")
    checks = {
        "phone":        ("Phone",          lambda v: len(str(v).replace(' ','').replace('-','')) >= 8),
        "email":        ("Email",          lambda v: "@" in str(v)),
        "sagsnummer":   ("Sagsnummer",     lambda v: bool(str(v).strip())),
        "p1_first":     ("p1_first_name",  lambda v: bool(str(v).strip())),
        "p1_last":      ("p1_last_name",   lambda v: bool(str(v).strip())),
        "p1_dob":       ("p1_dob",         lambda v: bool(str(v).strip())),
        "p1_address":   ("p1_address",     lambda v: bool(str(v).strip())),
        "p1_email":     ("p1_email",       lambda v: "@" in str(v)),
        "p2_first":     ("p2_first_name",  lambda v: bool(str(v).strip())),
        "p2_last":      ("p2_last_name",   lambda v: bool(str(v).strip())),
        "p2_dob":       ("p2_dob",         lambda v: bool(str(v).strip())),
        "p2_address":   ("p2_address",     lambda v: bool(str(v).strip())),
        "p2_email":     ("p2_email",       lambda v: "@" in str(v)),
    }
    field_summary = []
    all_ok = True
    for check_key, (field_name, validator) in checks.items():
        val = results["fields"].get(field_name, "")
        ok = validator(val) if val else False
        icon = "✅" if ok else "❌"
        if not ok:
            all_ok = False
            results["errors"].append(f"{field_name} not filled correctly (value='{val}')")
        print(f"    {icon} {field_name}: '{val}'")
        field_summary.append(f"{icon} {field_name}: <code>{str(val)[:40]}</code>")

    print(f"\n  DOB format detected: {fmt}")
    print(f"  PART 1 DOB filled as: '{results['fields'].get('p1_dob', '')}'")
    print(f"  PART 2 DOB filled as: '{results['fields'].get('p2_dob', '')}'")

    filled_shot = await page.screenshot(full_page=True)
    tg_photo(
        filled_shot,
        f"<b>UAT V4 — Dummy Form AFTER autofill</b>\n"
        f"DOB format detected: <code>{fmt}</code>\n"
        + "\n".join(field_summary[:8])
        + f"\n<b>All fields OK: {'✅' if all_ok else '❌'}</b>\n<b>NOT submitting</b>"
    )

    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def main():
    print("=" * 70)
    print("  UAT V4 — Without Witness + Dummy Form Full Pipeline")
    print(f"  Run at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)

    tg_send(
        f"<b>UAT V4 Started</b> — Without Witness + Dummy Form\n"
        f"<code>{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</code>\n"
        f"Tests: calendar nav | locked slot detection | detect_dob_format | "
        f"PART 1+2 autofill | phone | sagsnummer"
    )

    details = get_booking_details_from_env()
    if not details.get("partner1_email"):
        print("⚠️ No booking_details.json — using UAT dummy data")
        details = {
            "partner1_first_name": "Ikram", "partner1_last_name": "Issarti",
            "partner1_email": "isarti.ikram@gmail.com",
            "partner1_phone": "46552047", "partner1_dob": "15-09-1993",
            "partner1_nationality": "Belgian", "partner1_birth_city": "Larache",
            "partner1_birth_country": "Morocco",
            "partner1_residence_place": "2640 Mortsel",
            "partner1_address": "Oudebaan 38, 2640 Mortsel, Belgium",
            "partner1_gender": "Female", "partner1_marital_status": "Single",
            "partner1_cpr": "", "partner1_passport_ID_number": "595198060173",
            "partner1_passport_issue_place": "Belgium",
            "partner1_passport_issue_date": "01-02-2024",
            "partner1_passport_expiry_date": "01-02-2034",
            "partner2_first_name": "Mohsin", "partner2_last_name": "Mohammed",
            "partner2_email": "El.mohdmohsin@gmail.com",
            "partner2_phone": "46552047", "partner2_dob": "24-06-1994",
            "partner2_nationality": "Indian", "partner2_birth_city": "Hyderabad",
            "partner2_birth_country": "India",
            "partner2_residence_place": "Ajman, United Arab Emirates",
            "partner2_address": "Flat no 107, SAEED Building College street Al Nuaimia 2, Ajman, United Arab Emirates",
            "partner2_gender": "Male", "partner2_marital_status": "Single",
            "partner2_cpr": "", "partner2_passport_number": "V2319056",
            "partner2_passport_issue_place": "DUBAI",
            "partner2_passport_issue_date": "31-01-2022",
            "partner2_passport_expiry_date": "30-01-2032",
            "sagsnummer": "WZ2026-340594",
            "witness1_name": "", "witness1_address": "",
            "witness2_name": "", "witness2_address": "",
            "ceremony_language": "English", "ceremony_needed": "No",
            "invitees_count": "0", "password": "", "enable_autobook": False,
        }
    else:
        print(f"  Using details for: {details.get('partner1_first_name')} {details.get('partner1_last_name')}")

    # Force visible browser locally (override with HEADLESS=true for Railway)
    is_cloud = bool(os.getenv("PORT") or os.getenv("RAILWAY_STATIC_URL"))
    headless = is_cloud or os.getenv("HEADLESS", "false").lower() == "true"
    print(f"  headless={headless}  (set HEADLESS=true to run hidden)")

    async with async_playwright() as p:
        launch_args = ["--no-sandbox", "--disable-blink-features=AutomationControlled",
                       "--disable-infobars", "--disable-dev-shm-usage", "--disable-gpu"]
        try:
            browser = await p.chromium.launch(headless=headless, channel="chrome", args=launch_args)
        except Exception:
            browser = await p.chromium.launch(headless=headless, args=launch_args)

        print(f"  Browser launched (headless={headless})")

        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 1024},
            locale="en-DK",
            timezone_id="Europe/Copenhagen",
        )
        await context.add_init_script(_STEALTH)
        page = await context.new_page()
        await page.set_extra_http_headers({
            "Accept-Language": "en-DK,en;q=0.9,da;q=0.8",
            "Referer": "https://www.toender.dk/",
        })
        page.on("dialog", lambda d: asyncio.create_task(d.accept()))

        # PHASE 1
        p1 = await phase1_toender_calendar(page, details)

        # PHASE 2 — always run dummy form test regardless of phase 1 result
        p2 = await phase2_dummy_form(page, details)

        # Pause so user can see the filled form before the browser closes
        if not headless:
            print("\n  Browser will stay open for 20 seconds so you can inspect the filled form...")
            await page.wait_for_timeout(20_000)

        await browser.close()

    # Final summary
    lines = [
        "<b>UAT V4 Final Summary</b>",
        "",
        "<b>Phase 1 — Tønder Without Witness calendar</b>",
        f"  Calendar: {'✅' if p1['calendar_loaded'] else '❌'}",
        f"  Slots: <b>{p1['clickable_count']}</b> clickable / <b>{p1['locked_count']}</b> locked",
        f"  September dates visible: {', '.join(p1['sep_dates'][:5]) or 'none yet'}",
        f"  DOB fmt detected: {p1['dob_format_detected'] or 'N/A (no real form reached)'}",
        f"  Errors: {'; '.join(p1['errors']) or 'none'}",
        "",
        "<b>Phase 2 — Dummy form full pipeline</b>",
        f"  Form loaded: {'✅' if p2['form_loaded'] else '❌'}",
        f"  detect_dob_format(): <code>{p2['dob_format_detected']}</code>",
        f"  Autofill injected: {'✅' if p2['autofill_ok'] else '❌'}",
        f"  PART 1 DOB: <code>{p2['fields'].get('p1_dob', 'EMPTY')}</code>",
        f"  PART 2 DOB: <code>{p2['fields'].get('p2_dob', 'EMPTY')}</code>",
        f"  P1 name: <code>{p2['fields'].get('p1_first_name','')} {p2['fields'].get('p1_last_name','')}</code>",
        f"  P2 name: <code>{p2['fields'].get('p2_first_name','')} {p2['fields'].get('p2_last_name','')}</code>",
        f"  Phone: <code>{p2['fields'].get('Phone', 'EMPTY')}</code>",
        f"  Sagsnummer: <code>{p2['fields'].get('Sagsnummer', 'EMPTY')}</code>",
        f"  Errors: {'; '.join(p2['errors']) or 'none'}",
    ]
    summary = "\n".join(lines)
    tg_send(summary)
    print("\n" + summary.replace("<b>","").replace("</b>","").replace("<code>","'").replace("</code>","'"))
    print("\n" + "="*70)
    print("  UAT V4 COMPLETED")
    print("="*70)


if __name__ == "__main__":
    asyncio.run(main())


import asyncio
import os
import sys
import json
import re
import requests
from datetime import datetime
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

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

# Import from v4 bot
try:
    from toender_watch_v4 import JS_TEMPLATE, get_booking_details_from_env, load_env_file
    print("✅ Imported JS_TEMPLATE and helpers from toender_watch_v4")
except ImportError as ie:
    print(f"⚠️ Could not import from toender_watch_v4: {ie}")
    try:
        from toender_watch_v3 import JS_TEMPLATE, get_booking_details_from_env, load_env_file
        print("✅ Imported from toender_watch_v3 (fallback)")
    except ImportError:
        print("⚠️ No bot module available — using stub implementations")
        JS_TEMPLATE = "(function(){})()"
        def get_booking_details_from_env():
            return {}
        def load_env_file():
            pass

load_env_file()

# ---------------------------------------------------------------------------
# Telegram helpers
# ---------------------------------------------------------------------------
def _tg_creds():
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    return token, chat_id

def tg_send(msg: str) -> bool:
    token, chat_id = _tg_creds()
    if not token or not chat_id:
        print(f"[TG] {msg[:120]}")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=15,
        )
        return r.json().get("ok", False)
    except Exception as e:
        print(f"❌ TG send failed: {e}")
        return False

def tg_photo(photo_bytes: bytes, caption: str) -> bool:
    token, chat_id = _tg_creds()
    if not token or not chat_id:
        print(f"[TG PHOTO] {caption[:80]}")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendPhoto",
            data={"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"},
            files={"photo": ("shot.png", photo_bytes, "image/png")},
            timeout=25,
        )
        return r.json().get("ok", False)
    except Exception as e:
        print(f"❌ TG photo failed: {e}")
        return False

# ---------------------------------------------------------------------------
# Stealth script
# ---------------------------------------------------------------------------
_STEALTH = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
Object.defineProperty(navigator, 'languages', { get: () => ['en-DK', 'en', 'da'] });
window.chrome = { runtime: {} };
"""

# ---------------------------------------------------------------------------
# Category URLs
# ---------------------------------------------------------------------------
CATEGORIES = {
    "without_witness": {
        "name": "Without Witness",
        "url": (
            "https://reservation.frontdesksuite.com/toender/vielse/ReserveTime/StartReservation"
            "?pageId=8d47364a-5e21-4e40-892d-e9f46878e18b"
            "&buttonId=9d98558f-9d2e-4a50-8124-adf00b4abfb0"
            "&culture=en"
        ),
    },
    "with_witness": {
        "name": "With Witness",
        "url": (
            "https://reservation.frontdesksuite.com/toender/vielse/ReserveTime/StartReservation"
            "?pageId=8d47364a-5e21-4e40-892d-e9f46878e18b"
            "&buttonId=073d59ae-ab0d-484a-90b1-e1f9b68a8843"
            "&culture=en"
        ),
    },
}


# ---------------------------------------------------------------------------
# Main UAT flow
# ---------------------------------------------------------------------------
async def test_category(page, key: str, state: dict, details: dict) -> dict:
    """Run all checks for a single category. Returns a results dict."""
    name = state["name"]
    url = state["url"]
    results = {
        "name": name,
        "calendar_loaded": False,
        "clickable_count": 0,
        "locked_count": 0,
        "date_entries": [],
        "first_clickable_slot": None,
        "selecttime_called": False,
        "submit_btn_found": False,
        "autofill_ok": False,
        "phone_filled": False,
        "fullname_filled": False,
        "errors": [],
    }

    print(f"\n{'=' * 60}")
    print(f"  Testing category: {name}")
    print(f"{'=' * 60}")

    try:
        print(f"  -> goto {url[:80]}...")
        await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        await page.wait_for_timeout(2000)

        # Dismiss cookie banner
        try:
            cbtn = await page.query_selector("button.btn-warning, button:has-text('Accept necessary cookies')")
            if cbtn:
                await cbtn.click()
                await page.wait_for_timeout(1500)
        except Exception:
            pass

        # Wait for calendar
        try:
            await page.wait_for_selector("div.date.one-queue", timeout=20_000)
            results["calendar_loaded"] = True
        except PlaywrightTimeoutError:
            print(f"  ⚠️ Calendar div not found (may be no slots available)")
            results["errors"].append("Calendar div.date.one-queue not found")

        # Screenshot calendar page
        cal_shot = await page.screenshot(full_page=True)
        tg_photo(cal_shot, f"<b>UAT V4 [{name}] — Calendar loaded</b>\nURL: {page.url}")

        # Count and list all date entries
        day_divs = await page.query_selector_all("div.date.one-queue")
        print(f"  Found {len(day_divs)} date blocks")

        clickable_total = 0
        locked_total = 0
        first_clickable = None

        for day_div in day_divs:
            header_el = await day_div.query_selector("span.header-text")
            header_text = (await header_el.inner_text()).strip() if header_el else "(unknown date)"
            results["date_entries"].append(header_text)

            time_spans = await day_div.query_selector_all("span.available-time")
            for ts in time_spans:
                parent = await ts.evaluate_handle("el => el.parentElement")
                parent_name = await parent.evaluate("el => el.tagName.toLowerCase()")
                if parent_name == "a":
                    onclick = await parent.evaluate("el => el.getAttribute('onclick') || ''")
                    if "selectTime" in onclick:
                        clickable_total += 1
                        if first_clickable is None:
                            ts_text = (await ts.inner_text()).strip()
                            first_clickable = {
                                "day_text": header_text,
                                "time_text": ts_text,
                                "onclick": onclick,
                                "parent": parent,
                                "span": ts,
                            }
                    else:
                        locked_total += 1
                else:
                    locked_total += 1

        results["clickable_count"] = clickable_total
        results["locked_count"] = locked_total
        results["first_clickable_slot"] = first_clickable

        print(f"  Slots: {clickable_total} clickable / {locked_total} locked")
        print(f"  Dates found: {results['date_entries'][:5]} ...")

        tg_send(
            f"<b>UAT V4 [{name}]</b>\n"
            f"  Clickable: <b>{clickable_total}</b> | Locked: <b>{locked_total}</b>\n"
            f"  Dates: {', '.join(results['date_entries'][:5])}"
            + (" ..." if len(results["date_entries"]) > 5 else "")
        )

        # Expand first accordion and highlight first time span
        if day_divs:
            first_day = day_divs[0]
            acc_header = await first_day.query_selector("a.title")
            if acc_header:
                try:
                    await acc_header.click(timeout=3000)
                    await page.wait_for_timeout(1200)
                except Exception:
                    await page.evaluate("el => el.click()", acc_header)
                    await page.wait_for_timeout(1200)

            first_span = await first_day.query_selector("span.available-time")
            if first_span:
                await page.evaluate("""
                    (el) => {
                        el.style.outline = '4px solid red';
                        el.style.backgroundColor = 'yellow';
                        el.scrollIntoView({ behavior: 'smooth', block: 'center' });
                    }
                """, first_span)
                await page.wait_for_timeout(800)
                hi_shot = await page.screenshot(full_page=False)
                tg_photo(hi_shot, f"<b>UAT V4 [{name}]</b> — First slot highlighted (red outline)")

        # ── Test selectTime() path if clickable slot exists ──────────────────
        if first_clickable:
            print(f"  Clickable slot: {first_clickable['day_text']} {first_clickable['time_text']}")
            print(f"  onclick: {first_clickable['onclick'][:120]}")

            onclick_val = first_clickable["onclick"]
            clean_call = onclick_val.replace("return false;", "").strip().rstrip(";")

            print(f"  -> Calling selectTime() via page.evaluate()...")
            try:
                async with page.expect_navigation(timeout=15000):
                    await page.evaluate(clean_call)
                results["selecttime_called"] = True
                print(f"  selectTime() OK — now at: {page.url}")
            except Exception as nav_err:
                if "contactinfo" in page.url.lower():
                    results["selecttime_called"] = True
                    print(f"  selectTime() navigation exception but on ContactInfo: {nav_err}")
                else:
                    results["errors"].append(f"selectTime() failed: {nav_err}")
                    print(f"  ❌ selectTime() failed: {nav_err}")

            # Wait for submit button (v4 fix)
            print(f"  -> wait_for_selector('button#submit-btn', state='visible')...")
            try:
                await page.wait_for_selector("button#submit-btn", state="visible", timeout=15000)
                results["submit_btn_found"] = True
                print("  ✅ button#submit-btn found — ContactInfo form is fully loaded")
            except Exception as sel_err:
                results["errors"].append(f"button#submit-btn not found: {sel_err}")
                print(f"  ❌ button#submit-btn not found: {sel_err}")

            # Screenshot empty form
            empty_shot = await page.screenshot(full_page=True)
            tg_photo(empty_shot, f"<b>UAT V4 [{name}]</b> — ContactInfo form (BEFORE autofill)")

            # Inject autofill
            js_code = JS_TEMPLATE % json.dumps(details)
            try:
                await page.evaluate(js_code)
                await page.wait_for_timeout(800)
                results["autofill_ok"] = True
                print("  ✅ JS_TEMPLATE injected successfully")
            except Exception as ae:
                results["errors"].append(f"JS_TEMPLATE injection failed: {ae}")
                print(f"  ❌ JS_TEMPLATE injection failed: {ae}")

            # Verify phone field
            phone_val = await page.evaluate("""
                () => {
                    var inp = document.querySelector("input[name='PhoneNumber'], input[type='tel']");
                    return inp ? inp.value : null;
                }
            """)
            print(f"  Phone field value: '{phone_val}'")
            if phone_val and len(phone_val.replace(" ", "").replace("-", "")) >= 8:
                results["phone_filled"] = True
                print("  ✅ Phone field filled (8+ digits)")
            else:
                results["errors"].append(f"Phone field value suspicious: '{phone_val}'")
                print(f"  ⚠️ Phone field value may be incorrect: '{phone_val}'")

            # Verify full name / Fulde navn field
            fullname_val = await page.evaluate("""
                () => {
                    var inp = document.querySelector("input[name='field11316']");
                    if (!inp) {
                        // also check by label
                        var all = document.querySelectorAll("input");
                        for (var i = 0; i < all.length; i++) {
                            var id = all[i].id;
                            if (id) {
                                var lbl = document.querySelector("label[for='" + id + "']");
                                if (lbl && lbl.innerText.toLowerCase().includes("fulde")) {
                                    return all[i].value;
                                }
                            }
                        }
                        return null;
                    }
                    return inp.value;
                }
            """)
            print(f"  Fulde navn field value: '{fullname_val}'")
            if fullname_val and fullname_val.strip():
                results["fullname_filled"] = True
                print("  ✅ Fulde navn field filled")
            else:
                print("  ℹ️ Fulde navn field not found or empty (may not be on this form step)")

            # Check hidden phone country fields
            hidden_cc = await page.evaluate("""
                () => {
                    var el = document.getElementById('PhoneNumberCountryCallingCode');
                    return el ? el.value : null;
                }
            """)
            hidden_iso = await page.evaluate("""
                () => {
                    var el = document.getElementById('PhoneNumberIso2CountryCode');
                    return el ? el.value : null;
                }
            """)
            print(f"  Hidden PhoneNumberCountryCallingCode: '{hidden_cc}'")
            print(f"  Hidden PhoneNumberIso2CountryCode: '{hidden_iso}'")

            # Screenshot filled form
            filled_shot = await page.screenshot(full_page=True)
            tg_photo(
                filled_shot,
                f"<b>UAT V4 [{name}]</b> — ContactInfo form AFTER autofill\n"
                f"Phone: <code>{phone_val}</code> | CC: <code>{hidden_cc}</code> | ISO: <code>{hidden_iso}</code>\n"
                f"<b>NOT submitting — test stops here!</b>"
            )
            print("  ✅ Form screenshot sent. STOPPING before submit.")

        else:
            print(f"  ℹ️ No clickable slot found for {name} — testing autofill on live form by navigating fresh")
            # Navigate to the form page via the URL (this may fail with FlowStateIsMissing
            # if there are no open slots — we capture this as an informational result)
            await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            await page.wait_for_timeout(2000)
            no_slot_shot = await page.screenshot(full_page=True)
            tg_photo(
                no_slot_shot,
                f"<b>UAT V4 [{name}]</b> — No clickable slots. Calendar state shown.\n"
                f"Autofill test skipped (need a clickable slot to reach ContactInfo)."
            )

    except Exception as ex:
        results["errors"].append(f"Category test exception: {ex}")
        print(f"  ❌ Exception in {name}: {ex}")
        try:
            err_shot = await page.screenshot(full_page=True)
            tg_photo(err_shot, f"<b>UAT V4 [{name}] ERROR</b>\n<code>{str(ex)[:300]}</code>")
        except Exception:
            pass

    return results


async def main():
    print("=" * 70)
    print("  UAT V4 — Bot Rebuild Verification Test")
    print(f"  Run at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)

    tg_send(
        f"<b>UAT V4 Started</b>\n"
        f"Time: <code>{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</code>\n"
        f"Tests: selectTime() direct call | button#submit-btn wait | "
        f"jQuery phone flag | fulde navn | 8-digit DK phone"
    )

    # Load booking details
    details = get_booking_details_from_env()
    if not details.get("partner1_email"):
        print("⚠️ No booking_details.json — using UAT dummy data")
        details = {
            "partner1_first_name": "Maria",
            "partner1_last_name": "Garcia",
            "partner1_email": "ikoissa@gmail.com",
            "partner1_phone": "46552047",
            "partner1_dob": "01-01-1990",
            "partner1_nationality": "Danish",
            "partner1_birth_city": "Copenhagen",
            "partner1_birth_country": "Denmark",
            "partner1_residence_place": "Copenhagen",
            "partner1_address": "Test Street 1, Copenhagen",
            "partner1_gender": "Female",
            "partner1_marital_status": "Single",
            "partner1_cpr": "",
            "partner1_passport_ID_number": "TEST123",
            "partner1_passport_issue_place": "Denmark",
            "partner1_passport_issue_date": "01-01-2020",
            "partner1_passport_expiry_date": "01-01-2030",
            "partner2_first_name": "Test",
            "partner2_last_name": "Partner",
            "partner2_email": "partner@example.com",
            "partner2_phone": "+4512345678",
            "partner2_dob": "01-01-1992",
            "partner2_nationality": "Danish",
            "partner2_birth_city": "Aarhus",
            "partner2_birth_country": "Denmark",
            "partner2_residence_place": "Aarhus",
            "partner2_address": "Partner Street 2, Aarhus",
            "partner2_gender": "Male",
            "partner2_marital_status": "Single",
            "partner2_cpr": "",
            "partner2_passport_number": "PARTNER456",
            "partner2_passport_issue_place": "Denmark",
            "partner2_passport_issue_date": "01-01-2020",
            "partner2_passport_expiry_date": "01-01-2030",
            "sagsnummer": "UAT-2026-001",
            "witness1_name": "Witness One",
            "witness1_address": "Witness Street 1",
            "witness2_name": "Witness Two",
            "witness2_address": "Witness Street 2",
            "ceremony_language": "English",
            "ceremony_needed": "No",
            "invitees_count": "0",
            "password": "TestPass123",
            "enable_autobook": False,
        }
    else:
        print(f"  Using booking details for: {details.get('partner1_first_name')} {details.get('partner1_last_name')}")

    headless_str = os.getenv("HEADLESS", "true").lower()
    is_cloud = bool(os.getenv("PORT") or os.getenv("RAILWAY_STATIC_URL"))
    headless = headless_str == "true" or is_cloud

    async with async_playwright() as p:
        launch_args = [
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",
            "--disable-dev-shm-usage",
            "--disable-gpu",
        ]

        try:
            browser = await p.chromium.launch(headless=headless, channel="chrome", args=launch_args)
        except Exception:
            browser = await p.chromium.launch(headless=headless, args=launch_args)

        print(f"  Browser launched (headless={headless})")

        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 1024},
            locale="en-DK",
            timezone_id="Europe/Copenhagen",
        )
        await context.add_init_script(_STEALTH)
        page = await context.new_page()
        await page.set_extra_http_headers({
            "Accept-Language": "en-DK,en;q=0.9,da;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": "https://www.toender.dk/",
        })
        page.on("dialog", lambda d: asyncio.create_task(d.accept()))

        all_results = {}
        for key, cat in CATEGORIES.items():
            result = await test_category(page, key, cat, details)
            all_results[key] = result

        await browser.close()

    # Build final summary
    lines = ["<b>UAT V4 Final Summary</b>", ""]
    all_pass = True
    for key, r in all_results.items():
        status = "✅" if not r["errors"] else "⚠️"
        if r["errors"]:
            all_pass = False
        lines.append(f"<b>{r['name']}</b> {status}")
        lines.append(f"  Calendar loaded: {'✅' if r['calendar_loaded'] else '❌'}")
        lines.append(f"  Slots: <b>{r['clickable_count']}</b> clickable / <b>{r['locked_count']}</b> locked")
        lines.append(f"  selectTime() called: {'✅' if r['selecttime_called'] else '—'}")
        lines.append(f"  button#submit-btn found: {'✅' if r['submit_btn_found'] else '—'}")
        lines.append(f"  Autofill injected: {'✅' if r['autofill_ok'] else '—'}")
        lines.append(f"  Phone filled (8-digit): {'✅' if r['phone_filled'] else '—'}")
        lines.append(f"  Fulde navn filled: {'✅' if r['fullname_filled'] else '—'}")
        if r["errors"]:
            lines.append(f"  Errors: {'; '.join(r['errors'])[:200]}")
        lines.append("")

    lines.append("Overall: " + ("✅ ALL PASS" if all_pass else "⚠️ SOME FAILURES — see above"))
    summary = "\n".join(lines)
    tg_send(summary)
    print("\n" + summary.replace("<b>", "").replace("</b>", "").replace("<code>", "").replace("</code>", ""))

    print("\n" + "=" * 70)
    print("  UAT V4 COMPLETED — check Telegram for screenshots and summary")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
