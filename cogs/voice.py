"""Голосовые комнаты по пунктам 2.1 / 2.2 из референсов.

· Канал-триггер («Погладить камушек»): зашёл — получил личную комнату и стал её
  владельцем (мьют/глушение/перемещение участников). Вышел последним — комната
  исчезает.
· Публичные комнаты: владелец тот, кто первым занял канал; когда он уходит —
  права снимаются и переходят к следующему оставшемуся.

Решения вынесены в чистые функции (plan_join, plan_leave, room_overwrites), cog
только исполняет — так логика проверяется тестами без голосового соединения.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # кругового импорта нет: cog'и грузятся строками
    from bot import ModBot

import contextlib
import logging
from dataclasses import dataclass
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks

log = logging.getLogger("modbot.voice")

ROOM_PREFIX = "🔒"
OWNER_PERMS = {
    "connect": True,
    "speak": True,
    "stream": True,
    # без этого «владелец» не может впустить к себя гостей: права на редактирование
    # оверрайтов канала — часть идеи личной комнаты, а не привилегия модерации
    "manage_channels": True,
    "mute_members": True,
    "deafen_members": True,
    "move_members": True,
    "priority_speaker": True,
    "use_voice_activation": True,
}
CLEAR_PERMS = {key: None for key in OWNER_PERMS}
FRESH_GRACE_S = 60  # первую минуту комнату не удаляем: перезаход/переезд
MAX_ROOM_NAME = 80


@dataclass
class JoinPlan:
    create_room: bool = False
    grant_ownership: bool = False
    room_name: str = ""


@dataclass
class LeavePlan:
    delete_room: bool = False
    clear_owner: bool = False
    transfer_to: int | None = None


def room_name_for(member) -> str:
    name = getattr(member, "display_name", None) or getattr(member, "name", None) or str(member)
    return f"{ROOM_PREFIX} {' '.join(str(name).split())}"[:MAX_ROOM_NAME]


def is_temp_room(channel) -> bool:
    return str(getattr(channel, "name", "")).startswith(ROOM_PREFIX)


def other_members(channel, *, exclude_id: int | None = None) -> list:
    """Участники комнаты: без ботов и без того, про кого спрашиваем."""
    return [m for m in getattr(channel, "members", []) if not m.bot and (exclude_id is None or m.id != exclude_id)]


def plan_join(member, channel, *, trigger_id: int, private_cat_id: int, public_cat_id: int) -> JoinPlan:
    """Создавать ли комнату и кому отдавать владение."""
    if trigger_id and channel.id == trigger_id:
        if not private_cat_id:
            return JoinPlan()  # триггер настроен, а куда создавать — нет: не тычем в None
        return JoinPlan(create_room=True, grant_ownership=True, room_name=room_name_for(member))
    if public_cat_id and getattr(channel.category, "id", None) == public_cat_id:
        return JoinPlan(grant_ownership=not other_members(channel, exclude_id=member.id))
    return JoinPlan()


def plan_leave(member, channel, *, owner_id: int | None, public_cat_id: int) -> LeavePlan:
    """Разбор ухода: снос пустой личной комнаты, передача владения в публичной."""
    if channel is None:
        return LeavePlan()
    remaining = other_members(channel, exclude_id=member.id)
    if is_temp_room(channel) and not remaining:
        return LeavePlan(delete_room=True)
    if owner_id == member.id:
        return LeavePlan(clear_owner=True, transfer_to=remaining[0].id if remaining else None)
    if public_cat_id and getattr(channel.category, "id", None) == public_cat_id and not remaining:
        return LeavePlan(clear_owner=True)
    return LeavePlan()


def is_stale_room(channel, now, *, grace_s: int = FRESH_GRACE_S) -> bool:
    """Временная комната без людей, простоявшая дольше grace-паузы."""
    if not is_temp_room(channel) or other_members(channel):
        return False
    created = getattr(channel, "created_at", None)
    return created is None or created <= now - timedelta(seconds=grace_s)


def room_overwrites(member) -> dict:
    """Личная комната: видна и доступна только владельцу (остальных впускает он)."""
    return {
        member: discord.PermissionOverwrite(**OWNER_PERMS),
        member.guild.default_role: discord.PermissionOverwrite(connect=False, view_channel=False),
    }


class Voice(commands.GroupCog, group_name="voice"):
    """Личные и публичные войс-комнаты."""

    def __init__(self, bot: ModBot):
        self.bot = bot
        self.owners: dict[int, int] = {}  # channel_id -> id текущего владельца
        self.gc_loop.start()

    def cog_unload(self):  # без аннотации: базовый класс допускает coroutine
        self.gc_loop.cancel()

    # ------------------------------------------------------------------ config
    @app_commands.command(name="config", description="Канал-триггер и категории комнат")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(
        канал_триггер="Войдя сюда, участник получает личную комнату",
        категория_комнат="Где создавать личные комнаты",
        категория_публичных="Публичные: кто первый занял, тот временный владелец",
        выключить="Отключить модуль на сервере",
    )
    async def config(
        self,
        inter: discord.Interaction,
        канал_триггер: discord.VoiceChannel | None = None,
        категория_комнат: discord.CategoryChannel | None = None,
        категория_публичных: discord.CategoryChannel | None = None,
        выключить: bool = False,
    ) -> None:
        if выключить:
            await self.bot.db.set_guild_config(
                inter.guild.id,
                {"voice_create_channel_id": 0, "voice_private_category_id": 0, "voice_public_category_id": 0},
            )
            self.owners.clear()
            await inter.response.send_message("Модуль комнат выключен.", ephemeral=True)
            return
        if not канал_триггер or not категория_комнат:
            raise app_commands.AppCommandError("Нужны `канал_триггер` и `категория_комнат` (или `выключить:true`).")
        if канал_триггер.category and канал_триггер.category.id == категория_комнат.id:
            raise app_commands.AppCommandError(
                "Триггер не должен лежать внутри категории, куда создаются комнаты: вход в них же и плодит комнаты."
            )
        await self.bot.db.set_guild_config(
            inter.guild.id,
            {
                "voice_create_channel_id": канал_триггер.id,
                "voice_private_category_id": категория_комнат.id,
                "voice_public_category_id": категория_публичных.id if категория_публичных else 0,
            },
        )
        public = f", публичные — «{категория_публичных.name}»" if категория_публичных else ""
        await inter.response.send_message(
            f"✅ Комнаты: триггер {канал_триггер.mention}, личные — в «{категория_комнат.name}»{public}.",
            ephemeral=True,
        )

    @app_commands.command(name="rooms", description="Сколько комнат и кто в них владелец")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def rooms(self, inter: discord.Interaction) -> None:
        cfg = await self.bot.db.get_guild_config(inter.guild.id)
        category = inter.guild.get_channel(int(cfg.get("voice_private_category_id") or 0))
        if category is None or not hasattr(category, "channels"):
            raise app_commands.AppCommandError("Категория комнат не задана или удалена — `/voice config`.")
        found = [c for c in category.channels if is_temp_room(c)]
        if not found:
            await inter.response.send_message("Комнат пока нет.", ephemeral=True)
            return
        body = "\n".join(
            f"· {c.name} — {len(other_members(c))} чел."
            + (f", владелец <@{self.owners[c.id]}>" if c.id in self.owners else "")
            for c in found[:20]
        )
        await inter.response.send_message(f"Комнаты ({len(found)}):\n{body[:1800]}", ephemeral=True)

    @app_commands.command(name="take", description="Забрать комнату себе, если владелец вышел")
    @app_commands.guild_only()
    async def take(self, inter: discord.Interaction) -> None:
        voice_state = inter.user.voice
        channel = voice_state.channel if voice_state else None
        if channel is None:
            raise app_commands.AppCommandError("Сядьте в голосовой канал, потом `/voice take`.")
        owner_id = self.owners.get(channel.id)
        if owner_id and owner_id != inter.user.id and channel.get_member(owner_id) is not None:
            raise app_commands.AppCommandError("В комнате есть владелец — дождитесь, пока он выйдет.")
        await self._grant_owner(inter.user, channel)
        await inter.response.send_message("Комната теперь под вашим управлением.", ephemeral=True)

    # ------------------------------------------------------------------ events
    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after) -> None:
        cfg = await self.bot.db.get_guild_config(member.guild.id)
        trigger_id = int(cfg.get("voice_create_channel_id") or 0)
        private_cat_id = int(cfg.get("voice_private_category_id") or 0)
        public_cat_id = int(cfg.get("voice_public_category_id") or 0)
        if not (trigger_id or private_cat_id or public_cat_id):
            return
        joined, left = after.channel, before.channel
        if joined is not None and (left is None or joined.id != left.id):
            join = plan_join(member, joined, trigger_id=trigger_id,
                             private_cat_id=private_cat_id, public_cat_id=public_cat_id)
            if join.create_room:
                await self._create_room(member, private_cat_id, join.room_name)
                return
            if join.grant_ownership:
                await self._grant_owner(member, joined)
        if left is not None and (joined is None or joined.id != left.id):
            leave = plan_leave(member, left, owner_id=self.owners.get(left.id), public_cat_id=public_cat_id)
            await self._apply_leave(member, left, leave)

    # ------------------------------------------------------------- исполнители
    async def _create_room(self, member, category_id: int, name: str) -> None:
        category = member.guild.get_channel(category_id)
        if category is None:
            log.warning("гильдия %s: категория комнат %s не найдена", member.guild.id, category_id)
            return
        try:
            room = await member.guild.create_voice_channel(
                name, category=category, reason=f"личная комната {member}", overwrites=room_overwrites(member)
            )
        except discord.Forbidden:
            await _safe_send(member, f"Не могу создать комнату: нет прав в категории «{category.name}».")
            return
        except discord.HTTPException:
            log.exception("не создал комнату для %s", member)
            return
        self.owners[room.id] = member.id
        try:
            await member.move_to(room)
        except discord.HTTPException:
            # участник уже вышел — пустую комнату приберёт gc_loop
            log.debug("не перенёс %s в «%s»", member, room.name)

    async def _grant_owner(self, member, channel) -> None:
        self.owners[channel.id] = member.id
        try:
            await channel.set_permissions(member, reason="владелец комнаты", **OWNER_PERMS)
        except discord.Forbidden:
            log.debug("нет прав выдавать владельческие права в «%s»", channel.name)

    async def _apply_leave(self, member, channel, plan: LeavePlan) -> None:
        if plan.delete_room:
            self.owners.pop(channel.id, None)
            try:
                await channel.delete(reason="личная комната опустела")
            except discord.NotFound:
                pass
            except discord.Forbidden:
                log.warning("не смог удалить комнату «%s»", channel.name)
            return
        if plan.clear_owner:
            self.owners.pop(channel.id, None)
            with contextlib.suppress(discord.Forbidden):
                await channel.set_permissions(member, reason="владелец вышел", **CLEAR_PERMS)
            if plan.transfer_to:
                successor = channel.guild.get_member(plan.transfer_to)
                if successor is not None:
                    await self._grant_owner(successor, channel)

    # ---------------------------------------------------------------------- gc
    @tasks.loop(minutes=1)
    async def gc_loop(self) -> None:
        """Сносит опустевшие временные комнаты, свежие — не раньше grace-паузы."""
        for guild in self.bot.guilds:
            cfg = await self.bot.db.get_guild_config(guild.id)
            category = guild.get_channel(int(cfg.get("voice_private_category_id") or 0))
            if category is None or not hasattr(category, "channels"):
                continue  # id в конфиге устарел: категорию удалили
            for channel in list(category.channels):
                if not is_stale_room(channel, discord.utils.utcnow()):
                    continue
                try:
                    await channel.delete(reason="временная комната пуста")
                except discord.HTTPException:
                    continue
                self.owners.pop(channel.id, None)

    @gc_loop.before_loop
    async def wait_ready(self) -> None:
        await self.bot.wait_until_ready()


async def _safe_send(member, text: str) -> None:
    try:
        await member.send(text)
    except discord.HTTPException:
        log.debug("ЛС закрыт для %s", member)


async def setup(bot: ModBot) -> None:
    await bot.add_cog(Voice(bot))
