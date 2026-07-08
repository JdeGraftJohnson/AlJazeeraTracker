"""
backfill_from_discord.py — one-shot backfill of headline history from the
Discord channel where aj_live.py has been posting embeds.

Env:
  DISCORD_BOT_TOKEN    bot token with View Channel + Read Message History
                       (must be added to the same server + channel as the webhook)
  DISCORD_WEBHOOK_URL  existing webhook URL — channel_id auto-derived from it
  DISCORD_CHANNEL_ID   (optional) override; skips webhook lookup

Idempotent: uses the same sha1(published_utc|heading) id as aj_live.py's
append_history(), so re-runs won't duplicate. Writes to data/headlines_YYYY-MM.jsonl.
"""

import os
import re
import sys
import time
from datetime import datetime, timezone

import requests

from aj_live import HISTORY_DIR, _headline_id, _iso_to_est

API = "https://discord.com/api/v10"
FIELD_NAME_RE = re.compile(r"^🕐\s*(?P<ts>.+?)\s*—\s*(?P<heading>.+)$")
ISO_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")


def channel_id_from_webhook(webhook_url: str) -> str:
    """GET the webhook object (webhook_url IS its own auth) → returns channel_id."""
    r = requests.get(webhook_url, timeout=10)
    r.raise_for_status()
    return r.json()["channel_id"]


def fetch_messages(token: str, channel_id: str):
    headers = {"Authorization": f"Bot {token}"}
    before = None
    while True:
        params = {"limit": 100}
        if before:
            params["before"] = before
        r = requests.get(f"{API}/channels/{channel_id}/messages",
                         headers=headers, params=params, timeout=15)
        if r.status_code == 429:
            wait = r.json().get("retry_after", 1.0)
            print(f"rate-limited, sleeping {wait}s"); time.sleep(wait); continue
        r.raise_for_status()
        batch = r.json()
        if not batch:
            return
        for m in batch:
            yield m
        before = batch[-1]["id"]


def parse_embed(msg: dict):
    msg_ts = msg.get("timestamp", "")
    for embed in msg.get("embeds", []):
        liveblog_url = embed.get("url", "")
        for field in embed.get("fields", []):
            m = FIELD_NAME_RE.match(field.get("name", "").strip())
            if not m:
                continue
            ts_raw = m.group("ts").strip()
            heading = m.group("heading").strip()
            body = field.get("value", "").strip()
            if body == "_No summary available_":
                body = ""

            iso_match = ISO_RE.search(ts_raw)
            if iso_match:
                published_utc = ts_raw
            else:
                published_utc = ""

            yield {
                "heading": heading,
                "iso_dt": published_utc,
                "timestamp": ts_raw,
                "body": body,
                "liveblog_url": liveblog_url,
                "msg_ts": msg_ts,
            }


def main():
    token = os.environ.get("DISCORD_BOT_TOKEN")
    channel = os.environ.get("DISCORD_CHANNEL_ID")
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")

    if not token:
        print("Set DISCORD_BOT_TOKEN (bot with View Channel + Read Message History)."); sys.exit(1)

    if not channel:
        if not webhook_url:
            print("Set DISCORD_CHANNEL_ID or DISCORD_WEBHOOK_URL."); sys.exit(1)
        channel = channel_id_from_webhook(webhook_url)
        print(f"Resolved channel_id={channel} from webhook.")

    HISTORY_DIR.mkdir(exist_ok=True)

    # Load every existing id across all monthly partitions for global dedup
    seen_ids: set[str] = set()
    for p in HISTORY_DIR.glob("headlines_*.jsonl"):
        for line in p.read_text().splitlines():
            try:
                import json
                seen_ids.add(json.loads(line)["id"])
            except Exception:
                pass
    print(f"Existing archive contains {len(seen_ids)} unique headlines.")

    # Group new records by their target partition (msg_ts month, since seen_at)
    from collections import defaultdict
    import json
    buckets: dict[str, list[dict]] = defaultdict(list)
    scanned = 0
    for msg in fetch_messages(token, channel):
        scanned += 1
        for u in parse_embed(msg):
            hid = _headline_id(u["iso_dt"], u["heading"])
            if hid in seen_ids:
                continue
            seen_ids.add(hid)
            msg_ts = u["msg_ts"] or datetime.now(timezone.utc).isoformat()
            partition_key = msg_ts[:7]  # YYYY-MM of when Discord received it
            record = {
                "id": hid,
                "seen_at_utc": msg_ts,
                "published_utc": u["iso_dt"],
                "published_est": _iso_to_est(u["iso_dt"]),
                "timestamp_display": u["timestamp"],
                "heading": u["heading"],
                "body": u["body"],
                "liveblog_url": u["liveblog_url"],
                "source": "discord_backfill",
            }
            buckets[partition_key].append(record)
        if scanned % 500 == 0:
            print(f"  scanned {scanned} messages, staged {sum(len(v) for v in buckets.values())} new records")

    written = 0
    for month, records in sorted(buckets.items()):
        # Preserve chronological order (Discord returns newest-first)
        records.sort(key=lambda r: r["seen_at_utc"])
        path = HISTORY_DIR / f"headlines_{month}.jsonl"
        with path.open("a", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
                written += 1
        print(f"  wrote {len(records)} to {path}")

    print(f"Backfill complete: scanned {scanned} messages, wrote {written} new records.")


if __name__ == "__main__":
    main()
