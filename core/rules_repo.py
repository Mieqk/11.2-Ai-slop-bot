"""Хранилище интерактивных правил: категории + страницы текста.

Плюс разбиение простыни на страницы: карточка Discord ограничена по длине,
и молча обрезанное правило «3 б» хуже, чем две страницы.
"""

from __future__ import annotations

import json
import re

#: запас по лимиту description (2048) на разметку и эмодзи
MAX_PAGE = 1600
COLOR_DEFAULT = 0xFFA500


def slugify(title: str) -> str:
    slug = re.sub(r"[^0-9a-zа-яё]+", "-", title.strip().lower()).strip("-")
    return slug[:60] or "pravila"


def _hard_split(text: str, limit: int) -> list[str]:
    """Кусок длиннее лимита: режем по концам предложений, иначе по словам.

    Граница режется ВКЛЮЧАЯ знак конца предложения: иначе страница заканчивается
    серединой фразы, а точка уезжает на следующую страницу.
    """
    out: list[str] = []
    rest = text
    while len(rest) > limit:
        window = rest[:limit]
        best, keep = -1, 0
        for token, extra in ((". ", 1), ("! ", 1), ("? ", 1), (".\n", 1), ("!\n", 1), ("?\n", 1),
                             (";", 1), (",", 1), (" ", 0)):
            at = window.rfind(token)
            if at > best:
                best, keep = at, extra
        cut = best + keep if best >= limit // 3 else limit
        out.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    if rest:
        out.append(rest)
    return out


def split_pages(text: str, limit: int = MAX_PAGE) -> list[str]:
    """Собираем абзацы в страницы по < limit, режа только на границах абзацев."""
    if limit < 200:
        raise ValueError("страница не может быть короче 200 символов")
    text = text.replace("\r\n", "\n").strip()
    if not text:
        return [""]
    pages: list[str] = []
    current = ""
    for para in re.split(r"\n\s*\n", text):
        block = para.strip()
        if not block:
            continue
        for piece in _hard_split(block, limit) if len(block) > limit else [block]:
            joined = f"{current}\n\n{piece}" if current else piece
            if len(joined) > limit:
                if current:
                    pages.append(current)
                current = piece
            else:
                current = joined
    if current:
        pages.append(current)
    return pages or [""]


def _clamp(value: object, limit: int = 200) -> str:
    """Заголовок/эмодзи из импортируемого файла могут быть любого размера."""
    text = " ".join(str(value or "").split())
    return text[:limit]


class RulesRepo:
    """CRUD категорий правил для одного гильдии-видимого набора."""

    def __init__(self, db):
        self.db = db

    async def upsert(self, guild_id: int, title: str, text: str | list[str], *, emoji: str = "",
                     color: int = COLOR_DEFAULT, slug: str | None = None) -> str:
        """Добавить или обновить раздел.

        Заголовок и есть «id раздела» для админа: правка регистра или пробелов
        не должна плодить второй раздел с тем же содержимым — такие правки
        считаем апдейтом того же slug'а.
        """
        title, emoji = _clamp(title), _clamp(emoji, 16)
        pages = text if isinstance(text, list) else split_pages(text)
        wanted = (slug or slugify(title)).strip()
        if slug is None:  # slug вывели из заголовка — ищем «тот же» раздел по заголовку
            same = [c for c in await self.sections(guild_id)
                    if c["slug"] != wanted and slugify(c["title"]) == wanted]
            if same:
                wanted = same[0]["slug"]
        slug = wanted
        existing = await self.get(guild_id, slug)
        position = existing["position"] if existing else len(await self.sections(guild_id))
        await self.db._run(
            self._upsert, guild_id, slug, emoji, title.strip(), int(color), position,
            json.dumps(pages, ensure_ascii=False),
        )
        return slug

    def _upsert(self, guild_id, slug, emoji, title, color, position, pages) -> None:
        self.db._conn.execute(
            "INSERT INTO rule_categories (guild_id, slug, emoji, title, color, position, pages)"
            " VALUES (?,?,?,?,?,?,?)"
            " ON CONFLICT(guild_id, slug) DO UPDATE SET emoji = excluded.emoji,"
            " title = excluded.title, color = excluded.color, pages = excluded.pages",
            (guild_id, slug, emoji, title, color, position, pages),
        )
        self.db._conn.commit()

    async def sections(self, guild_id: int) -> list[dict]:
        rows = await self.db._run(self._sections, guild_id)
        return [
            {
                "slug": r["slug"],
                "emoji": r["emoji"],
                "title": r["title"],
                "color": r["color"],
                "position": r["position"],
                "pages": json.loads(r["pages"] or "[]"),
            }
            for r in rows
        ]

    def _sections(self, guild_id: int):
        return self.db._conn.execute(
            "SELECT * FROM rule_categories WHERE guild_id = ?"
            " ORDER BY position, title",
            (guild_id,),
        ).fetchall()

    async def get(self, guild_id: int, slug: str) -> dict | None:
        return next((c for c in await self.sections(guild_id) if c["slug"] == slug), None)

    async def remove(self, guild_id: int, slug: str) -> bool:
        """Удаляет категорию и перенумеровывает остальные, чтобы в порядке не было дыр."""
        removed = await self.db._run(self._remove, guild_id, slug)
        if removed:
            left = [c["slug"] for c in await self.sections(guild_id)]
            await self.reorder(guild_id, left)
        return removed

    def _remove(self, guild_id: int, slug: str) -> bool:
        cur = self.db._conn.execute(
            "DELETE FROM rule_categories WHERE guild_id = ? AND slug = ?", (guild_id, slug)
        )
        self.db._conn.commit()
        return cur.rowcount > 0

    async def reorder(self, guild_id: int, slugs: list[str]) -> None:
        await self.db._run(self._reorder, guild_id, slugs)

    def _reorder(self, guild_id, slugs) -> None:
        for pos, slug in enumerate(slugs):
            self.db._conn.execute(
                "UPDATE rule_categories SET position = ? WHERE guild_id = ? AND slug = ?",
                (pos, guild_id, slug),
            )
        self.db._conn.commit()
