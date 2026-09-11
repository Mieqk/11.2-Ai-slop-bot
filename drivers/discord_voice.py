"""Built-in fallback driver: silence the player through Discord voice state.

Use this when in-game voice is carried by Discord (CS2, Dota 2, Rust, Minecraft
with Discord VoIP and most others). Server-deafen cannot be undone by the
member themselves, so it survives channel switches.
"""

from __future__ import annotations

import contextlib

import discord

from .base import BaseDriver, DriverError, registry


@registry.register
class DiscordVoiceDriver(BaseDriver):
    id = "discord_voice"
    title = "Голос через Discord (server deafen) — без доступа к игре"
    supports_voice = True

    async def resolve(self, guild, member, identifier):
        if member is None:
            raise DriverError("в Discord-войсе нельзя мутить по нику игры — укажите участника Discord")
        return str(member.id)

    async def mute(self, guild, target: str, seconds: int, reason: str) -> str:
        member = guild.get_member(int(target))
        if member is None:
            raise DriverError("участник не на сервере — некого отключать")
        try:
            await member.edit(deafen=True, reason=f"[mutegame] {reason}")
        except discord.Forbidden:
            raise DriverError(
                "нет права «Отключить участников» (Mute Members) или цель стоит выше бота"
            ) from None
        note = "Discord: включён server deafen"
        role_id = int(self.config.get("quarantine_role_id") or 0)
        if role_id:
            role = guild.get_role(role_id)
            if role and role not in member.roles:
                try:
                    await member.add_roles(role, reason=f"[mutegame] {reason}")
                    note += f" + роль «{role.name}»"
                except discord.Forbidden:
                    note += " (роль выдать не удалось — проверьте иерархию)"
        return note

    async def unmute(self, guild, target: str) -> str:
        try:
            member = guild.get_member(int(target))
        except ValueError:
            raise DriverError(f"«{target}» не выглядит как Discord ID — это чужой драйвер?") from None
        if member is None:
            return "участник не на сервере — запись снята только в базе"
        try:
            await member.edit(deafen=False, reason="[unmutegame]")
        except discord.Forbidden:
            raise DriverError("нет права Mute Members") from None
        role_id = int(self.config.get("quarantine_role_id") or 0)
        if role_id:
            role = guild.get_role(role_id)
            if role and role in member.roles:
                with contextlib.suppress(discord.Forbidden):
                    await member.remove_roles(role, reason="[unmutegame]")
        return "Discord: deafen снят"
