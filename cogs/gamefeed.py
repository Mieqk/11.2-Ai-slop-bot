"""cogs/gamefeed.py — события наказаний, выданных В ИГРЕ.

Модератор набрал `/tempban Steve 7d ксы` в игре или забанил из консоли — в канале
наказаний должна появиться та же карточка, что и от команд бота.

Источники (`gamefeed.source` в config.json):
  "log"  — дочитываем хвост `log_file` (смещение храним в памяти, ротацию
           переносим с начала файла), строки разбирает core/gamefeed.py;
  "rcon" — периодически гоняем `poll_cmd` (например `banlist`) и сравниваем
           состав с прошлым снимком: появился — бан, исчез — разбан.

По умолчанию выключено (`gamefeed.enabled: false`): включать должен админ,
который знает путь к логам своего сервера.

Защита от двойных карточек:
  * «эхо-окно» 120 с — когда наказание выдал сам бот (hook `note_self_issued`);
  * хэш строки на 10 минут — один и тот же лог не разбирается дважды;
  * два одинаковых события за 2 минуты — не заводим вторую запись;
  * `unban`/`unmute`/`pardon` из лога не создают новую запись, а закрывают
    активную, выданную ботом (иначе журнал врал бы «висит мут», которого нет).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import time
from collections import OrderedDict, deque
from datetime import timedelta
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks

from core import audit, gamefeed, timeutil
from core.helpers import ModInputError
from core.moderation import GameTarget

log = logging.getLogger("modbot.gamefeed")

MAX_EVENTS_PER_TICK = 5     # шквал при первой читке большого лога не льём в канал
DEDUP_TTL_S = 600           # хэш строки
ECHO_TTL_S = 120            # «это я сам выдал»
DUPLICATE_WINDOW_S = 120    # такое же событие от другого лога-строки
NAMES_RE = re.compile(r"\b[A-Za-z0-9_]{1,16}\b")


class GameFeed(commands.GroupCog, group_name="gamefeed"):
    """Хвост лога сервера / опрос RCON -> карточка в канал наказаний."""

    def __init__(self, bot):
        self.bot = bot
        self.cfg: dict = bot.cfg.get("gamefeed") or {}
        self.patterns, self.pattern_errors = self._parse_patterns(self.cfg.get("patterns"))
        self.offsets: dict[str, int] = {}
        self.seen_lines: OrderedDict[str, float] = OrderedDict()
        self.echo: OrderedDict[str, float] = OrderedDict()
        self.last_lists: dict[str, set[str]] = {}
        self.recent: deque[str] = deque(maxlen=30)
        self.enabled = bool(self.cfg.get("enabled", False))
        self.events_handled = 0
        # пол: чаще 5 с долбить лог/консоль игры незачем, а RCON-опрос так вообще
        self.interval_s = max(5, int(self.cfg.get("interval_s") or 5))
        self._next_run = 0.0
        self.poll_loop.change_interval(seconds=self.interval_s)
        self.poll_loop.start()

    async def cog_load(self) -> None:
        """Сказать админу, почему поток молчит, — вместо тихого «ничего не пришло»."""
        if self.pattern_errors:
            log.warning("gamefeed: %s битых паттернов: %s", len(self.pattern_errors),
                        "; ".join(self.pattern_errors[:3]))
        if not self.enabled:
            log.info("gamefeed: выключен (поставьте gamefeed.enabled: true в config.json)")
            return
        source = str(self.cfg.get("source", "log"))
        if source == "rcon":
            log.info("gamefeed: опрос `%s` каждые %s с", self.cfg.get("poll_cmd", "banlist"), self.interval_s)
        elif not str(self.cfg.get("log_file") or ""):
            log.warning("gamefeed: включен, но gamefeed.log_file пуст — читать нечего")
        elif not Path(str(self.cfg["log_file"])).exists():
            log.warning(
                "gamefeed: файл лога %s не найден. Если бот и сервер на разных машинах,"
                " лог физически недоступен — используйте source: 'rcon'",
                self.cfg["log_file"],
            )

    def cog_unload(self):
        self.poll_loop.cancel()

    # ------------------------------------------------------------- public hooks
    def note_self_issued(self, action: str, nick: str, reason: str | None = None) -> None:
        """Вызывается движком: это наказание отправил в игру сам бот."""
        key = _echo_key(action, nick, reason)
        self.echo[key] = time.monotonic()
        while len(self.echo) > 500:
            self.echo.popitem(last=False)

    @staticmethod
    def _parse_patterns(specs: list[dict] | None) -> tuple[list[gamefeed.Pattern], list[str]]:
        patterns, errors = [], []
        for spec in (specs or gamefeed.DEFAULT_PATTERNS):
            try:
                patterns.append(gamefeed.build_pattern(
                    str(spec.get("name") or str(spec["regex"])[:24]), str(spec["action"]),
                    str(spec["regex"]), permanent=bool(spec.get("permanent", False)),
                ))
            except gamefeed.PatternError as exc:
                errors.append(f"{spec.get('name', '?')}: {exc}")
        return patterns, errors

    def _line_is_new(self, raw: str) -> bool:
        now = time.monotonic()
        self._gc(self.seen_lines, DEDUP_TTL_S, now)
        digest = hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:16]
        if digest in self.seen_lines:
            return False
        self.seen_lines[digest] = now
        return True

    def _is_echo(self, event: gamefeed.GameEvent) -> bool:
        now = time.monotonic()
        self._gc(self.echo, ECHO_TTL_S, now)
        return _echo_key(event.action, event.nick, event.reason) in self.echo

    @staticmethod
    def _gc(store: OrderedDict[str, float], ttl: int, now: float) -> None:
        for key in [k for k, ts in store.items() if now - ts > ttl]:
            store.pop(key, None)

    # ------------------------------------------------------------------ loop
    @tasks.loop(seconds=5)
    async def poll_loop(self) -> None:
        if not self.enabled or not self.patterns:
            return
        interval = self.interval_s
        if time.monotonic() < self._next_run:
            return
        self._next_run = time.monotonic() + interval
        for guild in self.bot.guilds:
            try:
                if str(self.cfg.get("source", "log")) == "rcon":
                    await self._tick_poll(guild)
                else:
                    await self._tick_log(guild)
            except Exception:  # noqa: BLE001 — один сервер не роняет цикл
                log.exception("gamefeed: тик на «%s» упал", guild.name)

    @poll_loop.before_loop
    async def wait_ready(self) -> None:
        await self.bot.wait_until_ready()

    # -------------------------------------------------------------- sources
    async def _tick_log(self, guild: discord.Guild) -> None:
        path = str(self.cfg.get("log_file") or "")
        if not path:
            return
        chunk = await asyncio.to_thread(self._read_tail, path)
        if not chunk:
            return
        events = gamefeed.parse_multiline(chunk, self.patterns)[:MAX_EVENTS_PER_TICK]
        for event in events:
            await self._handle(guild, event)

    def _read_tail(self, path: str) -> str:
        """Дочитать с сохранённого смещения; файл урезали/ротировали — с начала."""
        try:
            size = os.path.getsize(path)
        except OSError:
            return ""
        start = self.offsets.get(path)
        if start is None:
            start = size                      # первый проход: только новое, не историю
        elif size < start:
            start = 0
        with open(path, encoding="utf-8", errors="replace") as handle:
            handle.seek(start)
            data = handle.read()
        self.offsets[path] = start + len(data.encode("utf-8", "replace"))
        return data

    async def _tick_poll(self, guild: discord.Guild) -> None:
        command = str(self.cfg.get("poll_cmd") or "banlist")
        try:
            _, driver = await self.bot.mod.resolve_driver(guild.id)
            out = await driver.consult(command)  # read-only запрос к консоли игры
        except Exception as exc:  # noqa: BLE001 — сервер может быть офлайн
            log.info("gamefeed: опрос `%s` не удался: %s", command, exc)
            return
        found = set(NAMES_RE.findall(out or ""))
        key = f"{guild.id}:{command}"
        previous = self.last_lists.get(key)
        self.last_lists[key] = found
        if previous is None:
            log.info("gamefeed: снимок `%s` — %s игроков(а), карточек не будет", command, len(found))
            return
        base = gamefeed.ACTION_ALIASES.get(str(self.cfg.get("poll_action") or "ban"), "bangame")
        lifted = {"bangame": "unbangame", "mutegame": "unmutegame"}.get(base, base)
        for nick in sorted(found - previous)[:MAX_EVENTS_PER_TICK]:
            await self._handle(guild, gamefeed.GameEvent(
                action=base, nick=nick, reason=None,
                raw=f"poll `{command}`: {nick} появился в списке",
            ))
        for nick in sorted(previous - found)[:MAX_EVENTS_PER_TICK]:
            await self._handle(guild, gamefeed.GameEvent(
                action=lifted, nick=nick, reason=None,
                raw=f"poll `{command}`: {nick} исчез из списка",
            ))

    # ------------------------------------------------------------- processing
    async def _handle(self, guild: discord.Guild, event: gamefeed.GameEvent) -> None:
        if not self._line_is_new(event.raw) or self._is_echo(event):
            return
        if self._recent_duplicate(event):
            return
        self.events_handled += 1
        if event.action in gamefeed.LIFT_ACTIONS:
            closed = await self._close_matching(guild, event)
            if closed:
                self._remember(event)
            else:
                log.info("gamefeed: снимать нечего (%s %s)", event.action, event.nick)
            return
        await self._create(guild, event)
        self._remember(event)

    def _recent_duplicate(self, event: gamefeed.GameEvent) -> bool:
        return f"{event.action}|{event.nick.lower()}" in set(self.recent)

    def _remember(self, event: gamefeed.GameEvent) -> None:
        self.recent.append(f"{event.action}|{event.nick.lower()}")

    async def _create(self, guild: discord.Guild, event: gamefeed.GameEvent) -> None:
        member = await self._find_member(guild, event.nick)
        delta = event.delta or timedelta.max
        case_id = await self.bot.db.add_case(
            guild_id=guild.id,
            user_id=member.id if member else 0,
            moderator_id=None,
            action=event.action,
            reason=event.reason or f"выдано в игре{' ' + event.actor if event.actor else ''}",
            expires_at=None if event.delta is None else discord.utils.utcnow() + event.delta,
            extra={"source": "game", "game_id": event.nick, "game_actor": event.actor,
                   "seconds": 0 if event.delta is None else int(event.delta.total_seconds()),
                   "raw": event.raw[:200]},
        )
        target = member or GameTarget(event.nick)
        embed = audit.case_embed(
            event.action, target=target, moderator=None,
            reason=event.reason or (f"модератор в игре: {event.actor}" if event.actor else "причина не указана"),
            duration=timeutil.humanize_long(delta),
            note=f"из игры: {event.raw[:150]}",
            extra_fields={"КЕМ В ИГРЕ": event.actor} if event.actor else None,
        )
        await self.bot.mod.post(guild, embed)
        log.info("gamefeed: %s %s -> запись #%s", event.action, event.nick, case_id)
        if member is not None:
            await self.bot.mod.notify(member, audit.punishment_embed(
                event.action, timeutil.humanize_long(delta),
                event.reason or "см. журнал модерации", guild_name=guild.name, target=member,
            ))

    async def _close_matching(self, guild: discord.Guild, event: gamefeed.GameEvent) -> bool:
        """Разбан/размьют в игре закрывает активную запись бота."""
        wanted = gamefeed.LIFT_ACTIONS[event.action]
        case = await self.bot.db.active_game_case(guild.id, wanted, event.nick)
        if case is None:
            return False
        member = guild.get_member(int(case["user_id"])) or GameTarget(event.nick)
        embed = audit.case_embed(
            event.action, target=member, moderator=None,
            reason=f"снято в игре{' ' + event.actor if event.actor else ''}",
            note=f"из игры: {event.raw[:150]}",
        )
        await self.bot.db.lift(int(case["id"]), None)
        await self.bot.mod.post(guild, embed)
        log.info("gamefeed: запись #%s закрыта (снято в игре)", case["id"])
        return True

    async def _find_member(self, guild: discord.Guild, nick: str):
        user_id = await self.bot.db.find_discord_by_nick(guild.id, nick)
        return guild.get_member(int(user_id)) if user_id else None

    # ------------------------------------------------------------- commands
    @app_commands.command(name="status", description="Состояние потока событий игры")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def status(self, inter: discord.Interaction) -> None:
        path = str(self.cfg.get("log_file") or "")
        lines = [
            f"включен: {'да' if self.enabled else 'нет — `gamefeed.enabled: true` в config.json'}",
            f"источник: `{self.cfg.get('source', 'log')}` · интервал {self.cfg.get('interval_s', 5)} с",
            f"файл: `{path or '—'}`" + (f" · смещение {self.offsets.get(path, 0)} б" if path else ""),
            f"паттернов: рабочих {len(self.patterns)}, сломанных {len(self.pattern_errors)} ·"
            f" обработано событий: {self.events_handled}",
            f"помеченных строк: {len(self.seen_lines)} · окно эха: {len(self.echo)}",
        ]
        if self.pattern_errors:
            lines.append("⚠️ " + "; ".join(self.pattern_errors[:3]))
        if self.recent:
            lines.append("последние: " + ", ".join(sorted(set(self.recent))[-5:]))
        await inter.response.send_message("\n".join(lines)[:1900], ephemeral=True)

    @app_commands.command(name="try", description="Как парсер разберёт строку лога")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(строка="Строка из logs/latest.log или вывода плагина")
    async def try_parse(self, inter: discord.Interaction, строка: str) -> None:
        event = gamefeed.parse_line(строка, self.patterns)
        if event is None:
            raise ModInputError(
                "паттерны не совпали. Добавьте свой в `gamefeed.patterns`"
                " config.json (нужна группа `(?P<nick>...)`) — и проверьте её этим же `/gamefeed try`."
            )
        await inter.response.send_message(
            f"→ `{event.action}` · ник {audit.code(event.nick)} · срок {event.duration_text or 'не указан'}"
            f" · причина: {event.reason or '—'}" + (f" · кем: {event.actor}" if event.actor else ""),
            ephemeral=True,
        )


def _echo_key(action: str, nick: str, reason: str | None) -> str:
    return f"{action}|{nick.lower()}|{' '.join((reason or '').split()).lower()}"


async def setup(bot) -> None:
    await bot.add_cog(GameFeed(bot))
