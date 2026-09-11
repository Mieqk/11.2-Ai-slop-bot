"""/mute, /unmute, /mutegame, /unmutegame, /ban, /unban.

Every handler is a 4-step pipeline: parse duration -> resolve target -> guard
hierarchy -> delegate to core.moderation.Moderation, then confirm ephemerally.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # кругового импорта нет: cog'и грузятся строками
    from bot import ModBot

import discord
from discord import app_commands
from discord.ext import commands

from core import timeutil
from core.helpers import (
    DURATION_CHOICES,
    ModInputError,
    case_member,
    find_case,
    guard,
    resolve_member,
)
from core.moderation import Moderation as ModerationEngine
from drivers.base import DriverError


def _who_label(member, nick: str | None) -> str:
    """Кого показываем модератору: упоминание, ник игры или и то, и другое."""
    if member is not None:
        return getattr(member, "mention", None) or str(member)
    return f"ник `{nick}` (в Discord не отмечен)"


class Punishments(commands.Cog):
    """Наказания: мут в Discord, мут в игре, бан."""

    def __init__(self, bot: ModBot):
        self.bot = bot

    @property
    def mod(self) -> ModerationEngine:
        return self.bot.mod

    async def _game_target(self, inter, target: str | None, nick: str | None):
        """Кого наказываем в игре: участник Discord, ник, или и то и другое."""
        if not target and not nick:
            raise ModInputError("Укажите `цель:` (участник Discord) или `ник:` (игрок в игре).")
        member = None
        if target:
            member = await resolve_member(inter.guild, target)
            await guard(inter, member)
        return member, (nick.strip() if nick else None)

    def _duration(self, choice: app_commands.Choice[str] | None, default: str | None = None):
        try:
            return timeutil.parse_duration(choice.value if choice else default)
        except timeutil.DurationError as exc:
            raise ModInputError(str(exc)) from None

    # ------------------------------------------------------------------ mute
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True, moderate_members=True)
    @app_commands.command(name="mute", description="Мут в Discord: текст и голос на срок")
    @app_commands.choices(duration=DURATION_CHOICES)
    @app_commands.rename(duration="срок", target="цель", reason="причина", silent="тихо")
    @app_commands.describe(
        target="Участник: mention или числовой ID",
        duration="10m, 1h, 7d, perm (или свой вариант вроде 90m)",
        reason="Причина — попадёт в канал логов и в ЛС нарушителю",
        silent="Не публиковать в канал логов",
    )
    async def mute(
        self,
        inter: discord.Interaction,
        target: str,
        duration: app_commands.Choice[str] | None = None,
        reason: str | None = None,
        silent: bool = False,
    ) -> None:
        delta = self._duration(duration)
        member = await resolve_member(inter.guild, target)
        await guard(inter, member)
        case_id = await self.mod.mute(
            inter.user, member, delta, reason or self.bot.cfg["default_reason"], silent=silent
        )
        await inter.response.send_message(
            f"🔇 {member.mention} — мут **{timeutil.humanize_long(delta)}** · №{case_id}.",
            ephemeral=True,
        )

    # ---------------------------------------------------------------- unmute
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True, moderate_members=True)
    @app_commands.command(name="unmute", description="Снять мут (по цели или номеру записи)")
    @app_commands.rename(target="цель", case="номер", reason="причина")
    @app_commands.describe(
        target="Участник с активным мутом",
        case="Номер записи, если мутов несколько",
        reason="Комментарий для лога",
    )
    async def unmute(
        self,
        inter: discord.Interaction,
        target: str | None = None,
        case: int | None = None,
        reason: str | None = None,
    ) -> None:
        if not target and not case:
            raise ModInputError("Укажите цель или номер записи из /modlog.")
        if case:  # номер кейса важнее цели
            found = await find_case(self.bot.db, inter.guild.id, case, actions=("mute", "mutegame"))
            member = await case_member(inter.guild, found)
        else:
            member = await resolve_member(inter.guild, target or "")
        try:
            done = await self.mod.unmute(inter.user, member, reason or "Снято вручную", case_id=case)
        except ValueError as exc:
            raise ModInputError(str(exc)) from None
        await inter.response.send_message(f"✅ Мут снят · №{done}.", ephemeral=True)

    # -------------------------------------------------------------- mutegame
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True, moderate_members=True)
    @app_commands.command(name="mutegame", description="Мут в игре: голос/чат на игровом сервере")
    @app_commands.choices(duration=DURATION_CHOICES)
    @app_commands.rename(duration="срок", target="цель", nick="ник", reason="причина", silent="тихо")
    @app_commands.describe(
        target="Участник Discord (если он есть на сервере)",
        nick="Ник в игре — если участника нет в Discord или он не привязан",
        duration="Срок игрового мута",
        reason="Причина",
        silent="Не публиковать в канал логов",
    )
    async def mutegame(
        self,
        inter: discord.Interaction,
        target: str | None = None,
        nick: str | None = None,
        duration: app_commands.Choice[str] | None = None,
        reason: str | None = None,
        silent: bool = False,
    ) -> None:
        who, game_nick = await self._game_target(inter, target, nick)
        delta = self._duration(duration)
        try:
            case_id = await self.mod.mutegame(
                inter.user, who, delta, reason or self.bot.cfg["default_reason"],
                game_id=game_nick, guild=inter.guild, silent=silent,
            )
        except DriverError as exc:
            raise ModInputError(f"Драйвер игры: {exc}") from None
        except ValueError as exc:
            raise ModInputError(str(exc)) from None
        await inter.response.send_message(
            f"🎮 {_who_label(who, game_nick)} — мут в игре **{timeutil.humanize_long(delta)}** · №{case_id}.",
            ephemeral=True,
        )

    # ------------------------------------------------------------- unmutegame
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True, moderate_members=True)
    @app_commands.command(name="unmutegame", description="Снять мут в игре по номеру записи")
    @app_commands.rename(case="номер", reason="причина")
    async def unmutegame(self, inter: discord.Interaction, case: int, reason: str | None = None) -> None:
        await find_case(self.bot.db, inter.guild.id, case, actions=("mutegame",))
        try:
            await self.mod.unmutegame(inter.user, case, reason or "Снято вручную")
        except (DriverError, ValueError) as exc:
            raise ModInputError(str(exc)) from None
        await inter.response.send_message(f"✅ Мут в игре снят · №{case}.", ephemeral=True)

    # ---------------------------------------------------------- ban in game
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True, moderate_members=True)
    @app_commands.command(name="bangame", description="Бан на игровом сервере (не в Discord)")
    @app_commands.choices(duration=DURATION_CHOICES)
    @app_commands.rename(duration="срок", target="цель", nick="ник", reason="причина", silent="тихо")
    @app_commands.describe(
        target="Участник Discord (если он есть на сервере)",
        nick="Ник в игре — можно без участника Discord, тогда бан выдаётся только по нику",
        duration="perm = до /unbangame",
        reason="Причина бана",
        silent="Не публиковать в канале логов",
    )
    async def bangame(
        self,
        inter: discord.Interaction,
        target: str | None = None,
        nick: str | None = None,
        duration: app_commands.Choice[str] | None = None,
        reason: str | None = None,
        silent: bool = False,
    ) -> None:
        who, game_nick = await self._game_target(inter, target, nick)
        delta = self._duration(duration, "perm")
        try:
            case_id = await self.mod.bangame(
                inter.user, who, delta, reason or self.bot.cfg["default_reason"],
                game_id=game_nick, guild=inter.guild, silent=silent,
            )
        except DriverError as exc:
            raise ModInputError(f"Драйвер игры: {exc}") from None
        except ValueError as exc:
            raise ModInputError(str(exc)) from None
        await inter.response.send_message(
            f"🚫 {_who_label(who, game_nick)} — бан в игре на **{timeutil.humanize_long(delta)}** · №{case_id}.",
            ephemeral=True,
        )

    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True, moderate_members=True)
    @app_commands.command(name="unbangame", description="Разбанить на игровом сервере по номеру записи")
    @app_commands.rename(case="номер", reason="причина")
    async def unbangame(self, inter: discord.Interaction, case: int, reason: str | None = None) -> None:
        await find_case(self.bot.db, inter.guild.id, case, actions=("bangame",))
        try:
            await self.mod.unbangame(inter.user, case, reason or "Разбанен вручную")
        except (DriverError, ValueError) as exc:
            raise ModInputError(str(exc)) from None
        await inter.response.send_message(f"✅ Разбан в игре · №{case}.", ephemeral=True)

    # ------------------------------------------------------------------- ban
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(ban_members=True, moderate_members=True)
    @app_commands.command(name="ban", description="Бан: срок или навсегда (perm)")
    @app_commands.choices(duration=DURATION_CHOICES)
    @app_commands.rename(duration="срок", target="цель", reason="причина")
    @app_commands.describe(
        target="Участник или ID",
        duration="perm = навсегда; иначе авто-разбан при истечении",
        reason="Причина бана",
    )
    async def ban(
        self,
        inter: discord.Interaction,
        target: str,
        duration: app_commands.Choice[str] | None = None,
        reason: str | None = None,
    ) -> None:
        delta = self._duration(duration, "perm")
        member = await resolve_member(inter.guild, target)
        await guard(inter, member)
        try:
            case_id = await self.mod.ban(
                inter.user, member, delta, reason or self.bot.cfg["default_reason"]
            )
        except discord.Forbidden:
            raise ModInputError("Нет права Ban Members или цель выше меня в иерархии.") from None
        await inter.response.send_message(
            f"🔨 {member} забанен на **{timeutil.humanize_long(delta)}** · №{case_id}.",
            ephemeral=True,
        )

    # ----------------------------------------------------------------- unban
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(ban_members=True)
    @app_commands.command(name="unban", description="Разбанить по ID пользователя")
    @app_commands.rename(user_id="id", reason="причина")
    async def unban(self, inter: discord.Interaction, user_id: str, reason: str | None = None) -> None:
        if not user_id.isdigit():
            raise ModInputError("Нужен числовой ID пользователя (Разбан работает и для покинувших сервер).")
        try:
            done = await self.mod.unban(inter.user, int(user_id), reason or "Разбан вручную")
        except ValueError as exc:
            raise ModInputError(str(exc)) from None
        await inter.response.send_message(f"✅ ID `{user_id}` разбанен · №{done}.", ephemeral=True)

    # ---------------------------------------------------------------- errors
    @mute.error
    @unmute.error
    @mutegame.error
    @unmutegame.error
    @bangame.error
    @unbangame.error
    @ban.error
    @unban.error
    async def _on_error(self, inter: discord.Interaction, error: app_commands.AppCommandError) -> None:
        if isinstance(error, app_commands.MissingPermissions):
            missing = ", ".join(p.replace("_", " ").capitalize() for p in error.missing_permissions)
            msg = f"Не хватает прав: {missing}"
        elif isinstance(error, DriverError):
            msg = f"Драйвер игры: {error}"
        else:
            msg = str(error)
        text = f"⚠️ {msg[:1800]}"
        # явно, а не `**payload`: response и followup — разные перегрузки
        if inter.response.is_done():
            await inter.followup.send(text, ephemeral=True)
        else:
            await inter.response.send_message(text, ephemeral=True)


async def setup(bot: ModBot) -> None:
    await bot.add_cog(Punishments(bot))
