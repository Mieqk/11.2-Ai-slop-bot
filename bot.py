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



class ModBot(commands.Bot):
    def __init__(self, cfg):
        intents = discord.Intents.default()
        intents.members = True  # timeout/deafen state must stay correct across reconnects
        super().__init__(
            command_prefix=commands.when_mentioned_or("!"),
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

    async def setup_hook(self) -> None:
        await self.db.setup()
        self.registry.discover()
        for cog in COGS:
            try:
                await self.load_extension(cog)
            except Exception:  # noqa: BLE001
                log.exception("cog %s не загрузился", cog)
                raise
        await self.tree.sync()  # глобальные команды; guild= — только для серверных
        log.info("slash-команды синхронизированы: %s", ", ".join(sorted(c.name for c in self.tree.get_commands())))
        self.http_keepalive = await keepalive.start(self, self.cfg)


    async def start(self, *args, **kwargs) -> None:
        await super().start(*args, **kwargs)

    async def on_resume(self) -> None:
        log.info("связь с Discord восстановлена без переподключения сессии")

    async def on_error(self, event_method: str, *args) -> None:
        # a handler must never kill the process; free hosts restart slowly
        log.exception("в обработчике %s упало исключение", event_method)

    async def close(self) -> None:
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



def parse_args():
    ap = argparse.ArgumentParser(description="Discord moderation bot")
    ap.add_argument("--config", default="config.json", help="путь к config.json")
    ap.add_argument("--check", action="store_true", help="проверить конфиг и команды, не подключаясь")
    ap.add_argument("--once", action="store_true", help="без супервизора: одно падение = выход (под systemd/docker)")
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
                "%s аптайм %.0fс — перезапуск через %.0fс (попытка %s): %s",
                "связь потеряна" if isinstance(exc, TRANSIENT) else "ПАДЕНИЕ",
                uptime, delay, attempt, exc.__class__.__name__,
            )
            log.debug("подробности перезапуска", exc_info=exc)
            time.sleep(delay)



def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    for noisy in ("discord.http", "discord.gateway", "discord.client"):
        logging.getLogger(noisy).setLevel(logging.WARNING if not args.verbose else logging.DEBUG)
    cfg = config_loader.load(args.config, require_token=not args.check)
    if args.check:
        raise SystemExit(asyncio.run(run_check(cfg)))
    if args.once:
        ModBot(cfg).run(cfg["token"], log_handler=None)
        return
    run_forever(cfg)


if __name__ == "__main__":
    main()
