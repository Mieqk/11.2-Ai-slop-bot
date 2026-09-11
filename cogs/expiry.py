"""Background loop: auto-lift expired punishments, expire stale warns,
re-arm mutes that survived a bot restart.

Discord's own timeouts self-heal, but two things do not:
  * game-driver mutes (RCON etc.) — they must be pushed again after a restart
    and pulled when they run out;
  * mute roles / overwrites applied by the fallback path.
So the loop sweeps the `cases` table every minute.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # кругового импорта нет: cog'и грузятся строками
    from bot import ModBot

import json
import logging
from datetime import timedelta

import discord
from discord.ext import commands, tasks

from core import audit, timeutil
from core.moderation import Moderation as ModerationEngine
from drivers.base import DriverError

log = logging.getLogger("modbot.expiry")

WARN_RETENTION_DAYS = 90  # older warns stop counting towards escalation


class Expiry(commands.Cog):
    """Авто-снятие истёкших наказаний."""

    def __init__(self, bot: ModBot):
        self.bot = bot
        self._sweeping = False

    @property
    def mod(self) -> ModerationEngine:
        return self.bot.mod

    def cog_unload(self):  # без аннотации: базовый метод может быть coroutine
        self.sweep.cancel()

    @tasks.loop(minutes=1)
    async def sweep(self) -> None:
        if self._sweeping:
            return
        self._sweeping = True
        try:
            await self._sweep()
        except Exception:  # noqa: BLE001 - a background loop must never die
            log.exception("sweep упал")
        finally:
            self._sweeping = False

    @sweep.before_loop
    async def wait_until_ready(self) -> None:
        await self.bot.wait_until_ready()

    async def _sweep(self) -> None:
        now = discord.utils.utcnow()
        await self._lift_expired("mutegame", now)
        await self._lift_expired("bangame", now)
        await self._lift_expired("mute", now)
        await self._lift_expired("ban", now)
        await self._rearm_game_mutes(now)
        await self._prune_warns(now)
        await self._age_out_warns(now)

    # ------------------------------------------------------------- expired
    async def _lift_expired(self, action: str, now) -> None:
        for case in await self.bot.db.active_cases(action=action):
            expires = timeutil.from_iso(case["expires_at"])
            if expires is None or expires > now:
                continue
            guild = self.bot.get_guild(int(case["guild_id"]))
            if guild is None:
                await self.bot.db.lift(int(case["id"]), None)
                continue
            member = guild.get_member(int(case["user_id"]))
            try:
                if action == "ban":
                    await self._auto_unban(guild, case, member)
                elif action == "mutegame":
                    await self.mod.unmutegame(None, int(case["id"]), "auto: срок истёк")
                elif action == "bangame":
                    await self.mod.unbangame(None, int(case["id"]), "auto: срок истёк")
                elif member is not None:
                    await self.mod.unmute(None, member, "auto: срок истёк", case_id=int(case["id"]))
                else:
                    await self.bot.db.lift(int(case["id"]), None)
                    continue
                log.info("авто-снятие: кейс #%s (%s)", case["id"], action)
            except (DriverError, ValueError, discord.HTTPException):
                log.exception("не смог авто-снять кейс #%s", case["id"])

    async def _auto_unban(self, guild: discord.Guild, case, member) -> None:
        target = member or discord.utils.get(self.bot.users, id=int(case["user_id"]))
        if target is None:
            await self.bot.db.lift(int(case["id"]), None)
            return
        try:
            await guild.unban(target, reason=f"ban #{case['id']}: срок истёк")
        except discord.NotFound:
            pass
        except discord.Forbidden:
            log.warning("нет прав на разбан #%s — разбаньте вручную", case["id"])
            return
        await self.bot.db.lift(int(case["id"]), None)
        embed = audit.case_embed(
            "auto", target=target, moderator=None,
            reason="Бан по времени истёк",
        )
        await self.mod.post(guild, embed)

    # ------------------------------------------------------------- re-arm
    async def _rearm_game_mutes(self, now) -> None:
        """Re-send active game mutes so a restarted game server stays silenced."""
        for case in await self.bot.db.active_cases(action="mutegame"):
            extra = json.loads(case["extra"] or "{}")
            guild = self.bot.get_guild(int(case["guild_id"]))
            driver = await self.mod.driver_by_id(str(extra.get("driver") or ""))
            if guild is None or driver is None or not driver.idempotent_apply:
                continue
            try:
                await driver.mute(guild, str(extra.get("game_id")), 0, "re-arm после рестарта")
            except DriverError:
                log.debug("re-arm кейса #%s не удался (игра офлайн?)", case["id"])

    # ------------------------------------------------------------- warn ttl
    async def _prune_warns(self, now) -> None:
        """Close warns whose own timer ran out.

        Counting already ignores expired warns, so without this a row would sit
        as "active" in /history forever.
        """
        for case in await self.bot.db.expired_warns(now.isoformat()):
            await self.bot.db.lift(int(case["id"]), None)
            log.info("варн #%s истёк по своему сроку", case["id"])

    async def _age_out_warns(self, now) -> None:
        """Warns with no timer stop feeding the ladder after WARN_RETENTION_DAYS."""
        cutoff = now - timedelta(days=WARN_RETENTION_DAYS)
        for case in await self.bot.db.active_cases(action="warn"):
            issued = timeutil.from_iso(case["issued_at"])
            if issued and issued < cutoff:
                await self.bot.db.lift(int(case["id"]), None)
                log.info("варн #%s сгорел по сроку давности", case["id"])


async def setup(bot: ModBot) -> None:
    cog = Expiry(bot)
    await bot.add_cog(cog)
    cog.sweep.start()
