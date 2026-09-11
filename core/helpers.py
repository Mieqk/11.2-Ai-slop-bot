"""Shared helpers for the moderation cogs."""

from __future__ import annotations

import discord
from discord import app_commands

DURATION_CHOICES = [
    app_commands.Choice(name=name, value=name)
    for name in ("10m", "30m", "1h", "3h", "6h", "12h", "1d", "3d", "7d", "14d", "30d", "perm")
]


class ModInputError(app_commands.AppCommandError):
    """Expected, moderator-facing failure -> shown as a toast, no traceback."""


async def resolve_member(guild: discord.Guild, target: str) -> discord.Member:
    """Accept a mention, a raw ID, or an exact display/username."""
    raw = target.strip()
    if raw.startswith("<@") and raw.endswith(">"):
        raw = raw[2:-1].lstrip("!")
    if raw.isdigit():
        member = guild.get_member(int(raw))
        if member is None:
            raise ModInputError("Участника с таким ID нет на сервере.")
        return member
    lowered = raw.lower()
    # `discord.utils.get` takes keyword attrs only — a positional predicate is a
    # TypeError, so match by hand.
    member = next(
        (
            m
            for m in guild.members
            if m.display_name.lower() == lowered or m.name.lower() == lowered
        ),
        None,
    )

    if member is None:
        raise ModInputError(
            f"Не нашёл «{raw}». Упомяните участника (@name) или введите его числовой ID."
        )
    return member


async def guard(interaction: discord.Interaction, member: discord.Member) -> None:
    """Refuse to punish self, the bot, or someone at/above the bot's role."""
    me = interaction.guild.me
    if member.id == interaction.user.id:
        raise ModInputError("Нельзя наказывать самого себя.")
    if member.id == me.id:
        raise ModInputError("Нельзя наказывать меня.")
    # Member has no is_privileged(); "protected staff" means admin or a role at
    # or above ours, which Discord would refuse to let us edit anyway.
    protected = member.guild_permissions.administrator or member.top_role >= me.top_role
    if protected:
        raise ModInputError(
            f"У **{member}** роль не ниже моей. Поднимите мою роль в иерархии выше цели."
        )


async def find_case(db, guild_id: int, case_id: int, *, actions: tuple[str, ...] | None = None):
    case = await db.get_case(case_id)
    if case is None or case["guild_id"] != guild_id:
        raise ModInputError(f"Записи №{case_id} на этом сервере нет.")
    if actions and case["action"] not in actions:
        raise ModInputError(
            f"Запись №{case_id} — это «{case['action']}», а не {' или '.join(actions)}."
        )
    return case


async def case_member(guild: discord.Guild, case) -> discord.Member:
    member = guild.get_member(int(case["user_id"]))
    if member is None:
        raise ModInputError("Участник больше не на сервере — снимайте наказание на его стороне (или /unbangame номер).")
    return member
