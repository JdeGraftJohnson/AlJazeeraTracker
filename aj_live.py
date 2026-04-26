"""
aj_live.py — Al Jazeera live blog monitor for GitHub Actions.
"""

import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path

import requests
from playwright.async_api import async_playwright
from playwright_stealth import Stealth

LIVEBLOG_INDEX = "https://www.aljazeera.com/news/liveblog/"
LAST_SEEN_FILE = Path("last_seen.txt")

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


# ── Persistence ────────────────────────────────────────────────────────────

def load_last_seen() -> set[str]:
    if not LAST_SEEN_FILE.exists():
        return set()
    return set(LAST_SEEN_FILE.read_text().strip().splitlines())


def save_last_seen(headlines: list[str]) -> None:
    LAST_SEEN_FILE.write_text("\n".join(headlines))


# ── Discord ────────────────────────────────────────────────────────────────

def send_discord(updates: list[dict], liveblog_url: str) -> None:
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        print("No DISCORD_WEBHOOK_URL set — skipping alert.")
        return

    fields = []
    for u in updates:
        value = u["body"] if u["body"] else "_No summary available_"
        fields.append({
            "name": f"🕐 {u['timestamp']}  —  {u['heading'] or 'Update'}",
            "value": value[:500],
            "inline": False,
        })

    embed = {
        "title": "📡 Al Jazeera — New Live Updates",
        "url": liveblog_url,
        "color": 0xFF6B35,
        "fields": fields,
        "footer": {"text": f"Al Jazeera Live Tracker • {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}"},
    }

    resp = requests.post(webhook_url, json={"embeds": [embed]}, timeout=10)
    resp.raise_for_status()
    print(f"Discord alert sent — {len(updates)} new update(s).")


# ── Playwright ─────────────────────────────────────────────────────────────

async def get_todays_liveblog_url(page) -> str:
    await page.goto(LIVEBLOG_INDEX, wait_until="domcontentloaded", timeout=30_000)
    await page.wait_for_timeout(2_000)
    link = await page.query_selector("a[href*='/news/liveblog/20']")
    if not link:
        raise RuntimeError("Could not find today's liveblog on the index page.")
    href = await link.get_attribute("href")
    if not href.startswith("http"):
        href = "https://www.aljazeera.com" + href
    return href


async def get_live_updates(url: str = None, n: int = 3) -> tuple[str, list[dict]]:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
            ],
        )
        context = await browser.new_context(
            user_agent=UA,
            viewport={"width": 1280, "height": 900},
            locale="en-US",
            timezone_id="America/New_York",
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            },
        )
        page = await context.new_page()

        # Apply stealth — makes headless Chromium look like a real browser
        await Stealth().apply_stealth_async(page)

        if not url:
            url = await get_todays_liveblog_url(page)
            print(f"Today's liveblog: {url}")

        # networkidle waits for JS to finish rendering (vs domcontentloaded which is too early)
        await page.goto(url, wait_until="networkidle", timeout=45_000)
        await page.wait_for_timeout(3_000)

        # Dismiss cookie consent banner if present
        for btn_text in ["Reject all", "Accept all", "Allow all"]:
            try:
                btn = await page.query_selector(f"button:has-text('{btn_text}')")
                if btn:
                    await btn.click()
                    await page.wait_for_timeout(1_000)
                    print(f"Dismissed cookie banner: {btn_text}")
                    break
            except Exception:
                pass

        # Save screenshot as debug artifact
        await page.screenshot(path="debug_screenshot.png", full_page=False)
        print("Screenshot saved: debug_screenshot.png")

        title = await page.title()
        print(f"Page title: {title}")

        # Exact selectors from DevTools inspection:
        # li[data-testid="live-blog-feed-item"] > div.card-live
        entries = await page.query_selector_all('li[data-testid="live-blog-feed-item"]')
        print(f"Live blog feed items found: {len(entries)}")



        # Parse ALL entries, attach their datetime for sorting, take 3 most recent
        all_results = []
        for entry in entries:
            heading_el = await entry.query_selector(".card-live__content-area h2, .card-live__content-area h3, h2, h3")
            heading = (await heading_el.inner_text()).strip() if heading_el else ""

            # Body — use image caption or figcaption as fallback for photo-only entries
            body_el = await entry.query_selector(".card-live__content p, p, figcaption")
            body = (await body_el.inner_text()).strip()[:300] if body_el else ""

            time_el = await entry.query_selector(".card-live__precontent time, time")
            timestamp = ""
            iso_dt = ""
            if time_el:
                iso_dt = await time_el.get_attribute("datetime") or ""
                timestamp = iso_dt or await time_el.inner_text()
                timestamp = timestamp.strip()

            if not heading and not body:
                continue

            all_results.append({"timestamp": timestamp, "iso_dt": iso_dt, "heading": heading, "body": body})

        # Sort by ISO datetime descending (newest first), fall back to DOM order
        def sort_key(r):
            return r["iso_dt"] if r["iso_dt"] else ""
        all_results.sort(key=sort_key, reverse=True)

        results = []
        for r in all_results[:n]:
            del r["iso_dt"]
            results.append(r)
            print(f"  [{r['timestamp']}] {r['heading'][:70]}")

        await browser.close()
        return url, results


# ── Main ───────────────────────────────────────────────────────────────────

async def main():
    url = sys.argv[1] if len(sys.argv) > 1 else None
    liveblog_url, updates = await get_live_updates(url, n=3)

    if not updates:
        print("No updates found.")
        return

    last_seen = load_last_seen()
    new_updates = [u for u in updates if u["heading"] not in last_seen]

    if not new_updates:
        print("No new updates since last run.")
    else:
        print(f"{len(new_updates)} new update(s) found.")
        send_discord(new_updates, liveblog_url)

    save_last_seen([u["heading"] for u in updates if u["heading"]])


if __name__ == "__main__":
    asyncio.run(main())
