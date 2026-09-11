"""Plugin interface for game-side mutes.

Add support for a new game by dropping a module in `drivers/` that subclasses
`BaseDriver` and decorates it with `@registry.register`. Nothing else in the
bot has to change: `/mutegame` looks up the driver enabled for the guild and
calls resolve() -> mute(). `drivers/` also carries the periodic re-apply task
so a mute survives game reconnects, and the expiry task lifts it later.
"""

from __future__ import annotations

import importlib
import pkgutil
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:  # pragma: no cover
    import discord
    from core.db import Database


class DriverError(RuntimeError):
    """Raised by drivers for expected, moderator-facing failures."""


class BaseDriver(ABC):
    """One driver == one game/engine transport (RCON, SourceBans, HTTP API...)."""

    #: unique id used in config, e.g. "rcon", "sourcebans"
    id: ClassVar[str] = ""
    #: human readable name shown in /mutegame errors
    title: ClassVar[str] = ""
    #: can the driver silence voice (True) or only in-game chat (False)?
    supports_voice: ClassVar[bool] = False
    #: does the driver need re-applying after the player reconnects?
    idempotent_apply: ClassVar[bool] = False
    #: можно ли наказать, knowing только ник игры (участника Discord нет/не привязан)
    by_nick_only: ClassVar[bool] = False

    def __init__(self, db: Database, config: dict) -> None:
        self.db = db
        self.config = config

    async def setup(self) -> None:
        """Optional async init (open sockets, validate credentials)."""

    async def teardown(self) -> None:
        """Optional cleanup (close connections)."""

    @property
    def ok(self) -> bool:
        """False when the driver is configured but unreachable/not configured."""
        return True

    @abstractmethod
    async def resolve(
        self, guild: discord.Guild, member: discord.Member, identifier: str | None
    ) -> str:
        """Map a Discord member to the in-game id (SteamID, UUID, client id).

        `identifier` is the optional value typed by the moderator. Raise
        DriverError when the player cannot be located.
        """

    @abstractmethod
    async def mute(
        self, guild: discord.Guild, target: str, seconds: int, reason: str
    ) -> str:
        """Apply the mute. Return a short note for the modlog embed."""

    @abstractmethod
    async def unmute(self, guild: discord.Guild, target: str) -> str:
        """Lift the mute. Return a short note for the modlog embed."""


class Registry:
    """Holds every discovered driver class keyed by `.id`."""

    def __init__(self) -> None:
        self._drivers: dict[str, type[BaseDriver]] = {}

    def register(self, cls: type[BaseDriver]) -> type[BaseDriver]:
        if not cls.id:
            raise ValueError(f"{cls.__name__} needs a non-empty .id")
        self._drivers[cls.id] = cls
        return cls

    def ids(self) -> list[str]:
        return sorted(self._drivers)

    def get(self, driver_id: str) -> type[BaseDriver] | None:
        return self._drivers.get(driver_id)

    def descriptions(self) -> str:
        return "\n".join(
            f"`{key}` — {cls.title}" for key, cls in sorted(self._drivers.items())
        )

    def discover(self) -> None:
        """Import every module in the `drivers` package so decorators register."""
        package = importlib.import_module("drivers")
        for info in pkgutil.iter_modules(package.__path__):
            if info.name.startswith("_") or info.name == "base":
                continue
            importlib.import_module(f"drivers.{info.name}")


registry = Registry()
