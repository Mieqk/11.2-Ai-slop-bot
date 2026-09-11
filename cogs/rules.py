"""Интерактивные правила сервера — как в референсах, но видно только выбравшему.

В канале лежит карточка `[ПРАВИЛА СЕРВЕРА]` с кнопками разделов. Нажатие на
кнопку открывает раздел **ephemeral-сообщением** — его видит только тот, кто
нажал, и в канале ничего не меняется. (Select-меню для этого не годится:
Discord дописывает в общее сообщение «Выбранные варианты: …», и выбор виден
всем.)

Внутри открытого раздела: ◀ ▶ листают страницы, `≡` возвращает список разделов
(в то же ephemeral-сообщение), а внизу может быть кнопка-переход в канал
настройки через `/rules config кнопка_канал:#…` — как «Все прочитали? …» в
референсе.

Состояние (раздел, страница) в памяти вью не хранится: раздел подписан в
заголовке карточки, страница — в футере. Поэтому открытое меню работает и после
перезапуска бота (шаблон вью с фиксированными custom_id зарегистрирован в
setup()).
"""

from __future__ import annotations

import json
import logging
import pathlib

import discord
from discord import app_commands
from discord.ext import commands

from core import rules_repo, style

log = logging.getLogger("modbot.rules")

MENU_TITLE = "ПРАВИЛА СЕРВЕРА"
OPEN_PREFIX = "rules:open"
BACK_ID = "rules:back"
PREV_ID = "rules:prev"
NEXT_ID = "rules:next"
PAGE_MARKER = "· страница"
ONLY_YOU = " · видите только вы"
#: кнопок разделов в меню: у сообщения лимит 25 компонентов, ещё нужны ◀ ▶ ≡
MAX_SECTION_BUTTONS = 20


def _word(count: int, one: str, few: str, many: str) -> str:
    mod10, mod100 = count % 10, count % 100
    if mod100 in (11, 12, 13, 14):
        return many
    if mod10 == 1:
        return one
    if 2 <= mod10 <= 4:
        return few
    return many


def _pages_word(count: int) -> str:
    return f"{count} {_word(count, 'страница', 'страницы', 'страниц')}"


def open_id(index: int) -> str:
    return f"{OPEN_PREFIX}:{index}"


def open_index(custom_id: str) -> int:
    """Каким разделом была кнопка; -1 если id не наша кнопка раздела."""
    if not custom_id.startswith(f"{OPEN_PREFIX}:"):
        return -1
    tail = custom_id.rsplit(":", 1)[1]  # id вида rules:open:4 — правый сегмент
    return int(tail) if tail.isdigit() else -1


def _button(label: str, custom_id: str, button_style: discord.ButtonStyle, *, disabled: bool = False,
            emoji: str | None = None) -> discord.ui.Button:
    btn: discord.ui.Button = discord.ui.Button(
        label=label[:80], emoji=emoji, style=button_style, custom_id=custom_id, disabled=disabled
    )
    return btn


class RulesView(discord.ui.View):
    """Кнопки меню и листание.

    Один класс шлёт и публичное меню, и ephemeral-карточку раздела; шаблон со
    всеми 20 кнопками регистрируется для dispatch после рестарта."""

    def __init__(
        self,
        categories: list[dict],
        *,
        section: str | None = None,
        page: int = 0,
        guild_id: int | None = None,
        chat_channel_id: int | None = None,
    ):
        super().__init__(timeout=None)
        self.categories = categories
        if section is None:  # меню: по кнопке на раздел
            for index, cat in enumerate(categories[:MAX_SECTION_BUTTONS]):
                btn = _button(cat["title"], open_id(index), discord.ButtonStyle.secondary, emoji=cat["emoji"] or None)
                btn.callback = self._make_open(index)  # type: ignore[assignment,method-assign]
                self.add_item(btn)
            return
        back = _button("≡ к списку", BACK_ID, discord.ButtonStyle.secondary)
        back.callback = self._on_back  # type: ignore[assignment,method-assign]
        prev = _button("◀", PREV_ID, discord.ButtonStyle.primary, disabled=page <= 0)
        prev.callback = self._on_prev  # type: ignore[assignment,method-assign]
        total = len(self.pages_of(section))
        nxt = _button("▶", NEXT_ID, discord.ButtonStyle.primary, disabled=page + 1 >= total)
        nxt.callback = self._on_next  # type: ignore[assignment,method-assign]
        for btn in (back, prev, nxt):
            self.add_item(btn)
        if chat_channel_id and guild_id:  # кнопка-переход, как в референсе
            self.add_item(discord.ui.Button(
                label="Все прочитали? Перейти в канал", style=discord.ButtonStyle.link,
                url=f"https://discord.com/channels/{guild_id}/{chat_channel_id}",
            ))

    @classmethod
    def dispatch_template(cls) -> RulesView:
        """Вью-шаблон для `bot.add_view`: ВСЕ custom_id, что бывают на карточках.

        Обычное `RulesView([])` даёт только меню без кнопок — и после рестарта
        диспетчер не нашёл бы обработчиков для ◀ ▶ ≡ в открытых ephemeral-карточках.
        """
        placeholders = [
            {"slug": f"__slot{i}", "emoji": "", "title": f"раздел {i + 1}", "color": 0, "pages": [""]}
            for i in range(MAX_SECTION_BUTTONS)
        ]
        view = cls(placeholders)
        for custom_id, handler in ((BACK_ID, view._on_back), (PREV_ID, view._on_prev), (NEXT_ID, view._on_next)):
            btn = _button("·", custom_id, discord.ButtonStyle.secondary)
            btn.callback = handler  # type: ignore[assignment,method-assign]
            view.add_item(btn)
        return view

    def _make_open(self, index: int):
        async def handler(inter: discord.Interaction) -> None:
            await self._open(inter, index)
        return handler

    def pages_of(self, slug: str | None) -> list[str]:
        section = next((c for c in self.categories if c["slug"] == slug), None)
        return section["pages"] if section else [""]

    # -------------------------------------------------------------- handlers
    async def _open(self, inter: discord.Interaction, index: int) -> None:
        categories = await inter.client.rules.sections(inter.guild.id)
        if index >= len(categories):
            # раздел успели удалить, пока меню висело в канале
            await inter.response.send_message("Этот раздел удалён — меню обновлено: `/rules refresh`.", ephemeral=True)
            return
        cfg = await _hint_cfg(inter)
        embed, _ = build_embed(categories, cfg, categories[index]["slug"], 0, private=True)
        await inter.response.send_message(
            embed=embed,
            view=RulesView(categories, section=categories[index]["slug"], page=0,
                           guild_id=inter.guild.id, chat_channel_id=cfg.get("chat_channel_id")),
            ephemeral=True,  # только тот, кто нажал
        )

    async def _on_back(self, inter: discord.Interaction) -> None:
        categories = await inter.client.rules.sections(inter.guild.id)
        embed, _ = build_embed(categories, await _hint_cfg(inter), None, 0)
        await inter.response.edit_message(embed=embed, view=RulesView(categories))

    async def _on_prev(self, inter: discord.Interaction) -> None:
        await self._step(inter, -1)

    async def _on_next(self, inter: discord.Interaction) -> None:
        await self._step(inter, 1)

    async def _step(self, inter: discord.Interaction, delta: int) -> None:
        categories = await inter.client.rules.sections(inter.guild.id)
        slug = slug_from_title(categories, inter.message)
        section = next((c for c in categories if c["slug"] == slug), None) if slug else None
        if section is None:  # раздел успели удалить
            embed, _ = build_embed(categories, await _hint_cfg(inter), None, 0)
            await inter.response.edit_message(embed=embed, view=RulesView(categories))
            return
        total = len(section["pages"])
        page = min(max(page_of(inter.message) + delta, 0), max(total - 1, 0))
        cfg = await _hint_cfg(inter)
        embed, _ = build_embed(categories, cfg, slug, page, private=True)
        await inter.response.edit_message(
            embed=embed,
            view=RulesView(categories, section=slug, page=page, guild_id=inter.guild.id,
                           chat_channel_id=cfg.get("chat_channel_id")),
        )


async def _hint_cfg(inter: discord.Interaction) -> dict:
    keys = ("complaint_channel_id", "staff_role_id", "chat_channel_id")
    cfg = await inter.client.db.get_guild_config(inter.guild.id)
    return {k: cfg.get(k) for k in keys}


def slug_from_title(categories: list[dict], message: discord.Message | None) -> str | None:
    """Раздел подписан в заголовке карточки — якорь переживает рестарт бота."""
    if message is None or not message.embeds:
        return None
    title = message.embeds[0].title or ""
    if title == style.head(MENU_TITLE):
        return None
    for c in categories:
        if style.head(f"{c['emoji']} {c['title']}".strip()) == title or style.head(c["title"]) == title:
            return c["slug"]
    return None


def page_of(message: discord.Message | None) -> int:
    """Номер страницы — из футера карточки."""
    footer = (message.embeds[0].footer.text or "") if (message and message.embeds) else ""
    if PAGE_MARKER not in footer:
        return 0
    head = footer.split(PAGE_MARKER, 1)[1].strip()
    try:
        return max(int(head.split("/")[0].strip()) - 1, 0)
    except ValueError:
        return 0


def hint_line(cfg: dict) -> str | None:
    """Строчка внизу карточки: куда писать, если не согласен с наказанием."""
    parts = []
    if cfg.get("complaint_channel_id"):
        parts.append(f"есть вопрос по решению модерации — <#{cfg['complaint_channel_id']}>")
    if cfg.get("staff_role_id"):
        parts.append("поможет команда <@&" + str(cfg["staff_role_id"]) + ">")
    return style.bullet(" · ".join(parts)) if parts else None


def build_embed(categories: list[dict], cfg: dict, slug: str | None, page: int, *, private: bool = False):
    """(embed, раздел|None). Без раздела — карточка-меню."""
    section = next((c for c in categories if c["slug"] == slug), None) if slug else None
    if section is None:
        embed = discord.Embed(title=style.head(MENU_TITLE), colour=style.COLOR_AMBER)
        if not categories:
            embed.description = "Разделы ещё не добавлены. Заполните их через `/rules add` или `/rules import`."
        else:
            lines = [style.bullet(f"{c['emoji'] or '📕'} **{c['title']}** — {_pages_word(len(c['pages']))}")
                     for c in categories[:MAX_SECTION_BUTTONS]]
            if len(categories) > MAX_SECTION_BUTTONS:
                lines.append(style.bullet(f"показаны первые {MAX_SECTION_BUTTONS} из {len(categories)}"))
            lines.append("")
            lines.append(style.bullet("нажмите кнопку раздела — текст откроется только вам"))
            embed.description = "\n".join(lines)
        return embed, None

    total = len(section["pages"]) or 1
    page = min(max(page, 0), total - 1)
    body = section["pages"][page][:4000] if section["pages"] else ""
    extra = hint_line(cfg)
    if extra:
        body = f"{body}\n\n{extra}" if body else extra
    embed = discord.Embed(
        title=style.head(f"{section['emoji']} {section['title']}".strip()),
        description=body or "…",
        colour=section["color"] or style.COLOR_AMBER,
    )
    footer = f"{PAGE_MARKER} {page + 1}/{total}"
    embed.set_footer(text=footer + (ONLY_YOU if private else ""))
    return embed, section


class Rules(commands.GroupCog, group_name="rules"):
    """Правила: кнопки разделов в канале, ephemeral-просмотр, заполнение."""

    def __init__(self, bot):
        self.bot = bot
        self.rules: rules_repo.RulesRepo = bot.rules
        self._seed: list[dict] = []

    async def cog_load(self) -> None:
        """Зараз и читаем seed-файл: `/rules publish` на пустом сервере заполнит меню."""
        path = self.bot.cfg.get("rules_seed_file") or "rules.example.json"
        file = pathlib.Path(path)
        if not file.is_file():
            return
        try:
            self._seed = parse_import(file.read_text(encoding="utf-8"))
        except (ValueError, OSError, UnicodeDecodeError) as exc:
            log.warning("seed-файл %s не разбирается: %s", path, exc)
            return
        log.info("заготовок правил загружено: %s (%s)", len(self._seed), path)

    @app_commands.command(name="publish", description="Выложить или обновить меню правил в канале")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(канал="Где разместить меню (пусто — канал из /rules config)")
    async def publish(self, inter: discord.Interaction, канал: discord.TextChannel | None = None) -> None:
        cfg = await self.bot.db.get_guild_config(inter.guild.id)
        target = канал or inter.guild.get_channel(int(cfg.get("rules_channel_id") or 0))
        if target is None or not hasattr(target, "send"):
            raise app_commands.AppCommandError("Укажите канал: `/rules publish канал:#…` или задайте его в `/rules config`.")
        categories = await self.rules.sections(inter.guild.id)
        if not categories:  # сервер новый: берём заготовки из файла, а не пустое меню
            for item in self._seed:
                await self.rules.upsert(inter.guild.id, item["title"], item["pages"],
                                        emoji=item["emoji"], color=parse_color(item["color"]))
            categories = await self.rules.sections(inter.guild.id)
        embed, _ = build_embed(categories, cfg, None, 0)
        view = RulesView(categories)
        # повторный publish правит существующую карточку, а не плодит вторую
        old_id = int(cfg.get("rules_message_id") or 0)
        old_channel = inter.guild.get_channel(int(cfg.get("rules_channel_id") or 0))
        msg = None
        if old_id and old_channel is not None and old_channel.id == target.id and hasattr(old_channel, "fetch_message"):
            try:
                msg = await old_channel.fetch_message(old_id)
                await msg.edit(embed=embed, view=view)
            except discord.NotFound:
                msg = None
            except discord.Forbidden:
                raise app_commands.AppCommandError(
                    f"Нет прав редактировать карточку в {old_channel.mention} — нужно «Управлять сообщениями»."
                ) from None
        if msg is None:
            try:
                msg = await target.send(embed=embed, view=view)
            except discord.Forbidden:
                raise app_commands.AppCommandError(f"Нет прав писать в {target.mention}.") from None
        await self.bot.db.set_guild_config(inter.guild.id, {"rules_channel_id": target.id, "rules_message_id": msg.id})
        await inter.response.send_message(
            f"Меню правил в {target.mention} — {len(categories)} раздел(ов), открываются только нажавшему.",
            ephemeral=True,
        )

    @app_commands.command(name="add", description="Добавить или заменить раздел правил")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(
        название="Например: «Правила голосовых каналов»",
        текст="Пустая строка между абзацами — новый блок; длинное само разобьётся на страницы",
        эмодзи="Эмодзи раздела (📕, 🔊, ☕) — используется и как эмодзи кнопки",
        цвет="Полоса: amber|bordeaux|cyan|green|info или hex",
    )
    async def add(
        self,
        inter: discord.Interaction,
        название: app_commands.Range[str, 2, 100],
        текст: app_commands.Range[str, 1, 8000],
        эмодзи: str | None = None,
        цвет: str | None = None,
    ) -> None:
        slug = await self.rules.upsert(
            inter.guild.id, название, текст, emoji=(эмодзи or "").strip(), color=parse_color(цвет)
        )
        section = await self.rules.get(inter.guild.id, slug)
        pages = len(section["pages"]) if section else 1
        if len(await self.rules.sections(inter.guild.id)) > MAX_SECTION_BUTTONS:
            note = f" В меню покажутся первые {MAX_SECTION_BUTTONS} разделов."
        else:
            note = ""
        await inter.response.send_message(
            f"Раздел `{slug}` сохранён ({_pages_word(pages)}). Обновить меню: `/rules publish`.{note}",
            ephemeral=True,
        )

    @app_commands.command(name="remove", description="Удалить раздел")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def remove_cmd(self, inter: discord.Interaction, slug: str) -> None:
        if not await self.rules.remove(inter.guild.id, slug):
            raise app_commands.AppCommandError(f"Раздела `{slug}` нет. Список: `/rules list`.")
        await inter.response.send_message(f"Раздел `{slug}` удалён. Обновите меню: `/rules publish`.", ephemeral=True)

    @app_commands.command(name="seed", description="Заполнить меню из seed-файла (rules.example.json по умолчанию)")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def seed(self, inter: discord.Interaction) -> None:
        if not self._seed:
            raise app_commands.AppCommandError(
                "Seed-файл пуст или отсутствует (ключ `rules_seed_file` в config.json)."
            )
        slugs = [
            await self.rules.upsert(inter.guild.id, item["title"], item["pages"],
                                    emoji=item["emoji"], color=parse_color(item["color"]))
            for item in self._seed
        ]
        await inter.response.send_message(
            f"Меню заполнено из файла: {len(slugs)} раздел(ов). Теперь `/rules publish`.", ephemeral=True
        )

    @app_commands.command(name="list", description="Что сейчас в меню")
    @app_commands.guild_only()
    async def list_cmd(self, inter: discord.Interaction) -> None:
        categories = await self.rules.sections(inter.guild.id)
        if not categories:
            await inter.response.send_message("Разделов нет. Добавьте через `/rules add`.", ephemeral=True)
            return
        body = "\n".join(
            style.bullet(f"`{c['slug']}` · {c['emoji'] or '📕'} {c['title']} · {_pages_word(len(c['pages']))}")
            for c in categories
        )
        await inter.response.send_message(f"Разделы правил:\n{body[:1800]}", ephemeral=True)

    @app_commands.command(name="import", description="Загрузить разделы из JSON-файла")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(файл="JSON: [{emoji, title, color, text | pages:[…]}, …]", заменить="снести текущие разделы")
    async def import_cmd(self, inter: discord.Interaction, файл: discord.Attachment, заменить: bool = False) -> None:
        if файл.size and файл.size > 512 * 1024:
            raise app_commands.AppCommandError("Файл больше 512 КБ — не похоже на текст правил.")
        try:
            data = parse_import((await файл.read()).decode("utf-8-sig"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise app_commands.AppCommandError(f"Не смог прочитать файл: {exc}") from None
        if заменить:
            for c in await self.rules.sections(inter.guild.id):
                await self.rules.remove(inter.guild.id, c["slug"])
        slugs = [
            await self.rules.upsert(inter.guild.id, item["title"], item["pages"],
                                    emoji=item["emoji"], color=parse_color(item["color"]))
            for item in data
        ]
        await inter.response.send_message(
            f"Загружено разделов: {len(slugs)} — {', '.join(f'`{s}`' for s in slugs)}. Дальше `/rules publish`.",
            ephemeral=True,
        )

    @app_commands.command(name="config", description="Канал меню, канал жалоб, роль помощи, кнопка перехода")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(
        канал_меню="Где живёт карточка с меню",
        канал_жалоб="На него ссылается подсказка «есть вопрос по наказанию»",
        роль_помощи="Роль, которую зовут в спорных ситуациях",
        кнопка_канал="Куда ведёт кнопка «Все прочитали?» под открытым разделом",
    )
    async def config(
        self,
        inter: discord.Interaction,
        канал_меню: discord.TextChannel | None = None,
        канал_жалоб: discord.TextChannel | None = None,
        роль_помощи: discord.Role | None = None,
        кнопка_канал: discord.TextChannel | None = None,
    ) -> None:
        patch: dict[str, object] = {}
        if канал_меню:
            patch["rules_channel_id"] = канал_меню.id
        if канал_жалоб:
            patch["complaint_channel_id"] = канал_жалоб.id
        if роль_помощи:
            patch["staff_role_id"] = роль_помощи.id
        if кнопка_канал:
            patch["chat_channel_id"] = кнопка_канал.id
        if not patch:
            raise app_commands.AppCommandError("Нечего сохранять — укажите хотя бы один параметр.")
        await self.bot.db.set_guild_config(inter.guild.id, patch)
        await inter.response.send_message("Сохранено: " + ", ".join(f"`{k}`" for k in patch), ephemeral=True)

    @app_commands.command(name="refresh", description="Обновить выложенную карточку после правок")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def refresh(self, inter: discord.Interaction) -> None:
        cfg = await self.bot.db.get_guild_config(inter.guild.id)
        channel = inter.guild.get_channel(int(cfg.get("rules_channel_id") or 0))
        message_id = int(cfg.get("rules_message_id") or 0)
        if channel is None or not hasattr(channel, "fetch_message") or not message_id:
            raise app_commands.AppCommandError("Меню ещё не выложено: `/rules publish канал:#…`.")
        try:
            msg = await channel.fetch_message(message_id)
        except discord.NotFound:
            await self.bot.db.set_guild_config(inter.guild.id, {"rules_message_id": 0})
            raise app_commands.AppCommandError("Карточку удалили из канала — выложите заново: `/rules publish`.") from None
        categories = await self.rules.sections(inter.guild.id)
        embed, _ = build_embed(categories, cfg, None, 0)
        await msg.edit(embed=embed, view=RulesView(categories))
        await inter.response.send_message("Меню обновлено.", ephemeral=True)


def parse_import(raw: str) -> list[dict]:
    """JSON-массив разделов: проверяем по месту, чтобы битый файл не
    загрузился наполовину."""
    data = json.loads(raw)
    if isinstance(data, dict):  # дозволяем и один раздел файлом
        data = [data]
    if not isinstance(data, list):
        raise ValueError("ожидался список разделов: `[ {…}, {…} ]`")
    out = []
    for index, item in enumerate(data, 1):
        if not isinstance(item, dict) or not str(item.get("title", "")).strip():
            raise ValueError(f"в элементе {index} нет поля `title`")
        body = item.get("pages") or item.get("text") or ""
        pages = [str(p).strip() for p in (body if isinstance(body, list) else [body]) if str(p).strip()]
        if not pages:
            raise ValueError(f"раздел {index} («{item['title']}») пустой")
        out.append({
            "title": str(item["title"]).strip(),
            "emoji": str(item.get("emoji") or ""),
            "color": item.get("color"),
            "pages": pages,
        })
    return out


def parse_color(value: object) -> int:
    named = {
        "amber": style.COLOR_AMBER, "оранжевый": style.COLOR_AMBER,
        "bordeaux": style.COLOR_BORDEAUX, "бордо": style.COLOR_BORDEAUX,
        "cyan": style.COLOR_CYAN, "голубой": style.COLOR_CYAN,
        "green": style.COLOR_GREEN, "зелёный": style.COLOR_GREEN,
        "info": style.COLOR_INFO, "синий": style.COLOR_INFO,
    }
    if value is None:
        return style.COLOR_AMBER
    if isinstance(value, int):
        return value & 0xFFFFFF
    raw = str(value).strip().lower()
    text = raw.lstrip("#")
    if text in named:
        return named[text]
    try:
        if raw.startswith("#") or len(text) == 6:
            return int(text, 16) & 0xFFFFFF  # «#ff8800» и каноничный 6-значный hex
        return int(text, 10) & 0xFFFFFF      # «16753920» — десятичное, а не hex
    except ValueError:
        return style.COLOR_AMBER


async def setup(bot) -> None:
    await bot.add_cog(Rules(bot))
    # шаблон для dispatch после рестарта: все возможные custom_id на месте
    bot.add_view(RulesView.dispatch_template())
