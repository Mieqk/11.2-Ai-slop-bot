"""Discord embed rendering for the audit channel and the DMs.

Layout idea (accent bar, one event line with the nick in `inline code`, a
compact table of details, avatar as thumbnail) is borrowed from the punish-bots
everyone has seen; every label, color and sentence here is our own — see
BRANDING below, it is the single place to rebrand the whole bot.
"""

from __future__ import annotations

import discord

#: ---- BRANDING -------------------------------------------------------------
#: everything a reader sees as "voice" of the bot lives in this block.
REPORT_TITLE = "ОТЧЁТ МОДЕРАЦИИ"
DM_TITLE = "НАКАЗАНИЕ НА СЕРВЕРЕ"

COLOR_SENTENCE = 0xE0762C  # terracotta — наказание выдано
COLOR_WARN = 0xF0B429  # amber — предупреждение (без ограничений)
COLOR_LIFTED = 0x4CAF7D  # green — что-то снято/истекло
COLOR_INFO = 0x7C87C4  # slate — просмотры журнала (/modlog, /history)

#: labels of the detail table — uppercase and short so exactly three fit a row
COL_MODERATOR = "КЕМ"
COL_TERM = "СРОК"
COL_CAUSE = "ПРИЧИНА"
COL_WARNS = "ВАРНОВ"
COL_NEXT = "ДАЛЬШЕ"

#: action -> (heading verb for the event line, does it restrict the player)
ACTIONS: dict[str, tuple[str, str, bool]] = {
    # key: (что случилось, заголовок строки события, ограничивает ли игрока)
    "mute": ("Мут выдан", "мут", True),
    "mutegame": ("Голос в игре отключён", "мут в игре", True),
    "bangame": ("Забанен на игровом сервере", "бан в игре", True),
    "unbangame": ("Разбанен на игровом сервере", "бан в игре", False),
    "warn": ("Предупреждение выдано", "варн", False),
    "ban": ("Доступ закрыт", "бан", True),
    "kick": ("Игрок выгнан с сервера", "кик", False),
    "unmute": ("Мут снят", "мут", False),
    "unmutegame": ("Голос в игре возвращён", "мут в игре", False),
    "unban": ("Доступ возвращён", "бан", False),
    "auto": ("Срок наказания истёк", "авто-снятие", False),
}

#: the advice line under the event, per kind of message
HINT_SENTENCE = "Не согласны с наказанием — обсудите его в канале обратной связи или заведите тикет."
HINT_WARN = "Варн сам по себе ничего не блокирует, но включает авто-наказание при накоплении."
HINT_LIFTED = "Ограничения сняты, сервером можно пользоваться как раньше."
#: used when a server set no custom text of its own
HINT = HINT_SENTENCE
#: what КЕМ shows when nobody pressed the button (auto-expiry, warn ladder)
AUTO_AUTHOR = "серверная автоматика"


def color_for(action: str) -> int:
    """Accent colour for an action, including the ones only read back."""
    if action.startswith("un") or action in ("auto", "clearwarns"):
        return COLOR_LIFTED
    if action == "warn":
        return COLOR_WARN
    if action in ACTIONS:
        return COLOR_SENTENCE
    return COLOR_INFO


def headline(action: str) -> str:
    """"Мут выдан" / "Мут снят" — the bold line on top of the card."""
    return ACTIONS.get(action, (action, action, True))[0]


def code(value: object, limit: int = 600) -> str:
    r"""One-line `inline code` value.

    No backslash-escaping here on purpose: Discord shows the inside of a code
    span literally, so `Arab\_Sheih` would leak the backslash to the player.
    """
    text = "" if value in (None, "") else str(value)
    text = " ".join(text.split()).replace("`", "'")[:limit]
    return f"`{text or '—'}`"


def name_of(user) -> str:
    for attr in ("display_name", "global_name", "name"):
        value = getattr(user, attr, None)
        if value:
            return str(value)
    # a bare discord.Object (player who left the server) would otherwise render
    # as "<discord.object.Object object at 0x…>" in the report
    user_id = getattr(user, "id", None)
    return f"id:{user_id}" if user_id else str(user)


def _mention(user) -> str:
    return code(name_of(user)) if user is not None else code(AUTO_AUTHOR)


def _avatar(embed: discord.Embed, target) -> None:
    url = getattr(getattr(target, "display_avatar", None), "url", None)
    if url:
        embed.set_thumbnail(url=str(url))


def _dm_head(stem: str, guild_name: str, target) -> str:
    """"Мут выдан на `PIVO#1` · `Arab_Sheih`" for the DM cards."""
    where = f" на {code(guild_name)}" if guild_name else ""
    who = f" · {code(name_of(target))}" if target is not None else ""
    return f"{stem}{where}{who}"


def case_embed(
    action: str,
    *,
    target,
    moderator,
    reason: str | None,
    duration: str | None = None,
    note: str | None = None,
    extra_fields: dict[str, str] | None = None,
    hint: str | None = None,
) -> discord.Embed:
    """Report one case into the audit channel."""
    _, short, restricts = ACTIONS.get(action, (action, action, True))
    embed = discord.Embed(title=REPORT_TITLE, colour=color_for(action))

    subject = getattr(target, "mention", None) or code(name_of(target))
    embed.add_field(name="\u200b", value=f"{headline(action)} · {subject}", inline=False)
    chosen = hint or (HINT_SENTENCE if restricts else (HINT_WARN if action == "warn" else HINT_LIFTED))
    if chosen:
        embed.add_field(name=f"**{short}**", value=chosen[:1024], inline=False)

    embed.add_field(name=COL_MODERATOR, value=_mention(moderator), inline=True)
    if duration:
        embed.add_field(name=COL_TERM, value=code(duration), inline=True)
    embed.add_field(name=COL_CAUSE, value=code(reason), inline=True)
    for field, value in (extra_fields or {}).items():
        embed.add_field(name=field, value=code(value), inline=True)

    if note:
        embed.set_footer(text=f"применено: {' '.join(str(note).split())[:400]}")
    _avatar(embed, target)
    return embed


def warning_embed(
    warn_no: int,
    reason: str,
    moderator,
    next_threshold: int | None,
    *,
    guild_name: str = "",
    target=None,
    total_warns: int | None = None,
    hint: str | None = None,
) -> discord.Embed:
    """The DM a player gets for a warn."""
    embed = discord.Embed(title=DM_TITLE, colour=COLOR_WARN)
    embed.add_field(name="\u200b", value=_dm_head(f"Вам предупреждение №{warn_no}", guild_name, target), inline=False)
    embed.add_field(name="**варн**", value=(hint or HINT_WARN)[:1024], inline=False)
    embed.add_field(name=COL_MODERATOR, value=_mention(moderator), inline=True)
    embed.add_field(name=COL_CAUSE, value=code(reason), inline=True)
    if total_warns is not None:
        embed.add_field(name=COL_WARNS, value=code(f"{total_warns} активных"), inline=True)
        if next_threshold:
            embed.add_field(name=COL_NEXT, value=code(f"ещё {next_threshold - total_warns}"), inline=True)
    if target is not None:
        _avatar(embed, target)
    return embed


def punishment_embed(
    action: str,
    duration: str | None,
    reason: str,
    *,
    guild_name: str = "",
    target=None,
    hint: str | None = None,
) -> discord.Embed:
    """The DM a player gets for a mute / game mute / ban."""
    _, short, _ = ACTIONS.get(action, (action, action, True))
    embed = discord.Embed(title=DM_TITLE, colour=color_for(action))
    embed.add_field(name="\u200b", value=_dm_head(headline(action), guild_name, target), inline=False)
    embed.add_field(name=f"**{short}**", value=(hint or HINT_SENTENCE)[:1024], inline=False)
    if duration:
        embed.add_field(name=COL_TERM, value=code(duration), inline=True)
    embed.add_field(name=COL_CAUSE, value=code(reason), inline=True)
    if target is not None:
        _avatar(embed, target)
    return embed
