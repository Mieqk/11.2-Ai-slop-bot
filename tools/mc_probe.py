"""Диагностика связи с Minecraft-сервером: python tools/mc_probe.py --host .. --port .. --password ..

Проверяет ровно то, чем пользуется бот (тот же RconConnection из drivers/rcon.py):
доступность RCON, авторизацию, ответ на безопасную команду и разбор ника из
`list`. Никаких наказаний не выдаёт — только read-only команды.

Выход: 0 — всё хорошо, можно включать `drivers.active: "rcon"`;
      1 — нашёл проблему, ниже написано какую.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # import drivers.* как в боте

from drivers.base import DriverError  # noqa: E402
from drivers.rcon import RconConnection  # noqa: E402

SAFE_COMMANDS = ["list", "version", "gamerule maxCommandChainLength"]
UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
# законный ник: до 16 символов; кириллица тоже нужна — на оффлайн-серверах такие играют
MC_NAME_RE = re.compile(r"^[A-Za-z0-9_А-Яа-яЁё]{1,16}$")
NOT_A_PLAYER = {"no", "not", "none", "null", "players", "player", "online", "empty", "нет", "игроков"}


def parse_players(text: str) -> list[tuple[str, str | None]]:
    """Разбирает вывод Paper/Spigot `list`: «Игроки онлайн: 2: [Steve, Alex]» и
    формы с UUID (AdvancedBan/Tablist-плагины)."""
    """Игроки из вывода `list`: «[Steve, Alex]», «[BadSteve <uuid>]», «[Имя]».

    Мусор («[(no players)]», пустые списки) отфильтрован: имя обязано выглядеть
    как ник Minecraft, иначе подсказка для /gamelink будет врать.
    """
    players: list[tuple[str, str | None]] = []
    for chunk in re.findall(r"\[([^\]]+)\]", text):
        for item in chunk.split(","):
            item = item.strip()
            if not item:
                continue
            uuid = UUID_RE.search(item)
            rest = UUID_RE.sub(" ", item)
            name = next((w.strip("{}()") for w in rest.split() if MC_NAME_RE.match(w.strip("{}()"))), None)
            if name and name.lower() not in NOT_A_PLAYER:
                players.append((name, uuid.group(0) if uuid else None))
    return players


async def probe(host: str, port: int, password: str, timeout: float, extra: list[str]) -> int:
    print(f"→ {host}:{port} — TCP")
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except (TimeoutError, OSError) as exc:
        print(f"✗ порт недоступен: {exc.__class__.__name__}: {exc}")
        print("  частые причины: enable-rcon=false; другие rcon.ip/rcon.port; фаервол;")
        print("  бот живёт не на той машине, что сервер (RCON без шифрования")
        print("  наружу не выставляют — см. README, раздел про деплой).")
        return 1
    writer.close()
    print("  открыт")

    conn = RconConnection(host, port, password, timeout=timeout)
    try:
        print(f"→ авторизация ({'Source/Minecraft-рукопожатие'})")
        await asyncio.wait_for(conn.connect(), timeout + 2)
        print("  принято")
    except DriverError as exc:
        print(f"✗ {exc}")
        print("  проверьте rcon.password в server.properties и совпадает ли он с config.json")
        await conn.close()
        return 1
    except (TimeoutError, OSError, asyncio.IncompleteReadError) as exc:
        print(f"✗ рукопожатие не завершилось: {exc.__class__.__name__}")
        await conn.close()
        return 1

    code = 0
    for cmd in ["list", *extra]:
        try:
            out = await asyncio.wait_for(conn.command(cmd), timeout + 2)
        except DriverError as exc:
            print(f"✗ команда `{cmd}` не прошла: {exc}")
            code = 1
            continue
        shown = " ".join(out.split())
        print(f"→ `{cmd}` → {shown[:180] or '(пустой ответ — для части команд это норма)'}")
        if cmd == "list":
            players = parse_players(out)
            if players:
                print("  игроки: " + ", ".join(n + (f" [{u[:8]}…]" if u else "") for n, u in players[:12]))
                if all(u is None for _, u in players):
                    print("  ℹ UUID в выводе нет: `/gamelink` пишите ником, а для бана от"
                          " переименования вводите UUID вручную (`/bangame ник:<uuid>`)")
            else:
                print("  ℹ вывод `list` не разобрался: не страшно, но и авто-подсказать ник не сможем")
    await conn.close()
    print("\nитог: RCON работает — в config.json ставьте \"drivers\": {\"active\": \"rcon\", …}")
    return code


def main() -> int:
    ap = argparse.ArgumentParser(description="Проверка RCON для модер-бота")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=25575)
    ap.add_argument("--password", default="", help="пусто — возьмёт из MC_RCON_PASSWORD")
    ap.add_argument("--timeout", type=float, default=5.0)
    ap.add_argument("--cmd", action="append", default=[], help="дополнительная read-only команда (можно несколько)")
    args = ap.parse_args()
    import os

    password = args.password or os.getenv("MC_RCON_PASSWORD", "")
    if not password:
        print("Нужен пароль: --password или переменная MC_RCON_PASSWORD")
        return 1
    return asyncio.run(probe(args.host, args.port, password, args.timeout, args.cmd))


if __name__ == "__main__":
    raise SystemExit(main())
