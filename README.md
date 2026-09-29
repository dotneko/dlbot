# dlbot

A Discord bot that queues YouTube/Instagram video downloads. Users post a
slash command in a channel, the URL is validated and placed in a SQLite
queue, and a separate worker process drains the queue with `yt-dlp`. When a
job finishes the bot announces the result back in the channel.

```
user ─▶ /download ─▶ bot (discord.py) ─▶ SQLite queue ◀── polled by ── worker (yt-dlp)
                                                        │
                          bot polls DB ──▶ ✅/❌ embed ─┘  downloads/
```

## How it works

- **`/download url:<link> [format:video|audio] [prefix:<str>] [suffix:<str>] [subdir:<str>]`**
  validates the URL (YouTube or Instagram Reel/post only), sanitizes the
  parameters, and enqueues the job. The user gets an immediate embed with the
  job number and queue position.
- The **worker** claims jobs atomically (`BEGIN IMMEDIATE` on a WAL-mode
  SQLite DB, so multiple workers can't double-claim), downloads into a
  per-job temp dir, then moves the file to `downloads/[subdir]/` with a
  sanitized filename.
- **Filenames**: spaces → `_`, emoji/symbols/punctuation removed,
  letters/digits kept (any language), e.g.
  `New Episode 😊 #12 (HD)!` → `New_Episode_12_HD.mp4`. Prefix/suffix are
  sanitized the same way and joined with `_`. The title is truncated to
  150 chars; if nothing survives, the video ID is used.
- **Formats**: `video` → best mp4 (merged; needs `ffmpeg`); `audio` → mp3
  (needs `ffmpeg`).
- **`subdir`**: optional single folder name under `downloads/` the file is
  moved to after download (e.g. `tutorials`). Path separators and `.`/`..`
  are rejected.
- **Retries**: failed attempts are requeued up to `DLBOT_MAX_ATTEMPTS`
  (default 2), then the job is marked `failed` with the error stored in the
  DB.
- The **bot** polls the DB and posts a ✅/❌ embed to the requesting channel
  when a job reaches a terminal state.

## Setup

### 1. Prerequisites

- [uv](https://docs.astral.sh/uv/)
- `ffmpeg` on your PATH (required for mp4 merging and mp3 conversion):
  `brew install ffmpeg` (macOS) or `apt install ffmpeg` (Linux)

### 2. Discord application

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications)
   → **New Application** → give it a name.
2. **Bot** tab → **Reset Token** → copy the token (you'll paste it into `.env`).
3. **OAuth2 → URL Generator**:
   - Scopes: `bot`, `applications.commands`
   - Bot permissions: `Send Messages`, `Embed Links`
   - Open the generated URL and invite the bot to your server.
4. *(Recommended while developing)* Developer mode in Discord settings →
   right-click your server → *Copy Server ID*; right-click a channel →
   *Copy Channel ID*. Use these for instant command sync and channel
   restriction.

### 3. Configure

```bash
cp .env.example .env
Key settings:

| Variable | Default | Purpose |
|---|---|---|
| `DISCORD_TOKEN` | — | **required** bot token |
| `DISCORD_CHANNEL_ID` | *(empty)* | restrict commands to one channel |
| `DISCORD_GUILD_ID` | *(empty)* | guild-scoped (instant) slash sync while developing |
| `DLBOT_DATABASE` | `data/dlbot.db` | SQLite queue location |
| `DLBOT_DOWNLOAD_DIR` | `downloads` | where files are stored |
| `DLBOT_LOG_DIR` | `logs` | log files |
| `DLBOT_WORKER_POLL_INTERVAL` | `3` | seconds between queue polls |
| `DLBOT_MAX_ATTEMPTS` | `2` | attempts before a job is failed |
| `DLBOT_COOKIES_FILE` | *(empty)* | Netscape `cookies.txt` (e.g. for Instagram) |
| `DLBOT_LOG_LEVEL` | `INFO` | console log level (files are always DEBUG) |

> **Instagram note**: Instagram frequently requires authentication for
> downloads. Export a Netscape-format `cookies.txt` (e.g. with a browser
> extension like "Get cookies.txt LOCALLY") and point `DLBOT_COOKIES_FILE`
> at it.

### 4. Run

```bash
uv run dlbot          # worker (separate process) + bot, stops together
uv run dlbot bot      # bot only (use if the worker runs elsewhere)
uv run dlbot worker   # worker only
```

On first start the slash commands sync. **Global** sync can take up to an
hour to appear in Discord — set `DISCORD_GUILD_ID` for instant sync in one
server, and restart the bot after code changes.

## Slash command

```
/download url:https://youtu.be/dQw4w9WgXcQ format:video prefix:my suffix:v2 subdir:tutorials
```

| Parameter | Required | Description |
|---|---|---|
| `url` | yes | YouTube video or Instagram Reel/post link |
| `format` | no | `video` (default, mp4) or `audio` (mp3) |
| `prefix` | no | prepended to the sanitized title |
| `suffix` | no | appended to the sanitized title |
| `subdir` | no | single folder name the file is moved into |

Also available: `/logtail` — shows the last lines of the worker log
(ephemeral) for debugging failed jobs.

## Logging & debugging

- Console + rotating files: `logs/bot.log` and `logs/worker.log`
  (always DEBUG, 5 MB × 5 backups).
- yt-dlp output (progress, errors) is logged under the `dlbot.download`
  logger at DEBUG level in the worker log.
- Discord HTTP responses/errors are captured at DEBUG (`discord.http`),
  including API error bodies — the usual first stop for bot problems.
- Failed jobs keep their error message in the DB
  (`SELECT * FROM download_queue WHERE status='failed'`) and are
  announced in-channel.

## Data layout

```
dlbot/
├── data/dlbot.db          # SQLite queue (WAL mode)
├── downloads/             # final files (+ optional subdirs)
│   └── .tmp/              # per-job working dirs (cleaned up)
└── logs/                  # bot.log, worker.log (rotated)
```

## Troubleshooting

- **Command doesn't appear**: check the invite URL included the
  `applications.commands` scope; set `DISCORD_GUILD_ID` for instant guild
  sync; restart the bot after code changes.
- **"ffmpeg was not found"**: install ffmpeg (see Prerequisites).
- **Instagram 403 / cookies error**: set `DLBOT_COOKIES_FILE` to a fresh
  `cookies.txt`.
- **Job stuck `processing`**: the worker died mid-download. Let it be
  retried or reset it:
  `UPDATE download_queue SET status='pending' WHERE status='processing';`
- **Slow first run**: global slash command sync can take up to an hour;
  guild sync is instant.
