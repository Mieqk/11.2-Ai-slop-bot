"""SQLite storage for moderation cases, mutes and warn counters."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core import timeutil

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    moderator_id INTEGER,
    action      TEXT    NOT NULL,
    reason      TEXT,
    issued_at   TEXT    NOT NULL,
    expires_at  TEXT,
    lifted_at   TEXT,
    lifted_by   INTEGER,
    extra       TEXT
);
CREATE INDEX IF NOT EXISTS idx_cases_guild_user ON cases(guild_id, user_id);
CREATE INDEX IF NOT EXISTS idx_cases_expiry ON cases(action, expires_at, lifted_at);

CREATE TABLE IF NOT EXISTS mute_overwrites (
    case_id  INTEGER NOT NULL,
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    deny_send    INTEGER NOT NULL DEFAULT 0,
    deny_connect INTEGER NOT NULL DEFAULT 0,
    deny_speak   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (case_id, channel_id)
);

CREATE TABLE IF NOT EXISTS guild_config (
    guild_id INTEGER PRIMARY KEY,
    config   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rule_categories (
    guild_id  INTEGER NOT NULL,
    slug      TEXT    NOT NULL,
    emoji     TEXT    NOT NULL DEFAULT '',
    title     TEXT    NOT NULL,
    color     INTEGER NOT NULL DEFAULT 16753920,
    position  INTEGER NOT NULL DEFAULT 0,
    pages     TEXT    NOT NULL DEFAULT '[]',
    PRIMARY KEY (guild_id, slug)
);

CREATE TABLE IF NOT EXISTS game_links (
    guild_id  INTEGER NOT NULL,
    user_id   INTEGER NOT NULL,
    driver    TEXT    NOT NULL,
    game_id   TEXT    NOT NULL,
    note      TEXT,
    PRIMARY KEY (guild_id, user_id, driver)
);
"""

#: Per-guild overrides. `None` / 0 / "" means "inherit from config.json".
#: Warn thresholds are deliberately *not* inherited-able: an empty list means
#: "no escalation on this guild", overriding the global list.
DEFAULT_GUILD_CONFIG: dict[str, Any] = {
    "feedback_hint": "",
    "log_channel_id": 0,
    "mute_role_id": 0,
    "dm_target": None,
    "dry_run": None,
    "driver": "",
    "rules_channel_id": 0,
    "rules_message_id": 0,
    "complaint_channel_id": 0,
    "staff_role_id": 0,
    "chat_channel_id": 0,
    "voice_create_channel_id": 0,
    "voice_public_category_id": 0,
    "voice_private_category_id": 0,
    "warn_thresholds": None,
}


class Database:
    """Thin async wrapper over a blocking sqlite3 connection."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._lock = asyncio.Lock()

    def close(self) -> None:
        self._conn.close()

    async def _run(self, fn, *args):
        async with self._lock:
            return await asyncio.to_thread(fn, *args)

    # ---------------------------------------------------------------- setup
    async def setup(self) -> None:
        await self._run(self._conn.executescript, SCHEMA)
        self._conn.commit()

    # ---------------------------------------------------------------- cases
    async def add_case(
        self,
        *,
        guild_id: int,
        user_id: int,
        moderator_id: int | None,
        action: str,
        reason: str | None,
        expires_at: datetime | None,
        extra: dict | None = None,
    ) -> int:
        return await self._run(
            self._add_case,
            guild_id,
            user_id,
            moderator_id,
            action,
            reason,
            datetime.now(timezone.utc).isoformat(),
            timeutil.iso(expires_at),
            json.dumps(extra or {}, ensure_ascii=False),
        )

    def _add_case(self, *args) -> int:
        cur = self._conn.execute(
            "INSERT INTO cases (guild_id, user_id, moderator_id, action, reason,"
            " issued_at, expires_at, extra) VALUES (?,?,?,?,?,?,?,?)",
            args,
        )
        self._conn.commit()
        if cur.lastrowid is None:  # для INSERT сюда не приходим; пусть будет громко
            raise RuntimeError("SQLite не вернул id записи — база не отвечает?")
        return int(cur.lastrowid)

    async def get_case(self, case_id: int) -> sqlite3.Row | None:
        return await self._run(
            lambda: self._conn.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()
        )

    async def update_case(self, case_id: int, **fields) -> None:
        allowed = {"reason", "expires_at", "lifted_at", "lifted_by", "extra", "action"}
        for key in fields:
            if key not in allowed:
                raise KeyError(key)
        await self._run(self._update_case, case_id, fields)

    def _update_case(self, case_id: int, fields: dict) -> None:
        sets = ", ".join(f"{k} = ?" for k in fields)
        self._conn.execute(
            f"UPDATE cases SET {sets} WHERE id = ?", (*fields.values(), case_id)
        )
        self._conn.commit()

    async def lift(self, case_id: int, moderator_id: int | None) -> None:
        await self._run(
            self._lift, case_id, datetime.now(timezone.utc).isoformat(), moderator_id
        )

    def _lift(self, case_id: int, now: str, moderator_id: int | None) -> None:
        self._conn.execute(
            "UPDATE cases SET lifted_at = ?, lifted_by = ? WHERE id = ?",
            (now, moderator_id, case_id),
        )
        self._conn.commit()

    async def active_game_case(self, guild_id: int, action: str, game_id: str) -> sqlite3.Row | None:
        """Активная запись этого действия, выданная именно этому игроку игры."""
        return await self._run(self._active_game_case, guild_id, action, game_id.strip().lower())

    def _active_game_case(self, guild_id: int, action: str, game_id: str):
        return self._conn.execute(
            "SELECT * FROM cases WHERE guild_id = ? AND action = ? AND lifted_at IS NULL"
            " AND lower(json_extract(extra, '$.game_id')) = ? ORDER BY id DESC LIMIT 1",
            (guild_id, action, game_id),
        ).fetchone()

    async def active_cases(
        self, guild_id: int | None = None, action: str | None = None
    ) -> list[sqlite3.Row]:
        return await self._run(self._active_cases, guild_id, action)

    def _active_cases(self, guild_id, action) -> list[sqlite3.Row]:
        sql = "SELECT * FROM cases WHERE lifted_at IS NULL"
        args: tuple = ()
        if guild_id is not None:
            sql += " AND guild_id = ?"
            args += (guild_id,)
        if action:
            sql += " AND action = ?"
            args += (action,)
        return self._conn.execute(sql, args).fetchall()

    async def user_cases(self, guild_id: int, user_id: int, limit: int = 25) -> list[sqlite3.Row]:
        return await self._run(
            lambda: self._conn.execute(
                "SELECT * FROM cases WHERE guild_id = ? AND user_id = ?"
                " ORDER BY id DESC LIMIT ?",
                (guild_id, user_id, limit),
            ).fetchall()
        )

    async def recent_cases(self, guild_id: int, limit: int = 25) -> list[sqlite3.Row]:
        return await self._run(
            lambda: self._conn.execute(
                "SELECT * FROM cases WHERE guild_id = ? ORDER BY id DESC LIMIT ?",
                (guild_id, limit),
            ).fetchall()
        )

    async def search_cases(
        self, guild_id: int, *, limit: int = 50, action: str | None = None
    ) -> list[sqlite3.Row]:
        return await self._run(self._search_cases, guild_id, limit, action)

    def _search_cases(self, guild_id: int, limit: int, action: str | None):
        sql = "SELECT * FROM cases WHERE guild_id = ?"
        args: list = [guild_id]
        if action:
            sql += " AND action = ?"
            args.append(action)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        return self._conn.execute(sql, args).fetchall()

    # ------------------------------------------------------- channel overwrites
    async def save_overwrites(self, case_id: int, guild_id: int, channel_ids: list[int]) -> None:
        """Remember which channels a legacy mute wrote denials into, so /unmute can reset them."""
        await self._run(self._save_overwrites, case_id, guild_id, channel_ids)

    def _save_overwrites(self, case_id: int, guild_id: int, channel_ids: list[int]) -> None:
        self._conn.executemany(
            "INSERT OR REPLACE INTO mute_overwrites (case_id, guild_id, channel_id)"
            " VALUES (?,?,?)",
            [(case_id, guild_id, cid) for cid in channel_ids],
        )
        self._conn.commit()

    async def take_overwrites(self, case_id: int) -> list[sqlite3.Row]:
        return await self._run(
            lambda: self._conn.execute(
                "SELECT * FROM mute_overwrites WHERE case_id = ?", (case_id,)
            ).fetchall()
        )

    async def forget_overwrites(self, case_id: int) -> None:
        await self._run(self._forget_overwrites, case_id)

    def _forget_overwrites(self, case_id: int) -> None:
        self._conn.execute("DELETE FROM mute_overwrites WHERE case_id = ?", (case_id,))
        self._conn.commit()

    # ------------------------------------------------------------- guild config
    async def get_guild_config(self, guild_id: int) -> dict:
        row = await self._run(
            lambda: self._conn.execute(
                "SELECT config FROM guild_config WHERE guild_id = ?", (guild_id,)
            ).fetchone()
        )
        cfg = dict(DEFAULT_GUILD_CONFIG)
        if row:
            cfg.update(json.loads(row["config"]))
        return cfg

    async def set_guild_config(self, guild_id: int, patch: dict) -> dict:
        cfg = await self.get_guild_config(guild_id)
        cfg.update(patch)
        await self._run(self._set_guild_config, guild_id, json.dumps(cfg, ensure_ascii=False))
        return cfg

    def _set_guild_config(self, guild_id: int, config: str) -> None:
        self._conn.execute(
            "INSERT INTO guild_config (guild_id, config) VALUES (?,?)"
            " ON CONFLICT(guild_id) DO UPDATE SET config = excluded.config",
            (guild_id, config),
        )
        self._conn.commit()

    # -------------------------------------------------------------- warn stats
    async def active_warns(self, guild_id: int, user_id: int) -> list[sqlite3.Row]:
        """Warns that still count: never revoked and not past their own expiry.

        Each warn carries its own `expires_at`, so the "how many warns" question
        must respect it — otherwise a 7-day warn keeps feeding the escalation
        ladder forever.
        """
        return await self._run(self._active_warns, guild_id, user_id)

    def _active_warns(self, guild_id: int, user_id: int) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM cases WHERE guild_id = ? AND user_id = ?"
            " AND action = 'warn' AND lifted_at IS NULL"
            " AND (expires_at IS NULL OR expires_at > ?) ORDER BY id",
            (guild_id, user_id, datetime.now(timezone.utc).isoformat()),
        ).fetchall()

    async def expired_warns(self, now_iso: str) -> list[sqlite3.Row]:
        """Warns whose own timer ran out — the sweep closes them in the DB too."""
        return await self._run(self._expired_warns, now_iso)

    def _expired_warns(self, now_iso: str) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM cases WHERE action = 'warn' AND lifted_at IS NULL"
            " AND expires_at IS NOT NULL AND expires_at <= ?",
            (now_iso,),
        ).fetchall()

    # ------------------------------------------------------------- game links
    async def find_discord_by_game(self, guild_id: int, driver: str, game_id: str) -> int | None:
        row = await self._run(self._find_discord, guild_id, driver, game_id)
        return int(row["user_id"]) if row else None

    def _find_discord(self, guild_id, driver, game_id):
        return self._conn.execute(
            "SELECT user_id FROM game_links WHERE guild_id=? AND driver=? AND game_id=?",
            (guild_id, driver, game_id),
        ).fetchone()

    async def set_game_link(
        self, guild_id: int, user_id: int, driver: str, game_id: str, note: str = ""
    ) -> None:
        await self._run(self._set_game_link, guild_id, user_id, driver, game_id, note)

    def _set_game_link(self, guild_id, user_id, driver, game_id, note) -> None:
        self._conn.execute(
            "INSERT INTO game_links (guild_id, user_id, driver, game_id, note)"
            " VALUES (?,?,?,?,?)"
            " ON CONFLICT(guild_id, user_id, driver) DO UPDATE SET game_id = excluded.game_id,"
            " note = excluded.note",
            (guild_id, user_id, driver, game_id, note),
        )
        self._conn.commit()

    async def get_game_link(
        self, guild_id: int, user_id: int, driver: str
    ) -> str | None:
        row = await self._run(self._get_game_link, guild_id, user_id, driver)
        return row["game_id"] if row else None

    def _get_game_link(self, guild_id, user_id, driver):
        return self._conn.execute(
            "SELECT game_id FROM game_links WHERE guild_id=? AND user_id=? AND driver=?",
            (guild_id, user_id, driver),
        ).fetchone()

    async def find_discord_by_nick(self, guild_id: int, game_id: str) -> int | None:
        """Участник Discord по нику игры — по любому драйверу (gamefeed не знает, какой)."""
        row = await self._run(self._find_discord_by_nick, guild_id, game_id)
        return int(row["user_id"]) if row else None

    def _find_discord_by_nick(self, guild_id: int, game_id: str):
        return self._conn.execute(
            "SELECT user_id FROM game_links WHERE guild_id = ? AND lower(game_id) = lower(?) LIMIT 1",
            (guild_id, game_id.strip()),
        ).fetchone()

