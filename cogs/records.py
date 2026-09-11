"""/modlog, /case, /modsettings — reading and configuring the punishment store."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # кругового импорта нет: cog'и грузятся строками
    from bot import ModBot

import json

import discord
from discord import app_commands
from discord.ext import commands

from types import SimpleNamespace

from core import audit, keepalive, timeutil
from core.helpers import ModInputError, resolve_member


def _keepalive_state(bot, cfg) -> str:
    if bot.http_keepalive is None:
        port = keepalive.enabled(cfg)
        return (
            "выключен — включается `keepalive: true` в config.json "
            "(или сам, если хост задал $PORT)"
            if not port
            else f"настройка есть, но не слушается (порт {port})"
        )
    return f"слушает :{bot.http_keepalive.port} — GET /healthz"


def _who(row) -> str:
    """Кому: упоминание участника или ник игры, если Discord-адресата нет."""
    if int(row["user_id"]) == 0:
        extra = json.loads(row["extra"] or "{}")
        return f"ник {audit.code(extra.get('game_id') or '?')} (в Discord не отмечен)"
    return f"<@{row['user_id']}>"


class Records(commands.Cog):
    """Журнал наказаний и настройки сервера."""

    def __init__(self, bot: ModBot):
        self.bot = bot

    # -------------------------------------------------------------- /modlog
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True, moderate_members=True)
    @app_commands.command(name="modlog", description="Последние наказания на сервере")
    @app_commands.rename(action="действие", limit="лимит", target="цель")
    @app_commands.describe(
        action="Фильтр по типу: mute, mutegame, warn, ban, unban",
        limit="Сколько записей показать (1-25)",
        target="Показать только записи этого участника",
    )
    async def modlog(
        self,
        inter: discord.Interaction,
        action: str | None = None,
        limit: int = 15,
        target: str | None = None,
    ) -> None:
        if action and action.lower() not in {"mute", "mutegame", "bangame", "warn", "ban"}:
            raise ModInputError("Действие: mute, mutegame, bangame, warn или ban.")
        limit = max(1, min(limit, 25))
        if target:
            member = await resolve_member(inter.guild, target)
            rows = await self.bot.db.user_cases(inter.guild.id, member.id, limit)
            title = f"Наказания · {member.display_name}"
        else:
            rows = await self.bot.db.recent_cases(inter.guild.id, limit * 4)
            title = f"Последние наказания · {inter.guild.name}"
        if action:
            rows = [r for r in rows if r["action"] == action.lower()][:limit]
        rows = rows[:limit]
        if not rows:
            raise ModInputError("Записей нет.")
        embed = discord.Embed(
            title=title,
            description="\n".join(self._line(r) for r in rows)[:3900],
            colour=audit.COLOR_INFO,
            timestamp=discord.utils.utcnow(),
        )
        await inter.response.send_message(embed=embed, ephemeral=True)

    @staticmethod
    def _line(row) -> str:
        issued = timeutil.from_iso(row["issued_at"])
        state = "снято" if row["lifted_at"] else ("истекает" if row["expires_at"] else "активно")
        icon = {"mute": "🔇", "mutegame": "🎮", "bangame": "🚫", "warn": "⚠️", "ban": "🔨"}.get(row["action"], "•")
        return f"{icon} №{row['id']} `{row['action']:<9}` {_who(row)} · {state} · {issued:%d.%m.%y %H:%M}"

    # --------------------------------------------------------------- /case
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True, moderate_members=True)
    @app_commands.command(name="case", description="Карточка записи по номеру и её снятие")
    @app_commands.rename(case="номер", lift="снять", reason="причина")
    @app_commands.describe(case="Номер записи из /modlog", lift="Снять это наказание", reason="Комментарий")
    async def case(self, inter: discord.Interaction, case: int, lift: bool = False, reason: str | None = None) -> None:
        await self._case(inter, case, lift, reason)

    @app_commands.command(name="ping", description="Проверка, что бот жив")
    async def ping(self, inter: discord.Interaction) -> None:
        await inter.response.send_message(
            f"🏓 {round(inter.client.latency * 1000)} мс", ephemeral=True
        )

    @app_commands.command(name="whoami", description="Что видит бот: сервер, права, драйвер")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def whoami(self, inter: discord.Interaction) -> None:
        guild = inter.guild
        me = guild.me
        active = await self.bot.db.active_cases(guild.id)
        driver_id, driver = await self.bot.mod.resolve_driver(guild.id)
        log_channel = await self.bot.mod.log_channel(guild)
        lines = [
            f"**Сервер:** {guild.name} (`{guild.id}`)",
            f"**Бот:** {me} · администратор: {me.guild_permissions.administrator}",
            f"**Права:** mute={'✅' if me.guild_permissions.mute_members else '❌'} "
            f"timeout={'✅' if me.guild_permissions.moderate_members else '❌'} "
            f"ban={'✅' if me.guild_permissions.ban_members else '❌'} "
            f"roles={'✅' if me.guild_permissions.manage_roles else '❌'}",
            f"**Канал логов:** {log_channel.mention if log_channel else '❌ не задан (/modsettings)'}",
            f"**Драйвер игры:** `{driver_id}` — {driver.title}"
            + ("" if driver.ok else " ⚠️ не настроен"),
            f"**Активных наказаний:** {len(active)}",
            f"**Health-эндпоинт:** {_keepalive_state(self.bot, self.bot.cfg)}",
        ]
        await inter.response.send_message("\n".join(lines), ephemeral=True)

    # ------------------------------------------------------- /modsettings
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.command(name="modsettings", description="Настроить канал логов, роль мута, драйвер игры, подсказку")
    @app_commands.rename(log_channel="канал_логов", mute_role="роль_мута", driver="драйвер",
                         dm="лд", dry_run="тест", feedback_hint="подсказка")
    @app_commands.describe(
        log_channel="Канал, куда бот кидает отчёты о мутов/варнов/банов",
        mute_role="Роль-заглушка для Legacy-мутa (если нет прав timeout)",
        driver="ID драйвера игры: discord_voice | rcon | ...",
        dm="Писать нарушителю в ЛС",
        dry_run="Тренировочный режим: пишем лог, ничего не меняем",
        feedback_hint="Строка подсказки под событием: свои слова или markdown-ссылка на тикеты",
    )
    async def modsettings(
        self,
        inter: discord.Interaction,
        log_channel: discord.TextChannel | None = None,
        mute_role: discord.Role | None = None,
        driver: str | None = None,
        dm: bool | None = None,
        dry_run: bool | None = None,
        feedback_hint: str | None = None,
    ) -> None:
        if driver and self.bot.registry.get(driver) is None:
            raise ModInputError(f"Драйвера «{driver}» нет. Есть: {', '.join(self.bot.registry.ids())}")
        patch: dict[str, object] = {}
        if log_channel:
            patch["log_channel_id"] = log_channel.id
        if mute_role:
            patch["mute_role_id"] = mute_role.id
        if driver:
            patch["driver"] = driver
        if dm is not None:
            patch["dm_target"] = dm
        if feedback_hint is not None:
            patch["feedback_hint"] = feedback_hint.strip()
        if dry_run is not None:
            # только этот сервер: глобальный флаг трогать нельзя — он выключил бы
            # модерацию сразу на всех серверах бота
            patch["dry_run"] = dry_run
        if not patch:
            raise ModInputError("Нечего сохранять — укажите хотя бы один параметр.")
        cfg = await self.bot.db.set_guild_config(inter.guild.id, patch)
        if log_channel:
            try:
                await log_channel.send(
                    "📋 Здесь бот будет отчитываться о мутах, варнах и банах. "
                    "Проверка канала — это сообщение можно удалить."
                )
            except discord.Forbidden:
                raise ModInputError("В этот канал я писать не могу — выдайте право Отправлять сообщения.") from None
        await inter.response.send_message(
            "✅ Сохранено: " + ", ".join(f"`{k}`" for k in patch) + f"\nТекущий конфиг: `{json.dumps({k: v for k, v in cfg.items() if v})[:900]}`",
            ephemeral=True,
        )

    # ------------------------------------------------------- /gamelink
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True, moderate_members=True)
    @app_commands.command(name="gamelink", description="Привязать ник/SteamID игры к участнику Discord")
    @app_commands.rename(target="цель", game_id="ник")
    async def gamelink(self, inter: discord.Interaction, target: str, game_id: str) -> None:
        member = await resolve_member(inter.guild, target)
        driver_id, _ = await self.bot.mod.resolve_driver(inter.guild.id)
        nick = game_id.strip()
        if not nick:
            raise ModInputError("Укажите ник игры: `/gamelink цель:@Steve ник:Steve`.")
        await self.bot.db.set_game_link(inter.guild.id, member.id, driver_id, nick, note="вручную")
        await inter.response.send_message(
            f"🔗 {member.mention} ↔ `{nick}` (драйвер `{driver_id}`)", ephemeral=True
        )

    async def _case(self, inter: discord.Interaction, case_id: int, lift: bool, reason: str | None) -> None:
        row = await self.bot.db.get_case(case_id)
        if row is None or row["guild_id"] != inter.guild.id:
            raise ModInputError(f"Записи №{case_id} на этом сервере нет.")
        if lift:
            if row["lifted_at"]:
                raise ModInputError("Уже снят.")
            member = inter.guild.get_member(int(row["user_id"]))
            if row["action"] == "warn":
                await self.bot.db.lift(case_id, inter.user.id)
                msg = "⚠️ Варн снят."
            elif member is None and row["action"] in ("ban", "bangame"):
                await self.bot.mod.unban(inter.user, int(row["user_id"]), reason or "Разбан через /case")
                msg = "🔓 Бан снят."
            elif member is None:
                raise ModInputError("Участник покинул сервер — снимайте наказание на его стороне.")
            elif row["action"] == "mutegame":
                await self.bot.mod.unmutegame(inter.user, case_id, reason or "Снято через /case")
            elif row["action"] == "bangame":
                await self.bot.mod.unbangame(inter.user, case_id, reason or "Снято через /case")
                msg = "✅ Мут в игре снят."
            else:
                await self.bot.mod.unmute(inter.user, member, reason or "Снято через /case", case_id=case_id)
                msg = "✅ Мут снят."
            await inter.response.send_message(msg, ephemeral=True)
            return
        embed = self._case_embed(row, json.loads(row["extra"] or "{}"))
        await inter.response.send_message(embed=embed, ephemeral=True)

    def _case_embed(self, row, extra: dict) -> discord.Embed:
        """Full card of one case — same visual language as the reports, more detail."""
        guild = self.bot.get_guild(int(row["guild_id"]))
        target = (guild.get_member(int(row["user_id"])) if guild else None) or next(
            (u for u in self.bot.users if u.id == int(row["user_id"])), None
        )
        if target is None:  # ушёл из сервера или наказан по нику игры
            nick = extra.get("game_id") if int(row["user_id"]) == 0 else None
            label = nick or f"id:{row['user_id']}"
            target = SimpleNamespace(display_name=label, id=int(row["user_id"]))  # type: ignore[assignment]
        embed = discord.Embed(
            title=f"{audit.REPORT_TITLE} · {row['action']} · №{row['id']}",
            colour=audit.color_for(row["action"]),
        )
        embed.add_field(
            name="\u200b",
            value=f"{'С игрока' if row['action'].startswith('un') else 'Игрок'} {audit.code(audit.name_of(target))}",
            inline=False,
        )
        if row["moderator_id"]:
            embed.add_field(name="КЕМ", value=f"<@{row['moderator_id']}>", inline=True)
        embed.add_field(name="ВЫДАН", value=timeutil.discord_relative(timeutil.from_iso(row["issued_at"])), inline=True)
        if row["lifted_at"]:
            embed.add_field(name="СНЯТ", value=timeutil.discord_relative(timeutil.from_iso(row["lifted_at"])), inline=True)
        embed.add_field(name="ПРИЧИНА", value=audit.code(row["reason"], limit=900), inline=False)
        if extra:
            body = "\n".join(
                f"{k}: {audit.code(v)}" for k, v in extra.items() if v not in (None, "", 0)
            )
            if body:
                embed.add_field(name="ДЕТАЛИ", value=body[:1000], inline=False)
        url = getattr(getattr(target, "display_avatar", None), "url", None)
        if url:
            embed.set_thumbnail(url=str(url))
        return embed


async def setup(bot: ModBot) -> None:
    await bot.add_cog(Records(bot))
