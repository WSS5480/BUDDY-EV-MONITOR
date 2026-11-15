"""Tiny key-value store.

Production (Render free plan has no disk): Postgres via DATABASE_URL, e.g. a free Supabase
project. Use Supabase's *session pooler* connection string (IPv4). Local dev / no DATABASE_URL:
a SQLite file, which on Render free is wiped on every restart (the status page warns about it).

Everything the app must remember lives here: the Tesla refresh token (single-use, rotates),
the virtual-key private key, Claude connector OAuth clients/tokens, owner web sessions and
pending approvals. Secrets (Tesla tokens, the private key) are encrypted with
SECRET_KEY before they get here.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from typing import Any

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()


class Store:
    def __init__(self) -> None:
        self.kind = "postgres" if DATABASE_URL else "sqlite"
        self._pool = None
        self._sqlite: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()
        self.error: str | None = None  # set when the database can't be reached
        self._ready = False

    async def open(self) -> None:
        if self.kind == "postgres":
            from psycopg_pool import AsyncConnectionPool  # noqa: PLC0415

            # prepare_threshold=None keeps it working through Supabase's transaction pooler too.
            self._pool = AsyncConnectionPool(
                DATABASE_URL,
                min_size=0,
                max_size=4,
                timeout=15,
                open=False,
                check=AsyncConnectionPool.check_connection,  # drop connections the pooler closed
                kwargs={"prepare_threshold": None, "autocommit": True},
            )
            await self._pool.open(wait=False)
            try:
                await self._ensure_table()
            except Exception as e:  # keep the app up so the owner page can say what's wrong
                self.error = f"Can't reach the database ({e.__class__.__name__}). If it's Supabase, it may be paused."
        else:
            path = os.environ.get("SQLITE_PATH", "bem.sqlite3")
            self._sqlite = sqlite3.connect(path, check_same_thread=False)
            self._sqlite.execute(
                "CREATE TABLE IF NOT EXISTS bem_kv (k TEXT PRIMARY KEY, v TEXT NOT NULL, expires_at REAL)"
            )
            self._sqlite.commit()

    async def _ensure_table(self) -> None:
        assert self._pool is not None
        async with self._pool.connection() as conn:
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS bem_kv ("
                " k TEXT PRIMARY KEY, v JSONB NOT NULL, expires_at DOUBLE PRECISION)"
            )
            # Supabase exposes tables in `public` through its REST API to the anon and
            # authenticated roles. Lock this one down: RLS on with no policies (the table owner
            # used by this app skips RLS) and no grants for those roles.
            await conn.execute("ALTER TABLE bem_kv ENABLE ROW LEVEL SECURITY")
            cur = await conn.execute(
                "SELECT string_agg(quote_ident(rolname), ', ') FROM pg_roles"
                " WHERE rolname IN ('anon', 'authenticated', 'service_role')"
            )
            roles = (await cur.fetchone())[0]
            if roles:  # plain Postgres has none of these roles
                await conn.execute(f"REVOKE ALL ON TABLE bem_kv FROM {roles}")
        self._ready = True
        self.error = None

    async def _pg(self):
        """Pool connection, creating the table first if startup couldn't."""
        assert self._pool is not None
        if not self._ready:
            await self._ensure_table()
        return self._pool.connection()

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
        if self._sqlite is not None:
            self._sqlite.close()

    async def get(self, key: str) -> Any | None:
        now = time.time()
        if self.kind == "postgres":
            assert self._pool is not None
            async with await self._pg() as conn:
                cur = await conn.execute("SELECT v, expires_at FROM bem_kv WHERE k = %s", (key,))
                row = await cur.fetchone()
            if not row:
                return None
            value, exp = row
            if exp is not None and exp < now:
                await self.delete(key)
                return None
            return value
        async with self._lock:
            assert self._sqlite is not None
            row = self._sqlite.execute("SELECT v, expires_at FROM bem_kv WHERE k = ?", (key,)).fetchone()
        if not row:
            return None
        if row[1] is not None and row[1] < now:
            await self.delete(key)
            return None
        return json.loads(row[0])

    async def set(self, key: str, value: Any, ttl: float | None = None) -> None:
        exp = time.time() + ttl if ttl else None
        if self.kind == "postgres":
            from psycopg.types.json import Jsonb  # noqa: PLC0415

            assert self._pool is not None
            async with await self._pg() as conn:
                await conn.execute(
                    "INSERT INTO bem_kv (k, v, expires_at) VALUES (%s, %s, %s) "
                    "ON CONFLICT (k) DO UPDATE SET v = EXCLUDED.v, expires_at = EXCLUDED.expires_at",
                    (key, Jsonb(value), exp),
                )
            return
        async with self._lock:
            assert self._sqlite is not None
            self._sqlite.execute(
                "INSERT INTO bem_kv (k, v, expires_at) VALUES (?, ?, ?) "
                "ON CONFLICT (k) DO UPDATE SET v = excluded.v, expires_at = excluded.expires_at",
                (key, json.dumps(value), exp),
            )
            self._sqlite.commit()

    async def add(self, key: str, value: Any) -> bool:
        """Insert only if the key doesn't exist yet. True if this call stored it."""
        if self.kind == "postgres":
            from psycopg.types.json import Jsonb  # noqa: PLC0415

            assert self._pool is not None
            async with await self._pg() as conn:
                cur = await conn.execute(
                    "INSERT INTO bem_kv (k, v, expires_at) VALUES (%s, %s, NULL) ON CONFLICT (k) DO NOTHING",
                    (key, Jsonb(value)),
                )
                return cur.rowcount == 1
        async with self._lock:
            assert self._sqlite is not None
            cur = self._sqlite.execute(
                "INSERT INTO bem_kv (k, v, expires_at) VALUES (?, ?, NULL) ON CONFLICT (k) DO NOTHING",
                (key, json.dumps(value)),
            )
            self._sqlite.commit()
            return cur.rowcount == 1

    async def ping(self) -> None:
        """Touch the database (keeps a free Supabase project from pausing)."""
        if self.kind == "postgres":
            assert self._pool is not None
            async with await self._pg() as conn:
                await conn.execute("SELECT 1")

    async def take(self, key: str) -> Any | None:
        """Get and delete in one step (single-use items such as codes)."""
        if self.kind == "postgres":
            assert self._pool is not None
            async with await self._pg() as conn:
                cur = await conn.execute("DELETE FROM bem_kv WHERE k = %s RETURNING v, expires_at", (key,))
                row = await cur.fetchone()
            if not row or (row[1] is not None and row[1] < time.time()):
                return None
            return row[0]
        async with self._lock:
            assert self._sqlite is not None
            row = self._sqlite.execute("SELECT v, expires_at FROM bem_kv WHERE k = ?", (key,)).fetchone()
            self._sqlite.execute("DELETE FROM bem_kv WHERE k = ?", (key,))
            self._sqlite.commit()
        if not row or (row[1] is not None and row[1] < time.time()):
            return None
        return json.loads(row[0])

    async def expires_at(self, key: str) -> float | None:
        if self.kind == "postgres":
            assert self._pool is not None
            async with await self._pg() as conn:
                cur = await conn.execute("SELECT expires_at FROM bem_kv WHERE k = %s", (key,))
                row = await cur.fetchone()
            return row[0] if row else None
        async with self._lock:
            assert self._sqlite is not None
            row = self._sqlite.execute("SELECT expires_at FROM bem_kv WHERE k = ?", (key,)).fetchone()
        return row[0] if row else None

    async def delete(self, key: str) -> None:
        if self.kind == "postgres":
            assert self._pool is not None
            async with await self._pg() as conn:
                await conn.execute("DELETE FROM bem_kv WHERE k = %s", (key,))
            return
        async with self._lock:
            assert self._sqlite is not None
            self._sqlite.execute("DELETE FROM bem_kv WHERE k = ?", (key,))
            self._sqlite.commit()

    async def delete_prefix(self, prefix: str) -> None:
        pattern = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        if self.kind == "postgres":
            assert self._pool is not None
            async with await self._pg() as conn:
                await conn.execute("DELETE FROM bem_kv WHERE k LIKE %s ESCAPE '\\'", (pattern,))
            return
        async with self._lock:
            assert self._sqlite is not None
            self._sqlite.execute("DELETE FROM bem_kv WHERE k LIKE ? ESCAPE '\\'", (pattern,))
            self._sqlite.commit()

    async def count_prefix(self, prefix: str) -> int:
        pattern = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        if self.kind == "postgres":
            assert self._pool is not None
            async with await self._pg() as conn:
                cur = await conn.execute("SELECT count(*) FROM bem_kv WHERE k LIKE %s ESCAPE '\\'", (pattern,))
                row = await cur.fetchone()
            return int(row[0]) if row else 0
        async with self._lock:
            assert self._sqlite is not None
            row = self._sqlite.execute("SELECT count(*) FROM bem_kv WHERE k LIKE ? ESCAPE '\\'", (pattern,)).fetchone()
        return int(row[0]) if row else 0

    async def purge_expired(self) -> None:
        now = time.time()
        if self.kind == "postgres":
            assert self._pool is not None
            async with await self._pg() as conn:
                await conn.execute("DELETE FROM bem_kv WHERE expires_at IS NOT NULL AND expires_at < %s", (now,))
            return
        async with self._lock:
            assert self._sqlite is not None
            self._sqlite.execute("DELETE FROM bem_kv WHERE expires_at IS NOT NULL AND expires_at < ?", (now,))
            self._sqlite.commit()


store = Store()
