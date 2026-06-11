"""
Scheduled Discord channel poller for GitHub Actions.

This is for the non-live setup:
- GitHub Actions runs every 30 minutes.
- It checks configured Discord channels for recent messages.
- It extracts links from those messages.
- It sends new links to Monday.com using discord_to_monday_leads.py.
- It stores only processed message IDs and URL hashes in seen_leads_state.json.

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
import re
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

MONDAY_API_TOKEN = os.getenv("MONDAY_API_TOKEN", "").strip()
MONDAY_BOARD_ID = os.getenv("MONDAY_BOARD_ID", "18405764077").strip()
MONDAY_API_URL = "https://api.monday.com/v2"
COL_REFERRAL_BONUS = os.getenv("COL_REFERRAL_BONUS", "text_mm33jner").strip()


def require_env() -> None:
    missing = []
    if not DISCORD_BOT_TOKEN:
        missing.append("DISCORD_BOT_TOKEN")
    if not DISCORD_ALLOWED_CHANNEL_IDS:
        missing.append("DISCORD_ALLOWED_CHANNEL_IDS")
    if not MONDAY_API_TOKEN:
        missing.append("MONDAY_API_TOKEN")
    if not MONDAY_BOARD_ID:
        missing.append("MONDAY_BOARD_ID")
    if not COL_REFERRAL_BONUS:
        missing.append("COL_REFERRAL_BONUS")
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


def extract_referral_fee(content: str) -> str:
    patterns = [
        r"(?:referral\s*(?:bonus|fee)|bonus|reward|bounty)\s*[:\-]?\s*(\$?\s*[\d,]+(?:\.\d{1,2})?\s*(?:usd|cad)?)",
        r"(\$?\s*[\d,]+(?:\.\d{1,2})?\s*(?:usd|cad)?)\s*(?:referral\s*(?:bonus|fee)|bonus|reward|bounty)",
    ]
    for pattern in patterns:
        match = re.search(pattern, content or "", re.IGNORECASE)
        if not match:
            continue
        value = re.sub(r"\s+", " ", match.group(1)).strip().replace("$ ", "$")
        return value if value.startswith("$") else f"${value}"
    return ""


def monday_referral_value(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if COL_REFERRAL_BONUS.startswith(("numeric", "numbers")):
        return re.sub(r"[^0-9.]", "", value)
    return value


def extract_monday_item_id(result_message: str) -> str:
    match = re.search(r"Item ID:\s*(\d+)", result_message or "", re.IGNORECASE)
    return match.group(1) if match else ""


async def update_monday_referral_fee(item_id: str, referral_fee: str) -> None:
    value = monday_referral_value(referral_fee)
    if not item_id or not value:
        return

    mutation = """
    mutation SetReferralFee($board_id: ID!, $item_id: ID!, $column_id: String!, $value: String!) {
        change_simple_column_value(board_id: $board_id, item_id: $item_id, column_id: $column_id, value: $value) {
            id
        }
    }
    """
    payload = {
        "query": mutation,
        "variables": {
            "board_id": MONDAY_BOARD_ID,
            "item_id": item_id,
            "column_id": COL_REFERRAL_BONUS,
            "value": value,
        },
    }
    headers = {"Authorization": MONDAY_API_TOKEN, "Content-Type": "application/json"}
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.post(MONDAY_API_URL, json=payload) as response:
            data = await response.json(content_type=None)
    if "errors" in data:
        raise RuntimeError(f"Monday referral fee update failed: {json.dumps(data['errors'], indent=2)}")


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
    total_messages = 0
    total_urls_found = 0
    empty_content_messages = 0

    headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}"}
    async with aiohttp.ClientSession(headers=headers) as session:
        for channel_id in DISCORD_ALLOWED_CHANNEL_IDS:
            messages = await fetch_recent_messages(session, channel_id)
            total_messages += len(messages)
            print(f"Fetched {len(messages)} messages from Discord channel {channel_id}")

            # Discord returns newest first. Process oldest first.
            for message in reversed(messages):
                message_id = str(message.get("id", ""))
                content = message.get("content") or ""

                if not content:
                    empty_content_messages += 1

                urls = URL_REGEX.findall(content)
                if urls:
                    print(f"Message {message_id} contains {len(urls)} URL(s)")
                total_urls_found += len(urls)

                # Important: do NOT mark messages without URLs as seen.
                # If Message Content Intent was off, Discord can return empty content.
                # Marking those empty messages as seen would permanently skip them later.
                if not urls:
                    continue

                if message_id in seen_message_ids:
                    skipped_count += len(urls)
                    continue

                referral_fee = extract_referral_fee(content)

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
                        if created and referral_fee:
                            item_id = extract_monday_item_id(result)
                            await update_monday_referral_fee(item_id, referral_fee)
                            result = f"{result} | Referral Fee added: {referral_fee}"
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

    print(
        f"Done. Messages fetched: {total_messages}, empty-content messages: {empty_content_messages}, "
        f"URLs found: {total_urls_found}, created: {created_count}, skipped: {skipped_count}, errors: {error_count}"
    )

    if total_messages and empty_content_messages == total_messages:
        print(
            "WARNING: All fetched Discord messages had empty content. "
            "Turn on Message Content Intent in Discord Developer Portal > Bot, "
            "and make sure the bot can Read Message History in the channel."
        )

    if error_count:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
