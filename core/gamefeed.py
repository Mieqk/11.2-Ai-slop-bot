"""Разбор событий игры: наказания, выданные НЕ ботом (админом в игре/консоли).

Бот сам пишет карточки на свои `/mutegame`/`/bangame`/`/warn`. Но если модератор
забанил прямо на сервере, в канале должно появиться то же самое — для этого
строки лога/консоли разбираются здесь на `GameEvent`, а cog уже решает, откуда их
брать (файл лога или периодический RCON-опрос) и как не задублировать собственное
наказание.

Всё, что зависит от конкретного плагина, — это `patterns`: список regex'ов с
именованными группами. Дефолты покрывают ваниль (`issued server command`),
EssentialsX и AdvancedBan/LiteBans-рассылки; для своего плагина добавьте паттерн
в config.json, а проверить можно прямо в Discord: `/gamefeed try`.

Модуль не трогает Discord и БД — чистые функции, чтобы это было чем покрыть.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import timedelta

from core import timeutil

#: действие из паттерна -> действие_case, который понимает журнал бота
ACTION_ALIASES = {
    "ban": "bangame", "bangame": "bangame", "tempban": "bangame",
    "mute": "mutegame", "mutegame": "mutegame", "tempmute": "mutegame",
    "warn": "warn", "warning": "warn", "kick": "kick",
    "unban": "unbangame", "pardon": "unbangame", "unbangame": "unbangame",
    "unmute": "unmutegame", "unmutegame": "unmutegame",
}

#: «снятие из игры» -> действие, чью активную запись оно закрывает
LIFT_ACTIONS = {"unbangame": "bangame", "unmutegame": "mutegame"}

NICK_GROUP = "nick"
OPTIONAL_GROUPS = ("duration", "reason", "actor")
GROUP_RE = re.compile(r"\(\?P<(\w+)>")


@dataclass(frozen=True)
class GameEvent:
    """Одно наказание, увиденное в игре."""

    action: str                      # уже в терминах журнала: bangame / mutegame / warn / …
    nick: str
    delta: timedelta | None = None   # None = срок не указан (ванильный ban — навсегда)
    reason: str | None = None
    actor: str | None = None
    raw: str = ""
    key: str = field(default="", compare=False, hash=False)

    def __post_init__(self) -> None:
        # object.__setattr__ нужен, потому что dataclass frozen
        if not self.key:
            object.__setattr__(self, "key", f"{self.action}|{self.nick.lower()}|{self.reason or ''}")

    @property
    def permanent(self) -> bool:
        return self.delta is None

    @property
    def duration_text(self) -> str | None:
        if self.delta is None:
            return None
        return timeutil.humanize_long(self.delta)


class PatternError(ValueError):
    """Паттерн нельзя использовать: нет ника, битое regex, неизвестное действие."""


@dataclass(frozen=True)
class Pattern:
    name: str
    action: str
    regex: re.Pattern
    #: срок можно задать и константой ("ban" в ванили всегда навсегда)
    permanent: bool = False

    def match(self, line: str) -> GameEvent | None:
        found = self.regex.search(line)
        if not found:
            return None
        groups = found.groupdict()
        nick = (groups.get(NICK_GROUP) or "").strip()
        if not nick:
            return None
        delta = None if self.permanent else parse_duration_text(groups.get("duration"))
        return GameEvent(
            action=self.action,
            nick=nick,
            delta=delta,
            reason=_clean(groups.get("reason"), reason=True),
            actor=_clean(groups.get("actor1") or groups.get("actor2")),
            raw=line.strip()[:300],
        )


_REASON_PREFIX = re.compile(r"^(?:по\s+)?причин[ае]\s*[:\-]?\s*", re.I)


def case_matches_nick(case, nick: str) -> bool:
    """Запись выдавалась этому нику? Сравниваем по JSON-полю, а не подстрокой:
    «Notch» не должен совпасть с «BigNotch»."""
    try:
        stored = str(json.loads(case["extra"] or "{}").get("game_id") or "")
    except (ValueError, TypeError, KeyError):
        return False
    return bool(stored) and stored.lower() == nick.strip().lower()


def _clean(value: str | None, *, reason: bool = False) -> str | None:
    text = " ".join((value or "").split())
    if reason:
        text = _REASON_PREFIX.sub("", text)
    return text.strip(" .:-") or None


#: «7d», «1h30m», «2w», «30 минут», «навсегда», «0» — всё, что пишут плагины
_TOKEN = re.compile(
    r"(\d+)\s*(weeks?|w|нед\w*|days?|d|дн\w*|день|hours?|h|час\w*|ч|minutes?|mins?|m|мин\w*|seconds?|secs?|s|сек\w*)",
    re.I,
)
_LONG = {"weeks": 604800, "week": 604800, "w": 604800, "days": 86400, "day": 86400, "d": 86400,
         "hours": 3600, "hour": 3600, "h": 3600, "minutes": 60, "minute": 60, "min": 60, "m": 60,
         "seconds": 1, "second": 1, "sec": 1, "s": 1}


def _unit_seconds(unit: str) -> int | None:
    low = unit.lower()
    if low in _LONG:
        return _LONG[low]
    if low.startswith("нед"):
        return 604800
    if low.startswith("дн") or low.startswith("день") or low == "д":
        return 86400
    if low.startswith("час") or low == "ч":
        return 3600
    if low.startswith("мин"):
        return 60
    if low.startswith("сек"):
        return 1
    return None
FOREVER = {"permanent", "perm", "forever", "навсегда", "∞"}


def parse_duration_text(value: str | None) -> timedelta | None:
    """Срок из строки плагина. None — если в строке срока нет (не путать с «навсегда»)."""
    text = (value or "").strip().lower()
    if not text:
        return None
    if text in FOREVER:
        return timedelta.max
    total = 0
    for amount, unit in _TOKEN.findall(text):
        seconds = _unit_seconds(unit)
        if seconds:
            total += int(amount) * seconds
    if total:
        return timedelta(seconds=min(total, 356 * 86400))
    if re.fullmatch(r"\d+", text):
        # голое число: плагины понимают его по-разному (секунды/минуты/тики).
        # Угадывать нечем — считаем секундами и помечаем в raw у cog'а.
        return timedelta(seconds=min(int(text), 356 * 86400))
    return None


def build_pattern(name: str, action: str, regex: str, *, permanent: bool = False) -> Pattern:
    """Собрать и проверить паттерн. Ошибки — внятные, чтобы админ чинил конфиг сам."""
    mapped = ACTION_ALIASES.get(action.strip().lower())
    if mapped is None:
        raise PatternError(
            f" действие «{action}»: журнал знает {', '.join(sorted(set(ACTION_ALIASES)))}"
        )
    try:
        compiled = re.compile(regex)
    except re.error as exc:
        raise PatternError(f"regex не компилируется ({name}): {exc}") from None
    groups = set(GROUP_RE.findall(regex))
    if NICK_GROUP not in groups:
        raise PatternError(f"в regex «{name}» нет группы (?P<{NICK_GROUP}>...) — непонятно, о ком речь")
    unknown = groups - {NICK_GROUP, "actor1", "actor2", *OPTIONAL_GROUPS}
    if unknown:
        raise PatternError(
            f"неизвестные группы в «{name}»: {', '.join(sorted(unknown))}; "
            f"можно {NICK_GROUP}, {', '.join(OPTIONAL_GROUPS)}"
        )
    return Pattern(name=name, action=mapped, regex=compiled, permanent=permanent)


#: Дефолты покрывают ванильный лог команд и типовые рассылки плагинов. Для
#: своего плагина добавьте паттерн в config (`gamefeed.patterns`) и проверьте его
#: прямо в Discord: `/gamefeed try`. Порядок = приоритет: сначала более точные.
DEFAULT_PATTERNS: tuple[dict, ...] = (
    # Буккит пишет команду участника в лог:
    #   [12:00:00] [Server thread/INFO]: Admin issued server command: /tempban Steve 7d ксы
    {
        "name": "команда tempban",
        "action": "tempban",
        "regex": r"issued server command:\s*/temp(?:ip)?ban\s+(?P<nick>\w{1,16})\s+(?P<duration>\S+)(?:\s+(?P<reason>.*))?",
    },
    {
        "name": "команда tempmute",
        "action": "tempmute",
        "regex": r"issued server command:\s*/tempmute\s+(?P<nick>\w{1,16})\s+(?P<duration>\S+)(?:\s+(?P<reason>.*))?",
    },
    {
        "name": "команда mute",
        "action": "mute",
        "regex": r"issued server command:\s*/mute\s+(?P<nick>\w{1,16})(?:\s+(?P<reason>.*))?",
        "permanent": True,
    },
    {
        "name": "команда ban",
        "action": "ban",
        "regex": r"issued server command:\s*/ban(?:-ip)?\s+(?P<nick>[0-9a-f-]{36}|\w{1,16})(?:\s+(?P<reason>.*))?",
        "permanent": True,
    },
    {
        "name": "команда warn",
        "action": "warn",
        "regex": r"issued server command:\s*/warn\s+(?P<nick>\w{1,16})(?:\s+(?P<reason>.*))?",
    },
    {
        "name": "команда pardon",
        "action": "pardon",
        "regex": r"issued server command:\s*/pardon(?:-ip)?\s+(?P<nick>[0-9a-f-]{36}|\w{1,16})",
    },
    {
        "name": "команда unmute",
        "action": "unmute",
        "regex": r"issued server command:\s*/unmute\s+(?P<nick>\w{1,16})",
    },
    # Рассылки плагинов в консоль/чат (текст различается — эти четыре ловят типовые).
    {
        "name": "рассылка: banned by",
        "action": "ban",
        "regex": r"(?P<nick>\w{1,16})\s+(?:has\s+been|was|был)\s+(?:temporarily\s+|временно\s+)?banned"
                 r"(?:\s+by\s+(?P<actor1>\w{1,16}))?"
                 r"(?:\s+for\s+(?P<duration>\d+\s*[a-zа-я]{1,7}(?:\s+\d+\s*[a-zа-я]{1,7})*))?"
                 r"(?:\s+by\s+(?P<actor2>\w{1,16}))?"
                 r"(?:[^\w\n]*(?P<reason>.+))?",
    },
    {
        "name": "рассылка: muted by",
        "action": "mute",
        "regex": r"(?P<nick>\w{1,16})\s+(?:has\s+been|was|был)\s+(?:temporarily\s+|временно\s+)?(?:muted|замучен)"
                 r"(?:\s+by\s+(?P<actor1>\w{1,16}))?"
                 r"(?:\s+(?:for|на)\s+(?P<duration>\d+\s*[a-zа-я]{1,7}(?:\s+\d+\s*[a-zа-я]{1,7})*))?"
                 r"(?:\s+by\s+(?P<actor2>\w{1,16}))?"
                 r"(?:[^\w\n]*(?P<reason>.+))?",
    },
    {
        "name": "рассылка: забанен",
        "action": "ban",
        "regex": r"(?P<nick>\w{1,16})\s+был\s+(?:временно\s+)?забанен"
                 r"(?:\s+(?:на|до)\s+(?P<duration>\d+\s*[a-zа-я]{1,7}(?:\s+\d+\s*[a-zа-я]{1,7})*))?"
                 r"(?:[^\w\n]*(?P<reason>.+))?",
    },
    {
        "name": "рассылка: замучен",
        "action": "mute",
        "regex": r"(?P<nick>\w{1,16})\s+был\s+(?:временно\s+)?замучен"
                 r"(?:\s+на\s+(?P<duration>\d+\s*[a-zа-я]{1,7}(?:\s+\d+\s*[a-zа-я]{1,7})*))?"
                 r"(?:[^\w\n]*(?P<reason>.+))?",
    },
    {
        "name": "рассылка RU: разбанен",
        "action": "pardon",
        "regex": r"(?P<nick>\w{1,16})\s+(?:был\s+)?разбанен",
    },
)


def load_patterns(specs: list[dict] | None) -> list[Pattern]:
    """Собрать паттерны из конфига; пустой/None — дефолты."""
    patterns = []
    for spec in (specs or DEFAULT_PATTERNS):
        patterns.append(
            build_pattern(
                str(spec.get("name") or spec["regex"][:30]),
                str(spec["action"]),
                str(spec["regex"]),
                permanent=bool(spec.get("permanent", False)),
            )
        )
    return patterns


def parse_line(line: str, patterns: list[Pattern]) -> GameEvent | None:
    """Первое совпадение по списку (порядок = приоритет)."""
    for pattern in patterns:
        event = pattern.match(line)
        if event:
            return event
    return None


def parse_multiline(chunk: str, patterns: list[Pattern]) -> list[GameEvent]:
    out = []
    for line in chunk.splitlines():
        event = parse_line(line, patterns)
        if event:
            out.append(event)
    return out
