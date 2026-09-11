"""
Discord-бот модерации: муты (ДС + игра), варны, баны, отчёты в канал.

Запуск:  python bot.py                 # config.json рядом
Проверка: python bot.py --check         # конфиг и список команд, без входа в сеть

Соединение держит `run_forever`: дискордовый шлюз переподключается сам, но
фатальные сетевые ошибки (DNS, обрыв, `GatewayReconnectError`) обычно роняют
процесс — на бесплатных хостингах это происходит регулярно. Поэтому падение
считается нормальным положением дел: бот перезапускается с экспоненциальной
паузой, а настройки reconnect/keepalive лежат в config.json.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import random
import sys
import time

import discord
from discord import app_commands
from discord.ext import commands

from core import config as config_loader
from core import keepalive
from core.db import Database
from core.moderation import Moderation
from core.rules_repo import RulesRepo
from drivers.base import BaseDriver, registry

log = logging.getLogger("modbot")

log = logging.getLogger("modbot")

#: паузы фоновых повторов загрузки команд (сек): сначала часто, потом редко
SYNC_RETRY_DELAYS = (20, 60, 300, 900) + (1800,) * 8

COGS = ("cogs.moderation", "cogs.warns", "cogs.records", "cogs.rules", "cogs.media",
        "cogs.voice", "cogs.gamefeed", "cogs.expiry")

#: cog -> имя tasks.loop: их стартует __init__, а `--check` обязан убить, иначе
#: asyncio печатает «Task exception was never retrieved» вместо чистого вывода
BACKGROUND_LOOPS = {"Expiry": "sweep", "Voice": "gc_loop", "GameFeed": "poll_loop"}

#: retrying these is pointless — the problem is the token/intents, not the wire
FATAL = (discord.LoginFailure, discord.PrivilegedIntentsRequired)

#: network-shaped failures worth restarting for. discord.py reconnects the
#: gateway on its own (own exponential backoff inside `Client.connect`); these
#: are the cases that escape that and would otherwise kill the process —
#: DNS/connect storms surface as OSError, a dead websocket as ConnectionClosed.
TRANSIENT = (
    discord.ConnectionClosed,
    OSError,          # ConnectionReset/Refused/TimeoutError and aiohttp's ClientConnectorError
    asyncio.TimeoutError,
)



#: что делать при конкретной ошибке загрузки команд (Discord отвечает кодом статуса)
SYNC_HINTS = {
    401: "токен не принят — проверьте DISCORD_TOKEN / Reset Token в Developer Portal",
    403: ("у приложения нет доступа: бот приглашён без scope `applications.commands` "
          "или его роль/доступ убрали с сервера. Перепригласите по ссылке из SETUP.md §2.3"),
    404: "неверный Application (бот удалён из Discord Developer Portal?)",
    429: "слишком частые синхронизации (лимит на запись команд) — подождите и "
         "не перезапускайте процесс часто",
    400: "Discord отверг структуру команды (имена опций, длина, дубликаты) — смотри текст ошибки выше",
}


class _VoiceNoiseFilter(logging.Filter):
    """Выбрасывает только «voice will NOT be supported» (PyNaCl/davey)."""

    def filter(self, record: logging.LogRecord) -> bool:
        return "voice will NOT be supported" not in record.getMessage()


class ModBot(commands.Bot):
    def __init__(self, cfg):
        intents = discord.Intents.default()
        intents.members = True  # timeout/deafen state must stay correct across reconnects
        super().__init__(
            # только slash-команды: !префикс не реализован, а он ещё и выводит
            # предупреждение про message_content (см. discord/ext/commands/bot.py)
            command_prefix=commands.when_mentioned,
            intents=intents,
            help_command=None,
            case_insensitive=True,
        )
        self.cfg = cfg
        self.db = Database(cfg["db_path"])
        self.registry = registry
        self.mod = Moderation(self, cfg)
        self.rules = RulesRepo(self.db)
        self.http_keepalive = None
        # False, пока Discord не принял загрузку команд (см. _sync_commands)
        self.commands_synced = False
        self._sync_task = None
        # выставляется из on_ready: нужен --diagnose, у которого нет ready-ивентов клиента
        self.ready_signal: asyncio.Event | None = None
        self._sync_scope = "глобально"

    def _sync_target(self):
        """Куда синхронизировать: один сервер (мгновенно) или глобально.

        Для бота на одном сервере guild-sync лучше: команды видны сразу, лимит
        записи отдельный от глобального, и нет часа на распространение.
        """
        raw = str(self.cfg.get("sync_guild_id") or "").strip()
        if not raw:
            return None
        try:
            guild_id = int(raw)
        except ValueError:
            log.warning("sync_guild_id = %r — не id сервера, синхронизирую глобально", raw)
            return None
        guild = self.get_guild(guild_id)
        if guild is None:
            log.warning("сервера %s нет среди тех, где я состою (%s) — ухожу на глобальную синхронизацию",
                        guild_id, ", ".join(str(g.id) for g in self.guilds) or "ни одного")
            return None
        return discord.Object(id=guild_id)

    async def _sync_commands(self) -> bool:
        """Загрузить slash-команды и НЕ ронять старт из-за отказа.

        Глобальная синхронизация имеет жёсткий лимит на запись, а Discord может
        ответить 403 (нет доступа у скоупа) или 429. Пускать из-за этого процесс
        и перезапускаться — значит только ухудшать (каждый старт = новая попытка
        записи). Поэтому: одна попытка здесь, повторения — в фоне.
        """
        self._sync_scope = "сервер" if self._sync_target() is not None else "глобально"
        try:
            await self.tree.sync(guild=self._sync_target())
        except discord.app_commands.CommandSyncFailure as exc:
            self._report_sync_failure(exc)
        except discord.HTTPException as exc:
            log.error("sync slash-команд не прошёл (%s): %s", getattr(exc, "status", "?"),
                      " ".join(str(exc).split())[:300])
        else:
            self.commands_synced = True
            log.info("slash-команды синхронизированы (%s): %s", self._sync_scope,
                     ", ".join(sorted(c.name for c in self.tree.get_commands())))
            return True

        self.commands_synced = False
        self._sync_task = self.loop.create_task(self._sync_retries())
        return False

    def _report_sync_failure(self, exc: discord.app_commands.CommandSyncFailure) -> None:
        status = getattr(exc, "status", 0)
        log.error("slash-команды не загружены (попытка 1): %s", " ".join(str(exc).split())[:500])
        hint = SYNC_HINTS.get(status)
        log.error("→ что делать: %s", hint or "смотрите текст ошибки выше и SETUP.md §2.3")
        if status == 403:
            log.error("→ проще всего: выгнать бота с сервера и пригласить заново ссылкой "
                      "со scope `bot applications.commands` (§2.3 в SETUP.md)")

    async def _sync_retries(self) -> None:
        """Фоновые повторные попытки: чинится само, как только доступ вернули."""
        for delay in SYNC_RETRY_DELAYS:
            await asyncio.sleep(delay)
            try:
                await self.tree.sync(guild=self._sync_target())
            except (discord.app_commands.CommandSyncFailure, discord.HTTPException):
                continue
            self.commands_synced = True
            log.info("slash-команды загружены с повторной попытки")
            return
        log.error("slash-команды так и не загрузились — сервер Discord отклоняет запись; "
                  "проверьте приглашение бота (scope applications.commands) и права")

    async def setup_hook(self) -> None:
        await self.db.setup()
        self.registry.discover()
        for cog in COGS:
            try:
                await self.load_extension(cog)
            except Exception:  # noqa: BLE001
                log.exception("cog %s не загрузился", cog)
                raise
        await self._sync_commands()  # глобальные команды; guild= — только для серверных
        self.http_keepalive = await keepalive.start(self, self.cfg)


    async def start(self, *args, **kwargs) -> None:
        await super().start(*args, **kwargs)

    async def on_ready(self) -> None:
        """Кто я, где я и видны ли мои команды — это и есть ответы на «почему /ban не появляется»."""
        log.info("бот: %s (id %s) · серверов: %s · команд в дереве: %s · загружено Discord'ом: %s",
                 self.user, self.user.id, len(self.guilds), len(self.tree.get_commands()),
                 "да" if self.commands_synced else "НЕТ")
        ready = getattr(self, "ready_signal", None)
        if ready is not None:
            ready.set()
        if not self.guilds:
            log.warning("бот не состоит ни в одном сервере: пригласите его ссылкой из SETUP.md §2.3 "
                        "(scope `bot applications.commands`) — пока не увидите /ban в списке команд")
        elif not self.commands_synced:
            log.warning("серверов: %s, но Discord не принял команды: см. ERROR выше (обычно 403 = "
                        "в приглашении не было scope `applications.commands`)", len(self.guilds))

    async def on_resume(self) -> None:
        log.info("связь с Discord восстановлена без переподключения сессии")

    async def on_error(self, event_method: str, *args) -> None:
        # a handler must never kill the process; free hosts restart slowly
        log.exception("в обработчике %s упало исключение", event_method)

    async def close(self) -> None:
        if self._sync_task is not None:
            self._sync_task.cancel()
            self._sync_task = None
        if self.http_keepalive is not None:
            await self.http_keepalive.cleanup()
            self.http_keepalive = None
        driver: BaseDriver
        for driver in list(self.mod.drivers.values()):
            try:
                await driver.teardown()
            except Exception:  # noqa: BLE001
                log.exception("teardown драйвера %s не удался", driver.id)
        self.db.close()
        await super().close()




INVITE_HINT = ("пригласите бота заново ссылкой со scope `bot applications.commands` "
               "и permissions=1099784350740 (SETUP.md §2.3)")


def diagnosis_lines(user, guilds, command_count: int, sync_error: Exception | None = None) -> list[str]:
    """Что напечатает `--diagnose`: отдельная функция, чтобы это было чем покрыть.

    Три разных «команд не видно»: бота нет в серверах · серверы есть, но Discord
    отверг загрузку · всё ок (значит клиент/кэш Discord или права авторизации).
    """
    out = [f"бот: {user} (id {user.id})", f"команд в дереве бота: {command_count}"]
    if not guilds:
        out.append("серверов: 0 → бот ни в одном сервере не состоит (не приглашён или его удалили)")
        out.append("что делать: " + INVITE_HINT)
        return out
    out.append(f"серверов: {len(guilds)} — " + ", ".join(g.name for g in guilds[:10]))
    if sync_error is not None:
        status = getattr(sync_error, "status", 0)
        out.append(f"Discord отверг загрузку команд (HTTP {status}): "
                   + " ".join(str(sync_error).split())[:300])
        out.append("что делать: " + (SYNC_HINTS.get(status) or INVITE_HINT))
        return out
    out.append("команды приняты Discord'ом: " + str(command_count))
    out.append("если в списке всё ещё пусто: обновите клиент (Ctrl+R / перезаход), затем "
               "Server Settings → Integrations → этот бот: в авторизации должны быть "
               "bot + applications.commands, и не должно быть ограничения по каналам")
    return out


async def diagnose(cfg) -> int:
    """Одиночный запуск: кто я, где я, почему команд не видно. Без супервизора.

    `async with bot` только вызывает setup_hook и НЕ логинит (см. Client.__aenter__),
    поэтому заходим явно через `start reconnect=False`: иначе wait_until_ready
    висел бы вечно.
    """
    print("подключение к Discord…")
    bot = ModBot(cfg)
    bot.ready_signal = asyncio.Event()

    async def runner() -> None:
        await bot.start(cfg["token"], reconnect=False)

    task = asyncio.create_task(runner())
    try:
        await asyncio.wait_for(bot.ready_signal.wait(), timeout=45)
        error: Exception | None = None
        accepted: list = []
        try:
            accepted = await bot.tree.sync(guild=bot._sync_target())
        except (discord.app_commands.CommandSyncFailure, discord.HTTPException) as exc:
            error = exc
        for line in diagnosis_lines(bot.user, bot.guilds, len(bot.tree.get_commands()), error):
            print(line)
        if accepted:
            print(f"принято и загружено: {len(accepted)} команд(ы)")
        return 1 if error else 0
    except asyncio.TimeoutError:
        # старт мог упасть раньше (неверный токен/нет сети): поднимем его причину
        if task.done() and task.exception() is not None:
            failure = task.exception()
            print(f"не смог подключиться: {failure.__class__.__name__}: "
                  + " ".join(str(failure).split())[:200])
            if isinstance(failure, discord.LoginFailure):
                print("что делать: токен неверный/просроченный — Reset Token в Developer Portal "
                      "и обновите DISCORD_TOKEN")
            return 1
        print("не подключился за 45 с: проверьте исходящий TCP 443 до discord.com и gateway.discord.gg")
        return 1
    finally:
        await bot.close()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


def parse_args():
    ap = argparse.ArgumentParser(description="Discord moderation bot")
    ap.add_argument("--config", default="config.json", help="путь к config.json")
    ap.add_argument("--check", action="store_true", help="проверить конфиг и команды, не подключаясь")
    ap.add_argument("--once", action="store_true", help="без супервизора: одно падение = выход (под systemd/docker)")
    ap.add_argument("--diagnose", action="store_true",
                    help="зайти, показать кто я / на каких серверах и почему команды не видны, выйти")
    ap.add_argument("--verbose", action="store_true")
    return ap.parse_args()


def fmt_params(cmd) -> str:
    """Подсказка аргументов для --check; опциональные помечены «?»."""
    return ", ".join(
        f"{p.display_name or p.name}{'?' if not p.required else ''}" for p in cmd.parameters
    )


async def run_check(cfg) -> int:
    """Load every cog offline and print the command table — catches wiring bugs."""
    bot = ModBot(cfg)
    await bot.db.setup()
    bot.registry.discover()
    for cog in COGS:
        await bot.load_extension(cog)
    print(f"Драйверы игр: {', '.join(bot.registry.ids())}")
    print("Slash-команды:")
    for cmd in sorted(bot.tree.get_commands(), key=lambda c: c.name):
        if isinstance(cmd, app_commands.Group):  # у группы нет ни parameters, ни callback
            print(f"  /{cmd.name} — {cmd.description or ''}")
            for sub in cmd.commands:
                print(f"      /{cmd.name} {sub.name}({fmt_params(sub)})")
            continue
        print(f"  /{cmd.name}({fmt_params(cmd)}) — {cmd.description}")
    # фоновые циклы стартовали до логина: гасим их до выхода, иначе asyncio
    # печатает «Task exception was never retrieved» вместо чистого --check
    for name, loop_name in BACKGROUND_LOOPS.items():
        background = bot.get_cog(name)
        task = getattr(background, loop_name, None) if background else None
        if task is not None:
            task.cancel()
    await asyncio.sleep(0.1)
    bot.db.close()
    return 0


def next_delay(plan: list[float], attempt: int) -> float:
    """Backoff for restart #attempt: grow, then cap — a flaky host should not be
    hammered every second, nor should 3 a.m. outages be retried for hours."""
    if attempt < 1:
        raise ValueError("attempt is 1-based")
    return min(plan[min(attempt - 1, len(plan) - 1)], 900)


def run_forever(cfg) -> None:
    """Start the bot, and start it again if the network eats it alive.

    discord.py already re-handshakes the gateway on its own; what it cannot
    survive is a DNS outage, a host that sleeps the container, or its own retry
    budget running out. On a free tier those are Tuesday, so a crash is treated
    as a restart-able event: exponential backoff with jitter, a fresh process
    loop, and no attempt counter penalty once the bot has stayed up a while.
    """
    plan = [float(x) for x in (cfg.get("reconnect_delays") or [5, 15, 30, 60, 120, 300])]
    max_attempts = int(cfg.get("reconnect_attempts") or 0)  # 0 = forever
    stable_after = float(cfg.get("reconnect_reset_after") or 300)
    attempt = 0

    while True:
        started = time.monotonic()
        bot = ModBot(cfg)
        try:
            # `Client.run` swallows KeyboardInterrupt and returns -> clean exit
            bot.run(cfg["token"], log_handler=None)
            log.info("бот остановлен штатно (Ctrl+C / SIGTERM)")
            return
        except FATAL as exc:
            sys.exit(f"Переподключение не поможет: {exc}")
        except BaseException as exc:  # noqa: BLE001 - network or bug, restart anyway
            if isinstance(exc, (KeyboardInterrupt, SystemExit, MemoryError)):
                raise
            uptime = time.monotonic() - started
            if uptime >= stable_after:
                attempt = 0  # it was healthy for a while — this is a new incident
            attempt += 1
            if max_attempts and attempt > max_attempts:
                sys.exit(f"Бот не поднялся за {max_attempts} попыток: {exc}")
            delay = next_delay(plan, attempt) + random.uniform(0, 5)
            log.warning(
                "%s (аптайм %.0fс) — перезапуск через %.0fс (попытка %s): %s: %s",
                "связь потеряна" if isinstance(exc, TRANSIENT) else "ПАДЕНИЕ",
                uptime, delay, attempt, exc.__class__.__name__,
                " ".join(str(exc).split())[:300] or "(без сообщения)",
            )
            log.info("стек перезапуска (запустите с --verbose, чтобы видеть его всегда)", exc_info=exc)
            time.sleep(delay)



def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    # голос нами не используется, а Discord-либа на старте орёт про PyNaCl/davey —
    # приглушаем, чтобы админ не искал проблему там, где её нет
    for noisy in ("discord.http", "discord.gateway", "discord.client", "discord.ext.commands"):
        logging.getLogger(noisy).setLevel(logging.WARNING if not args.verbose else logging.DEBUG)
    # голос мы не используем вовсе (бот модерирует таймаутами/RCON), а discord.py при
    # каждом старте пишет про PyNaCl/davey — ровно эти две строки и скроем,
    # предупреждения по теме прав/сети остаются видимыми
    logging.getLogger("discord.client").addFilter(_VoiceNoiseFilter())
    cfg = config_loader.load(args.config, require_token=not args.check)
    if args.check:
        raise SystemExit(asyncio.run(run_check(cfg)))
    if args.diagnose:
        raise SystemExit(asyncio.run(diagnose(cfg)))
    if args.once:
        ModBot(cfg).run(cfg["token"], log_handler=None)
        return
    run_forever(cfg)


if __name__ == "__main__":
    main()
