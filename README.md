# LinkLeads

Discord slash-command bot that turns public lead links into Monday.com items.

## What it does

Use this in Discord:

```text
/lead https://www.linkedin.com/posts/...
```

The bot will:

- fetch public metadata from the link
- detect the likely creator-economy role
- create a lead in Monday.com
- dedupe repeated links with local SQLite

## Required environment variables

Create these as local `.env` values or hosting/GitHub secrets:

```env
DISCORD_BOT_TOKEN=your_discord_bot_token_here
MONDAY_API_TOKEN=your_monday_api_token_here
DISCORD_GUILD_ID=your_discord_server_id_here
MONDAY_BOARD_ID=18405764077
MONDAY_DEFAULT_GROUP_ID=group_mm1vwy0q
DISCORD_AUTO_CAPTURE_LINKS=false
DISCORD_ALLOWED_CHANNEL_IDS=
CHANNEL_GROUP_MAP={}
```

See `.env.example` for the full config, including Monday column IDs.

## Local setup

```bash
pip install -r requirements.txt
python discord_to_monday_leads.py
```

## Discord setup

In Discord Developer Portal, create an application and bot. Invite it with these scopes:

- `bot`
- `applications.commands`

Bot permissions:

- View Channels
- Send Messages
- Read Message History

For the first version, keep `DISCORD_AUTO_CAPTURE_LINKS=false` and use `/lead [url]`.

## Security

Do not commit `.env`, Discord bot tokens, Monday API tokens, or webhook URLs.
