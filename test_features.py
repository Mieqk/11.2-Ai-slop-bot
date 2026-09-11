"""Офлайн-тесты интерактивных правил, медиа-карточек и войс-комнат.

Дискорда нет: карточки собираются напрямую, UI-компоненты конструируются как
объекты, a логика комнат проверяется на чистых функциях (plan_join/plan_leave).

Запуск: .venv/bin/python test_features.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import discord

from core import rules_repo, style
from core.db import Database
from cogs import media, rules, voice

CHECKS: list[tuple[str, bool, object]] = []


def check(name: str, ok: bool, detail: object = None) -> None:
    CHECKS.append((name, bool(ok), detail))


def embed_field(embed, name):
    return next((f.value for f in embed.fields if f.name == name), None)


# ------------------------------------------------------------------ house style
def test_style() -> None:
    check("style.head: скобки и капс", style.head("Правила голосовых каналов") == "[ПРАВИЛА ГОЛОСОВЫХ КАНАЛОВ]",
          style.head("Правила голосовых каналов"))
    check("style.head: схлопывает пробелы", style.head("  а   б ") == "[А Б]", style.head("  а   б "))
    check("style.bullet: middot", style.bullet("текст") == "· текст", style.bullet("текст"))
    check("style.clause: номер + текст в коде", style.clause("2.1", "Владелец — первый занявший") ==
          "`2.1 Владелец — первый занявший`", style.clause("2.1", "x"))
    check("style.pages_label: с 1", style.pages_label(0, 3) == "1 / 3", style.pages_label(0, 3))


# ------------------------------------------------------------------ split_pages
def test_pages() -> None:
    check("pages: пусто -> одна страница", rules_repo.split_pages("") == [""], rules_repo.split_pages(""))
    short = rules_repo.split_pages("раз\nдва")
    check("pages: короткое не режется", short == ["раз\nдва"], short)
    text = "\n\n".join(f"Пункт {i}. " + "слова " * 60 for i in range(40))
    pages = rules_repo.split_pages(text)
    check("pages: много абзацев -> несколько страниц", len(pages) > 1 and all(len(p) <= 1600 for p in pages),
          [len(p) for p in pages])
    check("pages: текст не теряется",
          "".join("".join(p) for p in pages) == text.replace("\n", "").replace(" ", "") == text.replace("\n", "").replace(" ", "")
          if False else " ".join(" ".join(pages).split()) in " ".join(text.split()) + " " or
          all(f"Пункт {i}." in " ".join(" ".join(pages).split()) for i in range(40)),
          f"{len(text)} -> {sum(len(p) for p in pages)}")
    giant = rules_repo.split_pages("А" * 5000)
    check("pages: гигант без переносов режется", all(len(p) <= 1600 for p in giant) and "".join(giant) == "А" * 5000,
          [len(p) for p in giant])
    sentences = rules_repo.split_pages("Предложение тут довольно длинное. " * 200)
    check("pages: режет по предложениям, не по словам",
          all(len(p) <= 1600 for p in sentences) and all(p.rstrip().endswith((".", "!", "?")) for p in sentences[:-1]),
          [p[-25:] for p in sentences])
    try:
        rules_repo.split_pages("текст", limit=50)
        check("pages: подозрительно маленький лимит отбивается", False, "прошло")
    except ValueError:
        check("pages: подозрительно маленький лимит отбивается", True)
    check("pages: idempotent", rules_repo.split_pages(rules_repo.split_pages(text)[0])[0] ==
          rules_repo.split_pages(text)[0], "нет")


def test_slug() -> None:
    check("slug: кириллица", rules_repo.slugify("Правила Голосовых КАНАЛОВ!") == "правила-голосовых-каналов",
          rules_repo.slugify("Правила Голосовых КАНАЛОВ!"))
    check("slug: мусор -> дефолт", rules_repo.slugify("  ……… ") == "pravila", rules_repo.slugify("  …… "))
    check("slug: номер пункта сохраняет дефисы", rules_repo.slugify("3 б Конфликтные ситуации") == "3-б-конфликтные-ситуации",
          rules_repo.slugify("3 б Конфликтные ситуации"))
    check("slug: длинное режется", len(rules_repo.slugify("а" * 200)) <= 60, len(rules_repo.slugify("а" * 200)))


# ------------------------------------------------------------------- репозиторий
async def test_repo() -> None:
    fd, path = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    db = Database(path)
    await db.setup()
    repo = rules_repo.RulesRepo(db)
    try:
        await repo.upsert(7, "Общие правила", "первый абзац\n\nвторой абзац", emoji="📕")
        await repo.upsert(7, "Голосовые каналы", "x" * 4000, emoji="🔊")
        cats = await repo.sections(7)
        check("repo: две категории", len(cats) == 2, [c["slug"] for c in cats])
        check("repo: порядок сохранён", [c["title"] for c in cats] == ["Общие правила", "Голосовые каналы"], cats)
        check("repo: длинный текст разбит на страницы", len(cats[1]["pages"]) > 1, len(cats[1]["pages"]))
        check("repo: цвет по умолчанию янтарный", cats[0]["color"] == rules_repo.COLOR_DEFAULT, cats[0]["color"])
        # повторный upsert не множит записи, но заменят содержимое
        await repo.upsert(7, "Общие правила", "обновлённый текст", emoji="📕")
        again = await repo.get(7, "общие-правила")
        check("repo: upsert заменяет, а не дублирует",
              len(await repo.sections(7)) == 2 and again["pages"] == ["обновлённый текст"], again)
        # правка регистра/пробелов в заголовке = апдейт того же раздела, а не второй
        await repo.upsert(7, "  ОБЩИЕ   ПРАВИЛА  ", "правки не было", emoji="📕")
        after_rename = await repo.sections(7)
        check("repo: переименование регистра не плодит раздел",
              len([c for c in after_rename if c["slug"] == "общие-правила"]) == 1, [c["slug"] for c in after_rename])
        check("repo: переименование сохранило новый текст",
              (await repo.get(7, "общие-правила"))["pages"] == ["правки не было"], (await repo.get(7, "общие-правила")))
        await repo.upsert(7, "Общие правила", "первый абзац\n\nвторой абзац", emoji="📕")
        check("repo: get по несуществующему slug -> None", (await repo.get(7, "нету")) is None, None)
        check("repo: get чужого сервера -> None", (await repo.get(8, "общие-правила")) is None, None)
        # удаление + перенумерация (иначе в порядке дыра, и reorder не работает)
        await repo.upsert(7, "Третьи", "текст", emoji="☕")
        cats = await repo.sections(7)
        await repo.remove(7, cats[1]["slug"])
        after = await repo.sections(7)
        check("repo: удаление снимает запись", len(after) == 2 and cats[1]["slug"] not in [c["slug"] for c in after], after)
        check("repo: после удаления порядок плотный", [c["position"] for c in after] == [0, 1],
              [c["position"] for c in after])
        await repo.reorder(7, [after[1]["slug"], after[0]["slug"]])
        check("repo: reorder меняет порядок", [c["slug"] for c in await repo.sections(7)][0] == after[1]["slug"],
              [c["slug"] for c in await repo.sections(7)])
        check("repo: remove несуществующего -> False", not await repo.remove(7, "нету"), None)
        # заголовок и эмодзи из чужого JSON могут быть сколь угодно длинными
        await repo.upsert(7, "Д" * 600, "текст", emoji="📕" * 60)
        long_cat = (await repo.sections(7))[-1]
        check("repo: слишком длинный заголовок и эмодзи урезаются",
              len(long_cat["title"]) <= 200 and len(long_cat["emoji"]) <= 16,
              (len(long_cat["title"]), len(long_cat["emoji"])))
        check("repo: slug длинного раздела не выбит в пустоту", bool(long_cat["slug"]), long_cat["slug"])
        await repo.remove(7, long_cat["slug"])
        check("repo: мусорный раздел убран", len(await repo.sections(7)) == 2, [c["slug"] for c in await repo.sections(7)])
    finally:
        db.close()
        os.unlink(path)


# ------------------------------------------------------------------- импорт/цвет
def test_import_color() -> None:
    good = json.dumps([{"title": "Общие", "emoji": "📕", "text": "раз\n\nдва"},
                       {"title": "Войс", "pages": ["a", "b"], "color": "bordeaux"}], ensure_ascii=False)
    parsed = rules.parse_import(good)
    check("import: два раздела", len(parsed) == 2 and parsed[1]["pages"] == ["a", "b"], parsed)
    check("import: dict-одиночка тоже валиден", len(rules.parse_import('{"title":"Т","text":"x"}')) == 1, None)
    for bad, why in [("{}", "не список"), ("[{}]", "нет title"), ('[{"title":"x","text":"  "}]', "пустой текст"),
                     ("[1,2]", "не объект"), ("не json", "битый json")]:
        try:
            rules.parse_import(bad)
            check(f"import отбивает «{why}»", False, bad)
        except (ValueError, json.JSONDecodeError):
            check(f"import отбивает «{why}»", True)
    check("color: amber", rules.parse_color("amber") == style.COLOR_AMBER, None)
    check("color: бордо по-русски", rules.parse_color("бордо") == style.COLOR_BORDEAUX, None)
    check("color: hex без #", rules.parse_color("ff8800") == 0xFF8800, None)
    check("color: hex с #", rules.parse_color("#FF8800") == 0xFF8800, None)
    check("color: int как есть", rules.parse_color(0x123456) == 0x123456, None)
    check("color: десятичное число не читается как hex", rules.parse_color("16753920") == 0xFFA500, 
          hex(rules.parse_color("16753920")))
    check("color: #rrggbb", rules.parse_color("#00a8fc") == style.COLOR_CYAN, None)
    check("color: мусор -> дефолт", rules.parse_color("левый") == style.COLOR_AMBER, None)
    check("color: None -> дефолт", rules.parse_color(None) == style.COLOR_AMBER, None)


# ------------------------------------------------------------------- карточки
CATS = [
    {"slug": "obschie", "emoji": "📕", "title": "Общие правила", "color": style.COLOR_BORDEAUX,
     "position": 0, "pages": ["первая страница", "вторая страница", "третья"]},
    {"slug": "vois", "emoji": "🔊", "title": "Голосовые каналы", "color": style.COLOR_AMBER,
     "position": 1, "pages": ["единственная"]},
]


def test_cards() -> None:
    embed, section = rules.build_embed(CATS, {}, None, 0)
    check("card: меню без выбора", section is None and embed.title == "[ПРАВИЛА СЕРВЕРА]", embed.title)
    check("card: список разделов с эмодзи", "📕 **Общие правила**" in embed.description, embed.description[:80])
    check("card: склонение «3 страницы»", "3 страницы" in embed.description, embed.description)
    check("card: склонение «1 страница»", "1 страница" in embed.description, embed.description)
    check("card: меню объясняет, что откроется только нажавшему", "только вам" in embed.description, embed.description[-80:])
    check("card: у меню нет футера", not embed.footer, embed.footer)
    priv, _ = rules.build_embed(CATS, {}, "obschie", 0, private=True)
    check("card: ephemeral-карточка помечена «видите только вы»", "видите только вы" in priv.footer.text, priv.footer.text)
    publ, _ = rules.build_embed(CATS, {}, "obschie", 0)
    check("card: публичная карточка так не помечена", "видите только вы" not in publ.footer.text, publ.footer.text)

    empty, _ = rules.build_embed([], {}, None, 0)
    check("card: пустое меню объясняет, что делать", "/rules add" in empty.description, empty.description)

    embed, section = rules.build_embed(CATS, {}, "obschie", 1)
    check("card: раздел открыт", section["slug"] == "obschie", section)
    check("card: заголовок раздела в скобках", embed.title == "[📕 ОБЩИЕ ПРАВИЛА]", embed.title)
    check("card:_show_page 2 из 3", embed.description == "вторая страница", embed.description)
    check("card: футер со страницей", embed.footer.text == "· страница 2/3", embed.footer.text)
    check("card: цвет раздела", embed.colour.value == style.COLOR_BORDEAUX, hex(embed.colour.value))

    clamped, _ = rules.build_embed(CATS, {}, "obschie", 99)
    check("card: страница за границей прижимается", clamped.description == "третья", clamped.description)
    under, _ = rules.build_embed(CATS, {}, "obschie", -5)
    check("card: отрицательная страница -> первая", under.description == "первая страница", under.description)
    nobody, _ = rules.build_embed(CATS, {}, "несуществует", 0)
    check("card: неизвестный раздел = меню", nobody.title == "[ПРАВИЛА СЕРВЕРА]", nobody.title)

    cfg = {"complaint_channel_id": 555, "staff_role_id": 777}
    with_hint, _ = rules.build_embed(CATS, cfg, "obschie", 0)
    check("card: подсказка про жалобы есть", "<@#555>" in with_hint.description or "<#555>" in with_hint.description,
          with_hint.description[-90:])
    check("card: подсказка зовёт роль", "<@&777>" in with_hint.description, with_hint.description[-90:])
    solo, _ = rules.build_embed(CATS, {"complaint_channel_id": 555}, "vois", 0)
    check("card: одна подсказка без «·» хвостом", solo.description.endswith("<#555>"), solo.description[-40:])
    check("card: без настроек подсказки нет",
          rules.build_embed(CATS, {}, "vois", 0)[0].description == "единственная", None)


def test_page_marker() -> None:
    def fake(text):
        return SimpleNamespace(embeds=[SimpleNamespace(footer=SimpleNamespace(text=text))])

    check("marker: страница 3/5", rules.page_of(fake("· страница 3/5")) == 2, rules.page_of(fake("· страница 3/5")))
    check("marker: нет футера -> 0", rules.page_of(fake("")) == 0, None)
    check("marker: мусор в футере -> 0", rules.page_of(fake("· страница x/y")) == 0, None)
    check("marker: отрицательная защита", rules.page_of(fake("· страница 0/3")) == 0, None)
    check("marker: нет embed'ов -> 0", rules.page_of(SimpleNamespace(embeds=[])) == 0, None)


# ------------------------------------------------------------------- view
def _labels(view) -> list[str]:
    return [c.label for c in view.children if isinstance(c, discord.ui.Button)]


def _ids(view) -> list[str]:
    return [c.custom_id for c in view.children if isinstance(c, discord.ui.Button)]


def test_view() -> None:
    menu = rules.RulesView(CATS)
    check("view: меню = по кнопке на раздел", _labels(menu) == ["Общие правила", "Голосовые каналы"], _labels(menu))
    check("view: custom_id кнопок разделов", _ids(menu) == ["rules:open:0", "rules:open:1"], _ids(menu))
    check("view: эмодзи раздела на кнопке",
          [(c.emoji or {}).get("name") if isinstance(c.emoji, dict) else getattr(c.emoji, "name", None)
           for c in menu.children] == ["📕", "🔊"], [c.emoji for c in menu.children])
    check("view: ни одна кнопка меню не выключена", all(not c.disabled for c in menu.children), None)
    check("view: callback у кнопок живой", all(callable(c.callback) for c in menu.children), None)
    check("view: ни select'а, ни select-остатков", not any(isinstance(c, discord.ui.Select) for c in menu.children), None)

    section = rules.RulesView(CATS, section="obschie", page=1, guild_id=7)
    check("view: в разделе — ≡ ◀ ▶", _labels(section) == ["≡ к списку", "◀", "▶"], _labels(section))
    check("view: ◀ активна не на первой, ▶ активна до последней",
          section.children[1].disabled is False and section.children[2].disabled is False,
          [c.disabled for c in section.children])
    first = rules.RulesView(CATS, section="obschie", page=0, guild_id=7)
    last = rules.RulesView(CATS, section="obschie", page=2, guild_id=7)
    check("view: на первой ◀ выключена", first.children[1].disabled is True, _labels(first))
    check("view: на последней ▶ выключена", last.children[2].disabled is True, _labels(last))
    check("view: nav custom_id стабильны (persistent dispatch)",
          _ids(last) == ["rules:back", "rules:prev", "rules:next"], _ids(last))

    link = rules.RulesView(CATS, section="obschie", page=0, guild_id=7, chat_channel_id=55)
    check("view: кнопка-переход появляется только с каналом", len(_labels(link)) == 4 and
          link.children[3].url.endswith("/7/55"), link.children[3].url if len(_labels(link)) == 4 else _labels(link))

    many = [{"slug": f"s{i}", "emoji": "", "title": f"Раздел {i}", "color": 0, "pages": ["x"]} for i in range(40)]
    capped = rules.RulesView(many)
    check("view: кнопок не больше лимита Discord", len(list(capped.children)) == rules.MAX_SECTION_BUTTONS,
          len(list(capped.children)))
    empty = rules.RulesView([])
    check("view: пустое меню — ни одной кнопки", not list(empty.children), None)

    tpl = rules.RulesView.dispatch_template()
    tpl_ids = [c.custom_id for c in tpl.children]
    possible = [rules.open_id(i) for i in range(rules.MAX_SECTION_BUTTONS)] + [
        rules.BACK_ID, rules.PREV_ID, rules.NEXT_ID]
    check("template: в диспетчере есть ВСЕ custom_id, что бывают на карточках",
          set(tpl_ids) == set(possible), sorted(set(possible) - set(tpl_ids)))
    check("template: у каждой кнопки живой callback", all(callable(c.callback) for c in tpl.children), None)
    check("template: кнопок не больше лимита Discord на сообщение", len(tpl_ids) <= 25, len(tpl_ids))

    check("view: open_index_round", [rules.open_index(rules.open_id(i)) for i in (0, 3, 19)] == [0, 3, 19], None)
    check("view: чужой custom_id != раздел", rules.open_index("rules:prev") == -1 and rules.open_index("rules:open:x") == -1, None)
    check("view: slug_from_title по заголовку меню -> None",
          rules.slug_from_title(CATS, SimpleNamespace(embeds=[discord.Embed(title=style.head(rules.MENU_TITLE))])) is None, None)


# ---------------------------------------------------- насквозь: клик по кнопке
async def test_click_flow() -> None:
    """Кнопка раздела -> ephemeral-карточка; листание правит её же."""
    fd, path = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    db = Database(path)
    await db.setup()
    repo = rules_repo.RulesRepo(db)
    await repo.upsert(7, "Общие правила", "\n\n".join(f"страница {i}\n\n" + "текст " * 300 for i in (1, 2, 3)), emoji="📕")
    section = (await repo.sections(7))[0]
    total = len(section["pages"])

    class FakeDB:
        async def get_guild_config(self, gid):
            return {"complaint_channel_id": 0, "staff_role_id": 0, "chat_channel_id": 0}

    class Client:
        rules = repo
        db = FakeDB()

    class Response:
        def __init__(self):
            self.edited, self.sent = [], []

        async def edit_message(self, *, embed, view):
            self.edited.append((embed, view))

        async def send_message(self, content=None, *, embed=None, view=None, ephemeral=False):
            self.sent.append({"content": content, "embed": embed, "view": view, "ephemeral": ephemeral})

    class Guild:
        id = 7

    def make_inter(message=None):
        return SimpleNamespace(client=Client(), guild=Guild(), response=Response(), message=message)

    # 1) клик по кнопке раздела в публичном меню — ответ ephemeral
    inter = make_inter()
    await rules.RulesView([section]).children[0].callback(inter)
    assert inter.response.sent, "кнопка ничего не отправила"
    sent = inter.response.sent[0]
    check("click: раздел открыт ephemeral-сообщением", sent["ephemeral"] is True, sent["ephemeral"])
    check("click: в ephemeral не редактируется чужое сообщение", not inter.response.edited, inter.response.edited)
    embed, view = sent["embed"], sent["view"]
    check("click: открыт нужный раздел", embed.title == style.head("📕 Общие правила"), embed.title)
    check("click: первая страница", embed.description == section["pages"][0][:4000], embed.description[:40])
    check("click: пометка «видите только вы»", embed.footer.text.endswith("· видите только вы"), embed.footer.text)
    check("click: футер склеен пробелом", "7· в" not in embed.footer.text and " · " in embed.footer.text, embed.footer.text)
    check("click: внутри есть листалки", len(_labels(view)) == 3, _labels(view))

    # 2) листание внутри ephemeral (и после рестарта — через шаблонное view)
    for expected in range(1, total):
        inter = make_inter(message=SimpleNamespace(embeds=[embed]))
        await rules.RulesView([])._step(inter, +1)
        embed, view = inter.response.edited[0]
        check(f"click: страница {expected + 1}/{total} через шаблонное view",
              embed.description == section["pages"][expected][:4000], embed.description[:40])
        check(f"click: футер {expected + 1} из {total}",
              embed.footer.text == f"· страница {expected + 1}/{total} · видите только вы", embed.footer.text)
        check("click: листание правит своё сообщение, а не шлёт новое",
              inter.response.edited and not inter.response.sent, None)
    check("click: на последней ▶ гаснет", view.children[2].disabled is True, [c.disabled for c in view.children])

    inter = make_inter(message=SimpleNamespace(embeds=[embed]))
    await rules.RulesView([])._step(inter, +1)
    check("click: за последнюю не уходит", inter.response.edited[0][0].footer.text.startswith(f"· страница {total}/{total}"),
          inter.response.edited[0][0].footer.text)

    # 3) ≡ возвращает список разделов в то же ephemeral-сообщение
    inter = make_inter(message=SimpleNamespace(embeds=[embed]))
    await rules.RulesView([])._on_back(inter)
    back_embed, back_view = inter.response.edited[0]
    check("click: ≡ — список разделов обратно в ephemeral",
          back_embed.title == style.head(rules.MENU_TITLE) and len(_labels(back_view)) == 1,
          (back_embed.title, _labels(back_view)))

    # 4) раздел успели удалить — обработчик не падает
    await repo.remove(7, section["slug"])
    inter = make_inter(message=SimpleNamespace(embeds=[embed]))
    await rules.RulesView([])._step(inter, +1)
    check("click: удалённый раздел не роняет листание", bool(inter.response.edited), None)
    inter = make_inter()
    await rules.RulesView([section]).children[0].callback(inter)
    check("click: кнопка по удалённому разделу отвечает текстом",
          inter.response.sent and "удалён" in inter.response.sent[0]["content"], inter.response.sent)

    # 4b) кнопка-переход берётся из настройки сервера (`/rules config кнопка_канал`)
    await db.set_guild_config(7, {"chat_channel_id": 404})
    class ClientWithChat(Client):
        class db:  # noqa: N801
            @staticmethod
            async def get_guild_config(gid):
                return {"complaint_channel_id": 0, "staff_role_id": 0, "chat_channel_id": 404}

    inter = make_inter()
    inter.client = ClientWithChat()
    await repo.upsert(7, "Общие правила", "одна страница", emoji="📕")
    fresh = await repo.sections(7)
    await rules.RulesView(fresh).children[0].callback(inter)
    opened = inter.response.sent[0]
    check("click: кнопка-переход подставляется из настроек сервера",
          any((getattr(c, "url", None) or "").endswith("/7/404") for c in opened["view"].children),
          [(getattr(c, "url", None) or c.label) for c in opened["view"].children])
    await db.set_guild_config(7, {"chat_channel_id": 0})

    # 5) link-кнопка не требует dispatch: у неё нет custom_id/callback
    with_link = rules.RulesView([{"slug": "s", "emoji": "", "title": "T", "color": 0, "pages": ["a"]}],
                                section="s", page=0, guild_id=7, chat_channel_id=9)
    link_btn = with_link.children[3]
    check("click: кнопка-переход это link без callback-диспетчера",
          link_btn.style is discord.ButtonStyle.link and link_btn.url == "https://discord.com/channels/7/9",
          link_btn.url)

    db.close()
    os.unlink(path)


# ------------------------------------------------------------- publish: 1 карточка
SEED_JSON = json.dumps([
    {"title": "Общие правила", "emoji": "📕", "color": "bordeaux", "text": "раз\n\nдва"},
    {"title": "Голосовые каналы", "emoji": "🔊", "color": "amber", "text": "три"},
], ensure_ascii=False)


async def test_publish_flow() -> None:
    """Повторный publish правит карточку, а не плодит вторую."""
    fd, path = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    db = Database(path)
    await db.setup()
    repo = rules_repo.RulesRepo(db)
    await repo.upsert(7, "Общие", "текст", emoji="📕")

    sent, edited, replies = [], [], []

    class Chan:
        id, mention = 500, "<#500>"

        def __init__(self, *, gone=False):
            self.gone = gone

        async def send(self, *, embed, view=None):
            sent.append((embed, view))
            return SimpleNamespace(id=900 + len(sent))

        async def fetch_message(self, mid):
            if self.gone:
                raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), {"message": "x"})
            return SimpleNamespace(id=mid, edit=self._edit)

        async def _edit(self, **kw):
            edited.append(kw.get("embed"))
            return SimpleNamespace(id=1)

    class Guild7:
        id = 7

        def __init__(self, chan):
            self.chan = chan

        def get_channel(self, cid):
            return self.chan if cid == self.chan.id else None

    async def run(cfg_extra, *, gone=False):
        chan = Chan(gone=gone)
        await db.set_guild_config(7, {"rules_channel_id": 500, **cfg_extra})

        async def echo(content=None, **kw):
            replies.append(content if content is not None else kw.get("content"))

        inter = SimpleNamespace(guild=Guild7(chan), channel=chan, response=SimpleNamespace(send_message=echo),
                                user=SimpleNamespace(display_name="admin", id=1))
        cog = rules.Rules(SimpleNamespace(db=db, rules=repo))
        await rules.Rules.publish.callback(cog, inter, канал=None)

    await run({"rules_message_id": 0})
    check("publish: без старой карточки — новая в канале", len(sent) == 1 and not edited, (sent, edited))

    # пустой сервер: publish сам берёт заготовки из rules_seed_file
    sent.clear(); edited.clear()
    await db.set_guild_config(8, {"rules_channel_id": 0, "rules_message_id": 0})

    class Guild8:
        id = 8

        def __init__(self, chan):
            self.chan = chan

        def get_channel(self, cid):
            return self.chan if cid == self.chan.id else None

    chan8 = Chan()

    async def echo8(content=None, **kw):
        replies.append(content)

    inter8 = SimpleNamespace(guild=Guild8(chan8), channel=chan8, response=SimpleNamespace(send_message=echo8),
                             user=SimpleNamespace(display_name="admin", id=1))
    seeded = rules.Rules(SimpleNamespace(db=db, rules=repo))
    seeded._seed = rules.parse_import(SEED_JSON)
    await rules.Rules.publish.callback(seeded, inter8, канал=chan8)
    after = await repo.sections(8)
    check("publish: пустой сервер заполнился из seed-файла", len(after) == len(seeded._seed), [c["slug"] for c in after])
    menu_embed, menu_view = sent[-1]
    check("publish: кнопки меню = число разделов", len(list(menu_view.children)) == len(after),
          [c.label for c in menu_view.children])
    check("publish: меню не ссылается на удалённые разделы",
          [c.label for c in menu_view.children] == [c["title"] for c in after],
          ([c.label for c in menu_view.children], [c["title"] for c in after]))
    sent.clear(); edited.clear()
    await run({"rules_message_id": 901})
    check("publish: со старой карточкой — edit, а не вторая", len(sent) == 0 and len(edited) == 1, (sent, edited))
    sent.clear(); edited.clear()
    await run({"rules_message_id": 901}, gone=True)
    check("publish: старую удалили — кладём новую", len(sent) == 1 and not edited, (sent, edited))
    check("publish: ответ модератору текстом", all(r for r in replies[-3:]) or replies, replies[-1:])
    cfg = await db.get_guild_config(7)
    check("publish: id карточки запомнен", cfg["rules_message_id"] > 0, cfg["rules_message_id"])
    db.close()
    os.unlink(path)


# ------------------------------------------------------------------- медиа
def test_media() -> None:
    ok = ["https://x.ru/a.png", "https://x.ru/a.PNG", "https://x.ru/a.jpg?sig=1", "https://x.ru/a.webp#f",
          "https://cdn.discordapp.com/attachments/1/2/a.jpeg", "https://x.ru/anime.gif"]
    bad = ["https://x.ru/page", "https://youtu.be/abc", "https://x.ru/a.pdf", "", None, "https://x.ru/a.svgz"]
    check("media: формат картинок узнаётся", all(media.looks_like_image(u) for u in ok),
          [u for u in ok if not media.looks_like_image(u)])
    check("media: не-картинки отбиваются", all(not media.looks_like_image(u) for u in bad),
          [u for u in bad if media.looks_like_image(u)])

    link = "https://discord.com/channels/111/222/333"
    check("media: ссылка на сообщение разбирается", media.parse_message_link(link) == (222, 333),
          media.parse_message_link(link))
    check("media: канал в треде (без id сообщения) отбивается",
          media.parse_message_link("https://discord.com/channels/111/222") == (0, 0), None)
    check("media: ссылка на пользователя отбивается",
          media.parse_message_link("https://discord.com/@me") == (0, 0), None)
    check("media: мусор отбивается", media.parse_message_link("не ссылка") == (0, 0), None)
    check("media: пустая строка не падает", media.parse_message_link("") == (0, 0), None)
    check("media: нечисловые id отбиваются",
          media.parse_message_link("https://discord.com/channels/a/b/c") == (0, 0), None)
    check("media: хвостовые слеши", media.parse_message_link("  " + link + "/  ") == (222, 333),
          media.parse_message_link("  " + link + "/  "))
    check("media: app/disconect-домен тоже проходит",
          media.parse_message_link("https://canary.discord.com/channels/1/2/3") == (2, 3), None)

    embed = media.photo_embed("Фотография", "подпись", style.COLOR_CYAN)
    check("media: заголовок в скобках", embed.title == "[ФОТОГРАФИЯ]", embed.title)
    check("media: подпись как description", embed.description == "подпись", embed.description)
    check("media: пустая подпись не оставляет ``", media.photo_embed("T", None, 0).description is None, None)


# ------------------------------------------------------------------- войс
def test_voice() -> None:
    class RoleRef:  # discord.Role hashable — он тоже ключ в overwrites
        def __init__(self, rid):
            self.id = rid

        def __hash__(self):
            return hash(self.id)

    guild = SimpleNamespace(id=1, default_role=RoleRef(1))
    members = {}

    class Person:  # Member hashable: ключ словаря оверрайтов
        def __init__(self, mid, name, is_bot=False):
            self.id, self.display_name, self.name, self.bot, self.guild = mid, name, name, is_bot, guild

        def __hash__(self):
            return hash(self.id)

    def mk(mid, name="игрок", bot=False):
        m = Person(mid, name, bot)
        members[mid] = m
        return m

    def ch(name, *, members_=(), cat=None, cid=100, created=None):
        c = SimpleNamespace(id=cid, name=name, members=list(members_), category=cat, created_at=created)
        return c

    a, b = mk(10), mk(11)
    bot_member = mk(12, "Музыка", bot=True)
    trigger = ch("🐾 Погладить камушек", cid=500)
    public_cat, private_cat = SimpleNamespace(id=601), SimpleNamespace(id=602)
    room = ch(f"{voice.ROOM_PREFIX} {a.display_name}", members_=[a, bot_member], cat=private_cat, cid=700)
    public = ch("гостинная", members_=[a, b], cat=public_cat, cid=701)  # a занял канал до b

    p = voice.plan_join(a, trigger, trigger_id=500, private_cat_id=602, public_cat_id=601)
    check("voice: вход в триггер создаёт комнату", p.create_room and p.grant_ownership, p)
    check("voice: имя комнаты с префиксом", p.room_name.startswith(voice.ROOM_PREFIX) and a.display_name in p.room_name,
          p.room_name)
    nop = voice.plan_join(a, trigger, trigger_id=500, private_cat_id=0, public_cat_id=601)
    check("voice: без категории комнату не плодит", not nop.create_room, nop)
    first = voice.plan_join(a, ch("подвал", members_=[], cat=public_cat, cid=702),
                            trigger_id=500, private_cat_id=602, public_cat_id=601)
    check("voice: первый в публичной = владелец", first.grant_ownership and not first.create_room, first)
    check("voice: plan_join не создаёт комнату в публичной категории", not first.create_room, first)
    second = voice.plan_join(b, public, trigger_id=500, private_cat_id=602, public_cat_id=601)
    check("voice: второй в занятую публичную — не владелец", not second.grant_ownership, second)
    foreign = voice.plan_join(a, ch("музыка", members_=[a], cat=SimpleNamespace(id=999), cid=703),
                              trigger_id=500, private_cat_id=602, public_cat_id=601)
    check("voice: чужие категории не трогаем", not (foreign.create_room or foreign.grant_ownership), foreign)

    lp = voice.plan_leave(a, room, owner_id=a.id, public_cat_id=601)
    check("voice: владелец ушёл, людей нет -> снос", lp.delete_room, lp)
    empty_room = ch(f"{voice.ROOM_PREFIX} {a.display_name}", members_=[a, b], cat=private_cat, cid=704)
    lp2 = voice.plan_leave(a, empty_room, owner_id=a.id, public_cat_id=601)
    check("voice: владелец ушёл, но остались -> передача первому",
          lp2.clear_owner and lp2.transfer_to == b.id and not lp2.delete_room, lp2)
    lp3 = voice.plan_leave(b, room, owner_id=a.id, public_cat_id=601)
    check("voice: невладелец уходит -> ничего", not (lp3.delete_room or lp3.clear_owner), lp3)
    lp4 = voice.plan_leave(None, None, owner_id=None, public_cat_id=601)
    check("voice: уход «в никуда» не роняет план", lp4 == voice.LeavePlan(), lp4)
    lp5 = voice.plan_leave(b, ch("подвал", members_=[b], cat=public_cat, cid=705), owner_id=None, public_cat_id=601)
    check("voice: публичная опустела без владельца -> снимаем права", lp5.clear_owner, lp5)

    check("voice: is_temp_room по префиксу", voice.is_temp_room(room) and not voice.is_temp_room(public), None)
    check("voice: other_members не считает ботов",
          [m.id for m in voice.other_members(room)] == [10], voice.other_members(room))
    check("voice: other_members исключает asked",
          [m.id for m in voice.other_members(room, exclude_id=10)] == [], None)
    check("voice: room_name_for режет пробелы", voice.room_name_for(mk(77, "  И   Я  ")) == "🔒 И Я",
          voice.room_name_for(mk(77, "  И   Я  ")))
    long_name = voice.room_name_for(mk(78, "д" * 200))
    check("voice: имя комнаты в лимит Discord", len(long_name) <= voice.MAX_ROOM_NAME, len(long_name))
    valid = set(discord.Permissions.VALID_FLAGS)
    check("voice: все владельческие права существуют в discord.py",
          not (set(voice.OWNER_PERMS) - valid), sorted(set(voice.OWNER_PERMS) - valid))
    check("voice: CLEAR_PERMS совпадает с OWNER_PERMS", set(voice.CLEAR_PERMS) == set(voice.OWNER_PERMS), None)
    from core import moderation as mod_core
    check("audit: ключи запретов каналов валидны",
          not ((set(mod_core.OWNER_KEYS if hasattr(mod_core, "OWNER_KEYS") else set()) - valid)
               or (set(mod_core.TEXT_KEYS) - valid) or (set(mod_core.VOICE_KEYS) - valid)),
          sorted((set(getattr(mod_core, "TEXT_KEYS", ())) | set(getattr(mod_core, "VOICE_KEYS", ()))) - valid))
    ows = voice.room_overwrites(a)
    check("voice: оверрайты на владельца и @everyone", list(ows) == [a, guild.default_role], list(ows))
    check("voice: владелец может мутить", ows[a].mute_members is True and ows[a].connect is True, ows[a])
    check("voice: владелец может впустить гостей (manage_channels)", ows[a].manage_channels is True, ows[a])
    check("voice: чужим не видно комнату", ows[guild.default_role].connect is False, ows[guild.default_role])

    now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    fresh = ch(f"{voice.ROOM_PREFIX} x", members_=[], cat=private_cat, cid=706, created=now - timedelta(seconds=5))
    old = ch(f"{voice.ROOM_PREFIX} y", members_=[], cat=private_cat, cid=707, created=now - timedelta(minutes=5))
    full = ch(f"{voice.ROOM_PREFIX} z", members_=[a], cat=private_cat, cid=708, created=now - timedelta(hours=1))
    check("voice: gc не трогает свежую комнату", not voice.is_stale_room(fresh, now), None)
    check("voice: gc сносит пустую старую", voice.is_stale_room(old, now), None)
    check("voice: gc не трогает занятую", not voice.is_stale_room(full, now), None)
    check("voice: gc не трогает чужие названия", not voice.is_stale_room(ch("подвал", members_=[], cid=709, created=now), now), None)
    check("voice: gc не падения без created_at", voice.is_stale_room(ch(f"{voice.ROOM_PREFIX} n", members_=[], cid=710), now), None)


async def main() -> int:
    test_style()
    test_pages()
    test_slug()
    await test_repo()
    test_import_color()
    test_cards()
    test_page_marker()
    test_view()
    await test_click_flow()
    await test_publish_flow()
    test_media()
    test_voice()
    fails = [c for c in CHECKS if not c[1]]
    width = max(len(c[0]) for c in CHECKS)
    for name, ok, detail in CHECKS:
        print(f"{'✅' if ok else '❌'} {name:<{width}}  {'' if ok else detail}")
    print(f"\n{len(CHECKS) - len(fails)}/{len(CHECKS)} проверок пройдено")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
