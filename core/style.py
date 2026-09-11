"""House visual language shared by the info modules (rules, media posts).

The rule/info cards look like the reference bots: `[ЗАГОЛОВОК]` in brackets and
upper case, a colored accent bar, `·`-bulleted body and numbered clauses shown
as `inline code`. Punishment cards use core/audit.py instead — different job,
different density — but both pull their palette from here.
"""

from __future__ import annotations

#: палитра «дома» — та же, что в референсах
COLOR_BORDEAUX = 0x8B0000  # пояснительная записка, важное/серьёзное
COLOR_AMBER = 0xFFA500  # правила, подсказки
COLOR_CYAN = 0x00A8FC  # медиа-карточки («Фотография»)
COLOR_GREEN = 0x4CAF7D  # успех
COLOR_INFO = 0x7C87C4  # нейтральное

BULLET = "·"
CLAUSE_QUOTE = "`{}`"


def head(title: str) -> str:
    """"правила голосовых каналов" -> "[ПРАВИЛА ГОЛОСОВЫХ КАНАЛЫ]"."""
    text = " ".join(str(title).split())
    return f"[{text.upper()}]"


def bullet(text: str) -> str:
    return f"{BULLET} {text}"


def clause(number: str, text: str) -> str:
    """Нумерованный пункт, как в рефе: `2.1 Текст` серым плашечкой."""
    return CLAUSE_QUOTE.format(f"{number} {text}".strip()) if number else CLAUSE_QUOTE.format(text)


def pages_label(page: int, total: int) -> str:
    return f"{page + 1} / {total}"


def footer(*parts: str) -> str:
    return " · ".join(p for p in parts if p)
