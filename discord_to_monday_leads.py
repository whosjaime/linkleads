"""
Discord LinkedIn/Public Link -> Monday.com Lead Capture Bot

Use in Discord:
    /lead https://www.linkedin.com/posts/...

The bot:
- Scrapes basic public metadata from the URL
- Detects likely role/skill from the post text
- Creates a lead in Monday.com
- Dedupes links locally using SQLite

Keep secrets in .env locally or in your hosting platform/GitHub Secrets.
Do not commit a real .env file.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import aiohttp
import discord
from bs4 import BeautifulSoup
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()
DISCORD_GUILD_ID = os.getenv("DISCORD_GUILD_ID", "").strip()
MONDAY_API_TOKEN = os.getenv("MONDAY_API_TOKEN", "").strip()
MONDAY_DEFAULT_GROUP_ID = os.getenv("MONDAY_DEFAULT_GROUP_ID", "group_mm1vwy0q").strip()

AUTO_CAPTURE_LINKS = os.getenv("DISCORD_AUTO_CAPTURE_LINKS", "false").strip().lower() == "true"
ALLOWED_CHANNEL_IDS = {
    int(channel_id.strip())
    for channel_id in os.getenv("DISCORD_ALLOWED_CHANNEL_IDS", "").split(",")
    if channel_id.strip().isdigit()
}

try:
    CHANNEL_GROUP_MAP: dict[str, str] = json.loads(os.getenv("CHANNEL_GROUP_MAP", "{}"))
except json.JSONDecodeError:
    CHANNEL_GROUP_MAP = {}

MONDAY_BOARD_ID = os.getenv("MONDAY_BOARD_ID", "18405764077").strip()
MONDAY_API_URL = "https://api.monday.com/v2"
DATABASE_PATH = "lead_dedup.sqlite3"

COL_STATUS = os.getenv("COL_STATUS", "color_mm1v7b3s").strip()
COL_MARKET = os.getenv("COL_MARKET", "color_mm3fkwv7").strip()
COL_POST_DATE = os.getenv("COL_POST_DATE", "date_mm1v35kx").strip()
COL_LINK_TO_JP = os.getenv("COL_LINK_TO_JP", "link_mm1v7vdj").strip()
COL_COMPANY_CHANNEL = os.getenv("COL_COMPANY_CHANNEL", "text_mm1vyhy").strip()
COL_LINKEDIN_PROFILE = os.getenv("COL_LINKEDIN_PROFILE", "link_mm1vbyjc").strip()
COL_EMAIL = os.getenv("COL_EMAIL", "email_mm1v1yzs").strip()
COL_PRIMARY_SKILL = os.getenv("COL_PRIMARY_SKILL", "dropdown_mm1vf5c9").strip()
COL_LOCATION_TYPE = os.getenv("COL_LOCATION_TYPE", "dropdown_mm1vrjm1").strip()
COL_PLATFORM = os.getenv("COL_PLATFORM", "color_mm1vhds4").strip()
COL_SOURCED_FROM = os.getenv("COL_SOURCED_FROM", "color_mm1vhjmn").strip()
COL_CATEGORY = os.getenv("COL_CATEGORY", "color_mm1vcyn7").strip()
COL_DESCRIPTION = os.getenv("COL_DESCRIPTION", "long_text_mm1v4f4k").strip()
COL_ROLE_POSITION = os.getenv("COL_ROLE_POSITION", "dropdown_mm1v8vzh").strip()

URL_REGEX = re.compile(r"https?://[^\s<>\"]+", re.IGNORECASE)
EMAIL_REGEX = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)

TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "rcm",
}

ROLE_KEYWORDS = {
    "Short-Form Editor": ["short-form editor", "short form editor", "shorts editor", "reels editor", "tiktok editor"],
    "Long-Form Editor": ["long-form editor", "long form editor", "youtube editor"],
    "Lead Editor": ["lead editor", "head editor"],
    "Video Editor": ["video editor", "editor", "editing", "freelance editor", "skilled video editor"],
    "Thumbnail Designer": ["thumbnail", "thumbnail designer"],
    "Scriptwriter": ["scriptwriter", "script writer", "writer", "script"],
    "Producer": ["producer", "production", "production coordinator"],
    "Channel Manager": ["channel manager", "youtube manager"],
    "Strategist": ["strategist", "strategy"],
    "Graphic Designer": ["graphic designer", "designer"],
    "Animator": ["animator", "animation"],
    "Developer": ["developer", "coder", "software engineer"],
    "Engineer": ["engineer"],
    "Operations": ["operations", "ops manager", "operations manager"],
    "Personal Assistant": ["personal assistant", "assistant", "executive assistant"],
    "Creative Director": ["creative director"],
}

LOCATION_KEYWORDS = {
    "Remote": ["remote", "work from home", "wfh"],
    "Onsite": ["onsite", "on-site", "in person", "in-person", "located in", "based in"],
    "Hybrid": ["hybrid"],
}

CATEGORY_KEYWORDS = {
    "Agency": ["agency", "freelancing project", "freelance project", "client project"],
    "Company": ["company", "brand", "business"],
    "Creator": ["creator", "influencer", "content creator"],
    "YouTuber": ["youtuber", "youtube channel"],
    "Personal Brand": ["personal brand"],
    "Startup": ["startup"],
}

PLATFORM_BY_DOMAIN = {
    "linkedin": "LinkedIn",
    "instagram": "Instagram",
    "tiktok": "TikTok",
    "youtube": "YouTube",
    "youtu.be": "YouTube",
    "twitter": "X",
    "x.com": "X",
}


@dataclass
class LeadData:
    original_url: str
    normalized_url: str
    item_name: str
    platform: str
    sourced_from: str
    role: str
    location_type: str
    category: str
    post_text: str
    author: str
    author_profile_url: str
    domain: str
    emails: list[str]
    group_id: str
    source_channel: str
    scraped_at: str
    post_date: str
    scrape_status: str


def require_env() -> None:
    missing = []
    if not DISCORD_BOT_TOKEN:
        missing.append("DISCORD_BOT_TOKEN")
    if not MONDAY_API_TOKEN:
        missing.append("MONDAY_API_TOKEN")
    if not MONDAY_DEFAULT_GROUP_ID:
        missing.append("MONDAY_DEFAULT_GROUP_ID")
    if missing:
        raise RuntimeError(f"Missing required env values: {', '.join(missing)}")


def init_db() -> None:
    with sqlite3.connect(DATABASE_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS posted_leads (
                normalized_url TEXT PRIMARY KEY,
                monday_item_id TEXT,
                item_name TEXT,
                created_at TEXT
            )
            """
        )
        conn.commit()


def normalize_url(url: str) -> str:
    parsed = urlparse(url.strip())
    scheme = parsed.scheme.lower() or "https"
    netloc = parsed.netloc.lower().replace("www.", "")
    query_items = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in TRACKING_PARAMS
    ]
    clean_query = urlencode(query_items, doseq=True)
    clean_path = parsed.path.rstrip("/")
    return urlunparse((scheme, netloc, clean_path, "", clean_query, ""))


def is_duplicate(normalized_url: str) -> bool:
    with sqlite3.connect(DATABASE_PATH) as conn:
        row = conn.execute(
            "SELECT normalized_url FROM posted_leads WHERE normalized_url = ?",
            (normalized_url,),
        ).fetchone()
    return row is not None


def save_posted_lead(normalized_url: str, monday_item_id: str, item_name: str) -> None:
    with sqlite3.connect(DATABASE_PATH) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO posted_leads
            (normalized_url, monday_item_id, item_name, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (normalized_url, monday_item_id, item_name, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()


def get_group_id_for_channel(channel_id: int | None) -> str:
    if channel_id is None:
        return MONDAY_DEFAULT_GROUP_ID
    return CHANNEL_GROUP_MAP.get(str(channel_id), MONDAY_DEFAULT_GROUP_ID)


def clean_text(value: str | None, max_len: int = 3000) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", value).strip()[:max_len]


def detect_platform(domain: str) -> str:
    domain = domain.lower()
    for needle, platform in PLATFORM_BY_DOMAIN.items():
        if needle in domain:
            return platform
    return "Other"


def detect_from_keywords(text: str, mapping: dict[str, list[str]], default: str) -> str:
    lower = text.lower()
    for label, keywords in mapping.items():
        if any(keyword in lower for keyword in keywords):
            return label
    return default


def detect_role(text: str) -> str:
    return detect_from_keywords(text, ROLE_KEYWORDS, "Other")


def detect_location_type(text: str) -> str:
    return detect_from_keywords(text, LOCATION_KEYWORDS, "")


def detect_category(text: str) -> str:
    return detect_from_keywords(text, CATEGORY_KEYWORDS, "Awaiting")


def get_meta_content(soup: BeautifulSoup, selectors: list[tuple[str, str]]) -> str:
    for attr, value in selectors:
        tag = soup.find("meta", attrs={attr: value})
        if tag and tag.get("content"):
            return clean_text(str(tag["content"]), 3000)
    return ""


def parse_linkedin_slug(url: str) -> tuple[str, str]:
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 2 and parts[0] == "posts":
        slug = parts[1]
        author_public_id = slug.split("_")[0]
        display = re.sub(r"-[a-f0-9]{6,}$", "", author_public_id, flags=re.IGNORECASE)
        display = display.replace("-", " ").strip().title()
        profile_url = f"https://www.linkedin.com/in/{author_public_id}/"
        return display, profile_url
    return "", ""


def extract_linkedin_post_text(soup: BeautifulSoup, fallback_description: str) -> str:
    candidates: list[str] = []
    for selector in [
        ("property", "og:description"),
        ("name", "description"),
        ("name", "twitter:description"),
    ]:
        value = get_meta_content(soup, [selector])
        if value:
            candidates.append(value)

    page_text = clean_text(soup.get_text(" "), 9000)
    if page_text:
        candidates.append(page_text)
    if fallback_description:
        candidates.append(fallback_description)

    bad_fragments = ["join linkedin", "sign in", "agree & join linkedin", "new to linkedin", "authwall"]
    for candidate in candidates:
        lower = candidate.lower()
        if len(candidate) > 20 and not all(fragment in lower for fragment in bad_fragments):
            return clean_text(candidate, 3000)
    return clean_text(fallback_description, 3000)


async def fetch_public_metadata(url: str) -> dict[str, Any]:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    }
    timeout = aiohttp.ClientTimeout(total=20)

    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(url, allow_redirects=True) as response:
                html = await response.text(errors="ignore")
                final_url = str(response.url)
                http_status = response.status
    except Exception as exc:
        parsed = urlparse(url)
        domain = parsed.netloc.lower().replace("www.", "")
        platform = detect_platform(domain)
        author, author_profile_url = parse_linkedin_slug(url) if platform == "LinkedIn" else ("", "")
        return {
            "final_url": url,
            "domain": domain,
            "platform": platform,
            "sourced_from": platform if platform in {"LinkedIn", "X"} else "LinkedIn",
            "title": f"{platform} Lead - Needs Review",
            "author": author,
            "author_profile_url": author_profile_url,
            "post_text": "",
            "emails": [],
            "role": "Other",
            "location_type": "",
            "category": "Awaiting",
            "scrape_status": f"Fetch failed: {exc}",
        }

    soup = BeautifulSoup(html, "html.parser")
    parsed = urlparse(final_url)
    domain = parsed.netloc.lower().replace("www.", "")
    platform = detect_platform(domain)
    sourced_from = platform if platform in {"LinkedIn", "X"} else "LinkedIn"

    title = get_meta_content(soup, [("property", "og:title"), ("name", "twitter:title")])
    if not title and soup.title and soup.title.string:
        title = clean_text(soup.title.string, 255)

    description = get_meta_content(
        soup,
        [("property", "og:description"), ("name", "description"), ("name", "twitter:description")],
    )

    author = ""
    author_profile_url = ""
    post_text = description
    if platform == "LinkedIn":
        author, author_profile_url = parse_linkedin_slug(final_url)
        if not author:
            author, author_profile_url = parse_linkedin_slug(url)
        post_text = extract_linkedin_post_text(soup, description)

    page_text = clean_text(soup.get_text(" "), 12000)
    emails = sorted(set(EMAIL_REGEX.findall(page_text)))[:5]
    combined_text = " ".join([title, description, post_text])
    role = detect_role(combined_text)
    location_type = detect_location_type(combined_text)
    category = detect_category(combined_text)

    if not title:
        title = f"{author} - {role} Lead" if author and role != "Other" else f"{platform} Lead - Needs Review"

    scrape_status = "Scraped public metadata"
    if http_status >= 400:
        scrape_status = f"HTTP {http_status}; created fallback lead"
    elif not post_text:
        scrape_status = "Limited metadata; needs manual review"

    return {
        "final_url": final_url,
        "domain": domain,
        "platform": platform,
        "sourced_from": sourced_from,
        "title": title,
        "author": author,
        "author_profile_url": author_profile_url,
        "post_text": post_text,
        "emails": emails,
        "role": role,
        "location_type": location_type,
        "category": category,
        "scrape_status": scrape_status,
    }


def status_value(label: str) -> dict[str, str]:
    return {"label": label}


def dropdown_value(label: str) -> dict[str, list[str]]:
    return {"labels": [label]}


def link_value(url: str, text: str) -> dict[str, str]:
    return {"url": url, "text": text or url}


def email_value(email: str) -> dict[str, str]:
    return {"email": email, "text": email}


def date_value(date_string: str) -> dict[str, str]:
    return {"date": date_string}


def build_monday_column_values(lead: LeadData) -> dict[str, Any]:
    values: dict[str, Any] = {
        COL_STATUS: status_value("New Leads"),
        COL_MARKET: status_value("Awaiting"),
        COL_POST_DATE: date_value(lead.post_date),
        COL_LINK_TO_JP: link_value(lead.original_url, "Job Post"),
        COL_PLATFORM: status_value(lead.platform if lead.platform in {"YouTube", "Instagram", "LinkedIn", "X", "TikTok", "Other"} else "Other"),
        COL_SOURCED_FROM: status_value(lead.sourced_from if lead.sourced_from in {"YTJobs", "Roster", "LinkedIn", "X", "YTCareers", "BucketofCrabs"} else "LinkedIn"),
        COL_CATEGORY: status_value(lead.category if lead.category in {"Awaiting", "YouTuber", "Creator", "Company", "Agency", "Personal Brand", "Startup"} else "Awaiting"),
        COL_PRIMARY_SKILL: dropdown_value(lead.role or "Other"),
        COL_ROLE_POSITION: dropdown_value(lead.role or "Other"),
    }

    if lead.location_type:
        values[COL_LOCATION_TYPE] = dropdown_value(lead.location_type)
    if lead.author:
        values[COL_COMPANY_CHANNEL] = lead.author
    if lead.author_profile_url:
        values[COL_LINKEDIN_PROFILE] = link_value(lead.author_profile_url, lead.author or "LinkedIn Profile")
    if lead.emails:
        values[COL_EMAIL] = email_value(lead.emails[0])

    description_parts = [
        f"Scraped Post Text / Metadata:\n{lead.post_text or 'No public post text found. Needs manual review.'}",
        "",
        "Lead Details:",
        f"Author / Company: {lead.author or 'Unknown'}",
        f"Platform: {lead.platform}",
        f"Sourced From: {lead.sourced_from}",
        f"Detected Role: {lead.role}",
        f"Detected Location Type: {lead.location_type or 'Not detected'}",
        f"Detected Category: {lead.category}",
        f"Source Channel: {lead.source_channel}",
        f"Scrape Status: {lead.scrape_status}",
        f"Scraped At: {lead.scraped_at}",
        f"URL: {lead.original_url}",
    ]
    if lead.emails:
        description_parts.append(f"Public Emails Found: {', '.join(lead.emails)}")
    values[COL_DESCRIPTION] = "\n".join(description_parts)
    return values


async def create_monday_item(lead: LeadData) -> str:
    mutation = """
    mutation CreateLead($board_id: ID!, $group_id: String!, $item_name: String!, $column_values: JSON!) {
        create_item(board_id: $board_id, group_id: $group_id, item_name: $item_name, column_values: $column_values) {
            id
        }
    }
    """
    payload = {
        "query": mutation,
        "variables": {
            "board_id": MONDAY_BOARD_ID,
            "group_id": lead.group_id,
            "item_name": clean_text(lead.item_name, 255) or "New Lead",
            "column_values": json.dumps(build_monday_column_values(lead)),
        },
    }
    headers = {"Authorization": MONDAY_API_TOKEN, "Content-Type": "application/json"}
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.post(MONDAY_API_URL, json=payload) as response:
            data = await response.json(content_type=None)
    if "errors" in data:
        raise RuntimeError(json.dumps(data["errors"], indent=2))
    item_id = data.get("data", {}).get("create_item", {}).get("id")
    if not item_id:
        raise RuntimeError(f"Monday did not return item ID: {data}")
    return str(item_id)


async def process_lead_url(url: str, channel_id: int | None = None, source_channel: str = "Discord") -> tuple[bool, str]:
    original_normalized = normalize_url(url)
    if is_duplicate(original_normalized):
        return False, f"Duplicate skipped: {original_normalized}"

    metadata = await fetch_public_metadata(url)
    final_url = metadata.get("final_url") or url
    final_normalized = normalize_url(final_url)
    if final_normalized != original_normalized and is_duplicate(final_normalized):
        return False, f"Duplicate skipped: {final_normalized}"

    author = metadata.get("author", "")
    role = metadata.get("role", "Other")
    title = metadata.get("title", "New Lead")
    if author and role != "Other":
        item_name = f"{author} - {role} Lead"
    elif author:
        item_name = f"{author} - LinkedIn Lead"
    else:
        item_name = title

    now = datetime.now(timezone.utc)
    lead = LeadData(
        original_url=final_url,
        normalized_url=final_normalized,
        item_name=item_name,
        platform=metadata.get("platform", "Other"),
        sourced_from=metadata.get("sourced_from", "LinkedIn"),
        role=role,
        location_type=metadata.get("location_type", ""),
        category=metadata.get("category", "Awaiting"),
        post_text=metadata.get("post_text", ""),
        author=author,
        author_profile_url=metadata.get("author_profile_url", ""),
        domain=metadata.get("domain", ""),
        emails=metadata.get("emails", []),
        group_id=get_group_id_for_channel(channel_id),
        source_channel=source_channel,
        scraped_at=now.isoformat(),
        post_date=now.date().isoformat(),
        scrape_status=metadata.get("scrape_status", "Unknown"),
    )
    monday_item_id = await create_monday_item(lead)
    save_posted_lead(lead.normalized_url, monday_item_id, lead.item_name)
    return True, f"Created Monday lead: {lead.item_name} | Item ID: {monday_item_id}"


class LeadBot(commands.Bot):
    async def setup_hook(self) -> None:
        if DISCORD_GUILD_ID.isdigit():
            guild = discord.Object(id=int(DISCORD_GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            print(f"Slash commands synced to Discord server {DISCORD_GUILD_ID}")
        else:
            await self.tree.sync()
            print("Slash commands synced globally. This can take longer to appear in Discord.")


intents = discord.Intents.default()
if AUTO_CAPTURE_LINKS:
    intents.message_content = True

bot = LeadBot(command_prefix="!", intents=intents)


@bot.event
async def on_ready() -> None:
    print(f"Logged in as {bot.user}")


@bot.tree.command(name="lead", description="Send a URL to Monday.com as a lead")
@app_commands.describe(url="LinkedIn post/profile or other public lead URL")
async def lead_command(interaction: discord.Interaction, url: str) -> None:
    if not URL_REGEX.search(url):
        await interaction.response.send_message("Send a valid URL starting with http:// or https://", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    channel_id = interaction.channel_id
    channel_name = getattr(interaction.channel, "name", "unknown")
    source_channel = f"Discord #{channel_name}"

    try:
        created, message = await process_lead_url(url=url, channel_id=channel_id, source_channel=source_channel)
    except Exception as exc:
        await interaction.followup.send(f"Could not create lead: {exc}", ephemeral=True)
        return
    await interaction.followup.send(("✅ " if created else "⚠️ ") + message, ephemeral=True)


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot:
        return
    await bot.process_commands(message)

    if not AUTO_CAPTURE_LINKS:
        return
    if ALLOWED_CHANNEL_IDS and message.channel.id not in ALLOWED_CHANNEL_IDS:
        return

    urls = URL_REGEX.findall(message.content or "")
    if not urls:
        return

    url = urls[0]
    channel_name = getattr(message.channel, "name", "unknown")
    source_channel = f"Discord #{channel_name}"
    try:
        created, result = await process_lead_url(url=url, channel_id=message.channel.id, source_channel=source_channel)
        await message.reply(("✅ " if created else "⚠️ ") + result, mention_author=False)
    except Exception as exc:
        await message.reply(f"Could not create lead: {exc}", mention_author=False)


def main() -> None:
    require_env()
    init_db()
    bot.run(DISCORD_BOT_TOKEN)


if __name__ == "__main__":
    main()
