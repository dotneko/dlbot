"""The Discord client: slash commands, queueing, and completion notifications."""

from __future__ import annotations

import asyncio
import logging
from typing import Literal, Optional

import discord
from discord import app_commands

from .config import Settings
from .database import (
    STATUS_SUCCESS,
    Database,
    QueueJob,
)
from .logging_config import get_logger, log_exception, setup_logging, tail_recent_logs
from .urls import (
    InvalidSubdirError,
    InvalidURLError,
    parse_media_url,
    validate_subdir,
)

log = get_logger("bot")
log_http = logging.getLogger("discord.http")


class DownloadCommand(app_commands.Command):
    """/download — queue a YouTube or Instagram video for download."""

    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db
        super().__init__(
            name="download",
            description="Queue a YouTube or Instagram video for download",
            callback=self._callback,
        )

    @app_commands.describe(
        url="YouTube or Instagram video/Reel link to download",
        format="video (mp4) or audio (mp3); if omitted, uses the configured default",
        prefix="Text to prepend to the (sanitized) title",
        suffix="Text to append to the (sanitized) title",
        subdir="Single folder name to move the file into",
    )
    async def _callback(
        self,
        interaction: discord.Interaction,
        url: str,
        format: Optional[Literal["video", "audio"]] = None,
        prefix: str = None,
        suffix: str = None,
        subdir: str = None,
    ) -> None:
        settings, db = self.settings, self.db
        # Use the user's explicit choice when given, otherwise the configured default.
        media_format = format if format is not None else settings.default_format

        # Channel restriction (the bot "monitors" a specific channel).
        if settings.discord_channel_id and interaction.channel_id != settings.discord_channel_id:
            await interaction.response.send_message(
                f"❌ This command is only enabled in <#{settings.discord_channel_id}>.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=False)

        try:
            parsed = parse_media_url(url)
            clean_subdir = validate_subdir(subdir) if subdir else ""
            clean_prefix = (prefix or "").strip()[:60]
            clean_suffix = (suffix or "").strip()[:60]
        except (InvalidURLError, InvalidSubdirError) as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return

        job_id = db.enqueue_download(
            url=parsed.url,
            platform=parsed.platform,
            media_type=media_format,
            prefix=clean_prefix,
            suffix=clean_suffix,
            subdir=clean_subdir,
            requested_by=str(interaction.user),
            channel_id=interaction.channel_id,
            guild_id=interaction.guild_id,
            max_attempts=settings.max_attempts,
        )
        position = db.pending_count()

        embed = discord.Embed(
            title=f"📥 Queued download #{job_id}",
            description=parsed.url,
            color=discord.Color.blue(),
        )
        embed.add_field(name="Platform", value=parsed.platform, inline=True)
        embed.add_field(name="Format", value=media_format, inline=True)
        embed.add_field(name="Queue position", value=str(position), inline=True)
        if clean_subdir:
            embed.add_field(name="Subdir", value=clean_subdir, inline=True)
        if clean_prefix or clean_suffix:
            embed.add_field(
                name="Filename decoration",
                value=f"prefix=`{clean_prefix or '—'}` suffix=`{clean_suffix or '—'}`",
            )
        embed.set_footer(text=f"Requested by {interaction.user}")
        await interaction.followup.send(
            f"Queued as **#{job_id}** — {position} job(s) ahead (including this one). "
            "I'll post the result here when the worker finishes.",
            embed=embed,
        )
        log.info(
            "job %d enqueued by %s in channel %s (%s %s)",
            job_id, interaction.user, interaction.channel_id,
            parsed.platform, media_format,
        )


class LogTailCommand(app_commands.Command):
    """/logtail — show the recent worker log for debugging failed jobs."""

    def __init__(self, settings: Settings):
        self.settings = settings
        super().__init__(
            name="logtail",
            description="Show the last lines of the worker log (for debugging)",
            callback=self._callback,
        )

    async def _callback(self, interaction: discord.Interaction) -> None:
        settings = self.settings
        if settings.discord_channel_id and interaction.channel_id != settings.discord_channel_id:
            await interaction.response.send_message(
                f"❌ This command is only enabled in <#{settings.discord_channel_id}>.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        content = tail_recent_logs(settings, "worker", max_lines=40)
        if not content:
            content = "The worker log is empty — has the worker started yet?"
        await interaction.followup.send(f"```{content}```", ephemeral=True)


class DLBot(discord.Client):
    """Client wiring: command registration, command sync, notification loop."""

    def __init__(self, settings: Settings, db: Database):
        intents = discord.Intents.default()  # gateway intents not needed for slash commands
        super().__init__(intents=intents)
        self.settings = settings
        self.db = db
        self.download_command = DownloadCommand(settings, db)
        self.logtail_command = LogTailCommand(settings)
        self.tree = discord.app_commands.CommandTree(self)
        self.tree.add_command(self.download_command)
        self.tree.add_command(self.logtail_command)
        self._notifier_task: asyncio.Task | None = None

    # --- lifecycle ---------------------------------------------------------

    async def setup_hook(self) -> None:
        if not self.settings.discord_guild_id:
            await self.tree.sync()  # global
        else:
            guild = discord.Object(id=self.settings.discord_guild_id)
            await self.tree.sync(guild=guild)
            log.info("Slash commands synced for guild %s (instant)", guild.id)
        self._notifier_task = asyncio.create_task(
            self._notify_loop(), name="completion-notifier"
        )

    async def close(self) -> None:
        if self._notifier_task is not None:
            self._notifier_task.cancel()
        await super().close()

    async def on_ready(self) -> None:
        log.info("Logged in as %s (id=%s)", self.user, self.user.id)

    async def on_app_command_error(
        self, interaction: discord.Interaction, command, error: Exception
    ) -> None:
        log_exception(log, error)
        # `error` here may be a CommandInvokeError wrapping the real cause.
        cause = getattr(error, "__cause__", None) or error
        message = str(cause)[:500]
        if interaction.response.is_done():
            await interaction.followup.send(f"❌ Something went wrong: {message}",
                                            ephemeral=True)
        else:
            await interaction.response.send_message(
                f"❌ Something went wrong: {message}", ephemeral=True
            )

    # --- completion notifier ---------------------------------------------------

    async def _notify_loop(self) -> None:
        """Poll the DB for finished jobs and announce them in their channel."""
        await self.wait_until_ready()
        interval = max(2.0, self.settings.poll_interval)
        while True:
            try:
                for job in self.db.fetch_unnotified_completed():
                    await self._announce(job)
                    self.db.mark_notified(job.id)
            except Exception as exc:  # noqa: BLE001 - keep the notifier alive
                log_exception(log, exc)
            await asyncio.sleep(interval)

    async def _announce(self, job: QueueJob) -> None:
        channel = None
        try:
            channel = self.get_channel(job.channel_id)
            if channel is None and job.guild_id:
                guild = self.get_guild(job.guild_id)
                if guild is not None:
                    channel = guild.get_channel(job.channel_id)
        except Exception:  # noqa: BLE001 - channels are best-effort
            pass
        if channel is None:
            log.warning(
                "job %d: cannot find channel %s to announce in", job.id, job.channel_id
            )
            return

        if job.status == STATUS_SUCCESS:
            size_mb = (job.file_size or 0) / (1024 * 1024)
            embed = discord.Embed(
                title=f"✅ Download #{job.id} complete",
                description=f"**{job.title or 'media'}**",
                color=discord.Color.green(),
            )
            embed.add_field(name="File", value=f"`{job.file_path}`", inline=False)
            embed.add_field(name="Size", value=f"{size_mb:.2f} MB", inline=True)
            embed.add_field(name="Format", value=job.media_type, inline=True)
            if job.subdir:
                embed.add_field(name="Subdir", value=job.subdir, inline=True)
            embed.add_field(name="Attempts", value=str(job.attempt), inline=True)
        else:
            embed = discord.Embed(
                title=f"❌ Download #{job.id} failed",
                description=f"**{job.title or job.url}**",
                color=discord.Color.red(),
            )
            error_text = job.error or "Unknown error"
            if len(error_text) > 1024:
                error_text = error_text[:1021] + "..."
            embed.add_field(name="Error", value=f"```\n{error_text}\n```", inline=False)
            embed.add_field(name="Attempts", value=str(job.attempt), inline=True)

        embed.set_footer(text=f"Requested by {job.requested_by or 'unknown'}")
        try:
            await channel.send(embed=embed)
            log.info("job %d: announced in channel %s (%s)",
                     job.id, job.channel_id, job.status)
        except discord.HTTPException as exc:
            log.warning("job %d: could not announce in channel %s: %s",
                        job.id, job.channel_id, exc)


def run_bot(settings: Settings) -> int:
    """Entry point used by ``dlbot bot`` (and the bot half of ``dlbot``)."""
    setup_logging("bot", settings)
    if not settings.discord_token:
        raise SystemExit(
            "ERROR: DISCORD_TOKEN is not set. Copy .env.example to .env and "
            "fill in the token, or export DISCORD_TOKEN."
        )
    db = Database(settings.database_path)
    bot = DLBot(settings, db)
    # Surface Discord API errors (4xx/5xx with bodies) at DEBUG into our log.
    log_http.setLevel(logging.DEBUG)
    try:
        bot.run(settings.discord_token, log_handler=None)
    except discord.LoginFailure as exc:
        log.error("Discord login failed: %s — check DISCORD_TOKEN", exc)
        return 1
    return 0