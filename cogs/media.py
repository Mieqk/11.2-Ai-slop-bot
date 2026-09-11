"""Медиа-карточки — «Фотография» из референса Котёнка.

Одна команда кладёт в канал карточку: `[заголовок]`, подпись-описание, крупная
картинка и цветная полоса. Вид тот же, что у правил (house style), только
акцент по умолчанию голубой.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # кругового импорта нет: cog'и грузятся строками
    from bot import ModBot

import discord
from discord import app_commands
from discord.ext import commands

from cogs.rules import parse_color
from core import style

DEFAULT_TITLE = "Фотография"
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp")


def photo_embed(title: str, caption: str | None, color: int) -> discord.Embed:
    return discord.Embed(title=style.head(title), description=caption or None, colour=color)


def looks_like_image(url: str) -> bool:
    clean = (url or "").split("?", 1)[0].split("#", 1)[0].lower()
    return clean.endswith(IMAGE_EXTENSIONS)


def parse_message_link(link: str) -> tuple[int, int]:
    """/channels/<guild>/<channel>/<message> → (channel_id, message_id).

    Возвращает нули, если ссылка не похожа на ссылку на сообщение: формат
    «dis.gd» и ссылки на тред с /@users тоже должны падать внятной подсказкой,
    а не ValueError'ом в середине edit.
    """
    parts = [p for p in (link or "").strip().split("/") if p]
    try:
        channel_i = parts.index("channels") + 2
    except ValueError:
        return 0, 0
    if len(parts) < channel_i + 2:
        return 0, 0
    channel_id, message_id = parts[channel_i], parts[channel_i + 1]
    if not (channel_id.isdigit() and message_id.isdigit()):
        return 0, 0
    return int(channel_id), int(message_id)


class Media(commands.GroupCog, group_name="photo"):
    """Медиа-посты: карточка с картинкой в house style."""

    def __init__(self, bot: ModBot):
        self.bot = bot

    @app_commands.command(name="post", description="Выложить карточку с картинкой в канал")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_messages=True)
    @app_commands.describe(
        заголовок="Текст в квадратных скобках (по умолчанию «Фотография»)",
        подпись="Описание под картинкой",
        картинка="Прикрепите изображение — тогда ссылка не нужна",
        ссылка="URL картинки, если не прикрепляете файлом",
        цвет="Полоса: cyan|amber|bordeaux|green|info или hex без #",
        канал="Куда постить (пусто — текущий канал)",
        закрепить="Закрепить сообщение после публикации",
    )
    async def post(
        self,
        inter: discord.Interaction,
        заголовок: app_commands.Range[str, 1, 120] = DEFAULT_TITLE,
        подпись: app_commands.Range[str, 0, 2000] | None = None,
        картинка: discord.Attachment | None = None,
        ссылка: str | None = None,
        цвет: str | None = None,
        канал: discord.TextChannel | None = None,
        закрепить: bool = False,
    ) -> None:
        url = картинка.url if картинка else (ссылка or "").strip()
        if not url:
            raise app_commands.AppCommandError("Прикрепите картинку или укажите `ссылка:`.")
        if not looks_like_image(url):
            raise app_commands.AppCommandError(
                "Ссылка не похожа на изображение: нужен .png/.jpg/.gif/.webp "
                "(или прикрепите файл напрямую)."
            )
        embed = photo_embed(заголовок, подпись, parse_color(цвет) if цвет else style.COLOR_CYAN)
        embed.set_image(url=url)
        target = канал or inter.channel
        try:
            msg = await target.send(embed=embed)
        except discord.Forbidden:
            raise app_commands.AppCommandError(f"Нет прав писать в {target.mention}.") from None
        if закрепить:
            try:
                await msg.pin(reason=f"медиа-пост от {inter.user.display_name}")
            except discord.Forbidden:
                await inter.response.send_message(
                    f"🖼 Карточка в {target.mention}, но закрепить не разрешили права.", ephemeral=True
                )
                return
        await inter.response.send_message(f"🖼 Карточка выложена в {target.mention}.", ephemeral=True)

    @app_commands.command(name="attach", description="Добавить картинку к карточке бота в этом канале")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_messages=True)
    @app_commands.describe(
        сообщение="Ссылка на сообщение бота (ПКМ → «Копировать ссылку»)",
        картинка="Прикреплённое изображение",
    )
    async def attach(
        self, inter: discord.Interaction, сообщение: str, картинка: discord.Attachment
    ) -> None:
        channel_id, message_id = parse_message_link(сообщение)
        if not (channel_id and message_id):
            raise app_commands.AppCommandError(
                "Нужна ссылка вида https://discord.com/channels/<сервер>/<канал>/<сообщение>."
            )
        if channel_id != inter.channel_id:
            raise app_commands.AppCommandError("Править карточку можно только в том же канале.")
        channel = inter.guild.get_channel(channel_id) or inter.channel
        if not hasattr(channel, "fetch_message"):
            raise app_commands.AppCommandError("Канал недоступен — обновите список каналов или проверьте права.")
        try:
            msg = await channel.fetch_message(message_id)
        except discord.NotFound:
            raise app_commands.AppCommandError("Сообщение не найдено (или удалено).") from None
        if msg.author.id != inter.client.user.id:
            raise app_commands.AppCommandError("Править можно только сообщения этого бота.")
        if not msg.embeds:
            raise app_commands.AppCommandError("В сообщении нет карточки — дополнить нечем.")
        if not looks_like_image(картинка.url):
            raise app_commands.AppCommandError("Прикрепите изображение (.png/.jpg/.gif/.webp).")
        embed = msg.embeds[0]
        embed.set_image(url=картинка.url)
        try:
            await msg.edit(embed=embed)
        except discord.Forbidden:
            raise app_commands.AppCommandError("Нет права «Управлять сообщениями» в этом канале.") from None
        await inter.response.send_message("🖼 Картинка добавлена в карточку.", ephemeral=True)


async def setup(bot: ModBot) -> None:
    await bot.add_cog(Media(bot))
