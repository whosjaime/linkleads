"""
Scheduled Discord channel poller for GitHub Actions.

This is for the non-live setup:
- GitHub Actions runs every 30 minutes.
- It checks configured Discord channels for recent messages.
- It extracts links from those messages.
- It sends new links to Monday.com using discord_to_monday_leads.py.
- It stores only message IDs and URL hashes in seen_leads_state.json.

Required secrets/env:
- DISCORD_BOT_TOKEN
- MONDAY_API_TOKEN
- DISCORD_ALLOWED_CHANNEL_IDS=123,456

Optional:
- DISCORD_LOOKBACK_LIMIT=50
- SEEN_STATE_FILE=seen_leads_state.json
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp
from dotenv import load_dotenv

from discord_to_monday_leads import URL_REGEX, init_db, normalize_url, process_lead_url

load_dotenv()

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()
DISCORD_ALLOWED_CHANNEL_IDS = [
    channel_id.strip()
    for channel_id in os.getenv("DISCORD_ALLOWED_CHANNEL_IDS", "").split(",")
    if channel_id.strip()
]
DISCORD_LOOKBACK_LIMIT = int(os.getenv("DISCORD_LOOKBACK_LIMIT", "50"))
SEEN_STATE_FILE = Path(os.getenv("SEEN_STATE_FILE", "seen_leads_state.json"))
DISCORD_API_BASE = "https://discord.com/api/v10"
MAX_STATE_ITEMS = int(os.getenv("MAX_STATE_ITEMS", "5000"))


def require_env() -> None:
    missing = []
    if not DISCORD_BOT_TOKEN:
        missing.append("DISCORD_BOT_TOKEN")
    if not DISCORD_ALLOWED_CHANNEL_IDS:
        missing.append("DISCORD_ALLOWED_CHANNEL_IDS")
    if missing:
        raise RuntimeError(f"Missing required env values: {', '.join(missing)}")


def load_state() -> dict[str, Any]:
    if not SEEN_STATE_FILE.exists():
        return {"message_ids": [], "url_hashes": [], "updated_at": None}
    try:
        data = json.loads(SEEN_STATE_FILE.read_text(encoding="utf-8"))
        return {
            "message_ids": list(data.get("message_ids", [])),
            "url_hashes": list(data.get("url_hashes", [])),
            "updated_at": data.get("updated_at"),
        }
    except json.JSONDecodeError:
        return {"message_ids": [], "url_hashes": [], "updated_at": None}


def save_state(state: dict[str, Any]) -> None:
    state["message_ids"] = state.get("message_ids", [])[-MAX_STATE_ITEMS:]
    state["url_hashes"] = state.get("url_hashes", [])[-MAX_STATE_ITEMS:]
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    SEEN_STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def hash_url(url: str) -> str:
    normalized = normalize_url(url)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


async def fetch_recent_messages(session: aiohttp.ClientSession, channel_id: str) -> list[dict[str, Any]]:
    url = f"{DISCORD_API_BASE}/channels/{channel_id}/messages"
    params = {"limit": min(max(DISCORD_LOOKBACK_LIMIT, 1), 100)}
    async with session.get(url, params=params) as response:
        if response.status >= 400:
            body = await response.text()
            raise RuntimeError(f"Discord fetch failed for channel {channel_id}: HTTP {response.status} {body}")
        return await response.json(content_type=None)


async def main() -> None:
    require_env()
    init_db()

    state = load_state()
    seen_message_ids = set(state.get("message_ids", []))
    seen_url_hashes = set(state.get("url_hashes", []))

    created_count = 0
    skipped_count = 0
    error_count = 0

    headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}"}
    async with aiohttp.ClientSession(headers=headers) as session:
        for channel_id in DISCORD_ALLOWED_CHANNEL_IDS:
            messages = await fetch_recent_messages(session, channel_id)

            # Discord returns newest first. Process oldest first.
            for message in reversed(messages):
                message_id = str(message.get("id", ""))
                content = message.get("content") or ""
                urls = URL_REGEX.findall(content)

                if not urls:
                    if message_id and message_id not in seen_message_ids:
                        seen_message_ids.add(message_id)
                    continue

                if message_id in seen_message_ids:
                    skipped_count += len(urls)
                    continue

                for url in urls:
                    url_digest = hash_url(url)
                    if url_digest in seen_url_hashes:
                        skipped_count += 1
                        continue

                    try:
                        created, result = await process_lead_url(
                            url=url,
                            channel_id=int(channel_id),
                            source_channel=f"Discord channel {channel_id} message {message_id}",
                        )
                        print(result)
                        if created:
                            created_count += 1
                        else:
                            skipped_count += 1

                        seen_url_hashes.add(url_digest)
                    except Exception as exc:
                        error_count += 1
                        print(f"ERROR processing {url}: {exc}")

                if message_id:
                    seen_message_ids.add(message_id)

    state["message_ids"] = list(seen_message_ids)
    state["url_hashes"] = list(seen_url_hashes)
    save_state(state)

    print(f"Done. Created: {created_count}, skipped: {skipped_count}, errors: {error_count}")

    if error_count:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
