"""
Persistence backends for the plan queue.

``PlanQueueOperations`` owns all queue/history/UID logic; the classes here own
nothing but storage. The interface (``QueueStore``) is shaped by what the queue
code actually needs: JSON-string scalars under fixed keys plus two ordered
collections with redis-list semantics. Both implementations are flat classes —
inject whichever one a deployment needs:

- ``RedisQueueStore`` — the historical backend, a thin wrapper over one
  ``redis.asyncio`` pool.
- ``SqlQueueStore`` — SQLAlchemy (SQLite or PostgreSQL, selected by the URI
  scheme, mirroring configuration_service's dual-backend pattern).

``create_queue_store`` maps a URI to an implementation and fails hard on
anything it does not recognize.

List semantics every implementation must honor (they are redis's, since the
queue logic was written against them):

- ``list_range``/``list_trim`` use INCLUSIVE stop indices and accept negative
  indices counted from the end (``-1`` is the last element).
- ``list_insert`` pivots on an element's exact string value and returns the
  new list length, or ``-1`` if the pivot is absent.
- ``list_remove`` removes ALL occurrences of the exact string value and
  returns the number removed.
- push/insert return the new list length.
"""

from typing import List, Optional, Protocol

import redis.asyncio


class QueueStore(Protocol):
    """Storage interface required by ``PlanQueueOperations``."""

    async def open(self) -> None:
        """Connect and verify the backend is reachable (fail hard if not)."""
        ...

    async def close(self) -> None: ...

    # --- scalar JSON-string values ---

    async def get(self, key: str) -> Optional[str]: ...

    async def set(self, key: str, value: str) -> None: ...

    async def delete(self, *keys: str) -> None: ...

    # --- ordered collections (redis-list semantics, see module docstring) ---

    async def list_len(self, key: str) -> int: ...

    async def list_range(self, key: str, start: int, stop: int) -> List[str]: ...

    async def list_index(self, key: str, index: int) -> Optional[str]: ...

    async def list_push_front(self, key: str, value: str) -> int: ...

    async def list_push_back(self, key: str, value: str) -> int: ...

    async def list_pop_front(self, key: str) -> Optional[str]: ...

    async def list_pop_back(self, key: str) -> Optional[str]: ...

    async def list_insert(self, key: str, where: str, pivot: str, value: str) -> int: ...

    async def list_remove(self, key: str, value: str) -> int: ...

    async def list_trim(self, key: str, start: int, stop: int) -> None: ...


class RedisQueueStore:
    """``QueueStore`` over a ``redis.asyncio`` connection pool."""

    def __init__(self, redis_host: str = "localhost"):
        self._redis_host = redis_host
        self._r_pool = None

    async def open(self) -> None:
        if self._r_pool:
            return
        host = self._redis_host
        if "://" not in host:
            host = f"redis://{host}"
        try:
            self._r_pool = redis.asyncio.from_url(host, encoding="utf-8", decode_responses=True)
            await self._r_pool.ping()
        except Exception as ex:
            self._r_pool = None
            error_msg = (
                f"Failed to create the Redis pool: "
                f"Redis server may not be available at '{self._redis_host}'. "
                f"Exception: {ex}"
            )
            raise OSError(error_msg) from ex

    async def close(self) -> None:
        if self._r_pool:
            await self._r_pool.aclose()
            self._r_pool = None

    async def get(self, key):
        return await self._r_pool.get(key)

    async def set(self, key, value):
        await self._r_pool.set(key, value)

    async def delete(self, *keys):
        await self._r_pool.delete(*keys)

    async def list_len(self, key):
        return await self._r_pool.llen(key)

    async def list_range(self, key, start, stop):
        return await self._r_pool.lrange(key, start, stop)

    async def list_index(self, key, index):
        return await self._r_pool.lindex(key, index)

    async def list_push_front(self, key, value):
        return await self._r_pool.lpush(key, value)

    async def list_push_back(self, key, value):
        return await self._r_pool.rpush(key, value)

    async def list_pop_front(self, key):
        return await self._r_pool.lpop(key)

    async def list_pop_back(self, key):
        return await self._r_pool.rpop(key)

    async def list_insert(self, key, where, pivot, value):
        return await self._r_pool.linsert(key, where, pivot, value)

    async def list_remove(self, key, value):
        return await self._r_pool.lrem(key, 0, value)

    async def list_trim(self, key, start, stop):
        await self._r_pool.ltrim(key, start, stop)


class SqlQueueStore:
    """``QueueStore`` over SQLAlchemy (SQLite or PostgreSQL by URI scheme).

    Layout: one key/value table for scalars and one position-ordered table for
    lists. Positions are floats; pushes extend the range and inserts take the
    midpoint between neighbors (positions are renumbered if the midpoint
    collides, so precision exhaustion cannot corrupt ordering).

    Single-writer assumption: ``PlanQueueOperations`` serializes all calls
    through its ``asyncio.Lock``, so statements here do not need cross-call
    transactions.
    """

    def __init__(self, uri: str):
        self._uri = uri
        self._engine = None
        # Lazy import so the redis-only deployment does not need SQLAlchemy's
        # async drivers installed.
        import sqlalchemy as sa

        self._sa = sa
        metadata = sa.MetaData()
        self._t_kv = sa.Table(
            "qs_kv_store",
            metadata,
            sa.Column("key", sa.Text, primary_key=True),
            sa.Column("value", sa.Text, nullable=False),
        )
        self._t_list = sa.Table(
            "qs_list_store",
            metadata,
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column("list_key", sa.Text, nullable=False, index=True),
            sa.Column("pos", sa.Float, nullable=False),
            sa.Column("value", sa.Text, nullable=False),
            sa.Index("ix_qs_list_store_key_pos", "list_key", "pos"),
        )
        self._metadata = metadata

    async def open(self) -> None:
        if self._engine:
            return
        from sqlalchemy.ext.asyncio import create_async_engine

        try:
            self._engine = create_async_engine(self._uri)
            async with self._engine.begin() as conn:
                await conn.run_sync(self._metadata.create_all)
        except Exception as ex:
            self._engine = None
            error_msg = (
                f"Failed to open the queue database: the database may not be "
                f"available at '{self._uri}'. Exception: {ex}"
            )
            raise OSError(error_msg) from ex

    async def close(self) -> None:
        if self._engine:
            await self._engine.dispose()
            self._engine = None

    async def get(self, key):
        sa = self._sa
        async with self._engine.connect() as conn:
            row = (await conn.execute(sa.select(self._t_kv.c.value).where(self._t_kv.c.key == key))).first()
        return row[0] if row else None

    async def set(self, key, value):
        sa = self._sa
        async with self._engine.begin() as conn:
            updated = await conn.execute(
                sa.update(self._t_kv).where(self._t_kv.c.key == key).values(value=value)
            )
            if updated.rowcount == 0:
                await conn.execute(sa.insert(self._t_kv).values(key=key, value=value))

    async def delete(self, *keys):
        sa = self._sa
        async with self._engine.begin() as conn:
            await conn.execute(sa.delete(self._t_kv).where(self._t_kv.c.key.in_(keys)))
            await conn.execute(sa.delete(self._t_list).where(self._t_list.c.list_key.in_(keys)))

    # --- list helpers ---

    def _list_select(self, key):
        sa = self._sa
        t = self._t_list
        return sa.select(t.c.id, t.c.pos, t.c.value).where(t.c.list_key == key).order_by(t.c.pos, t.c.id)

    async def _rows(self, conn, key):
        return (await conn.execute(self._list_select(key))).all()

    @staticmethod
    def _resolve_range(length, start, stop):
        """Redis-style inclusive range with negative indices -> python slice bounds."""
        if start < 0:
            start = max(length + start, 0)
        if stop < 0:
            stop = length + stop
        stop = min(stop, length - 1)
        return start, stop

    async def list_len(self, key):
        sa = self._sa
        async with self._engine.connect() as conn:
            n = (
                await conn.execute(
                    sa.select(sa.func.count()).where(self._t_list.c.list_key == key)
                )
            ).scalar()
        return int(n or 0)

    async def list_range(self, key, start, stop):
        async with self._engine.connect() as conn:
            rows = await self._rows(conn, key)
        start, stop = self._resolve_range(len(rows), start, stop)
        if start > stop:
            return []
        return [r.value for r in rows[start : stop + 1]]

    async def list_index(self, key, index):
        async with self._engine.connect() as conn:
            rows = await self._rows(conn, key)
        if index < 0:
            index = len(rows) + index
        if 0 <= index < len(rows):
            return rows[index].value
        return None

    async def _push(self, key, value, *, front):
        sa = self._sa
        t = self._t_list
        async with self._engine.begin() as conn:
            agg = sa.func.min(t.c.pos) if front else sa.func.max(t.c.pos)
            edge = (await conn.execute(sa.select(agg).where(t.c.list_key == key))).scalar()
            pos = 0.0 if edge is None else (edge - 1.0 if front else edge + 1.0)
            await conn.execute(sa.insert(t).values(list_key=key, pos=pos, value=value))
            n = (await conn.execute(sa.select(sa.func.count()).where(t.c.list_key == key))).scalar()
        return int(n)

    async def list_push_front(self, key, value):
        return await self._push(key, value, front=True)

    async def list_push_back(self, key, value):
        return await self._push(key, value, front=False)

    async def _pop(self, key, *, front):
        sa = self._sa
        t = self._t_list
        async with self._engine.begin() as conn:
            order = (t.c.pos, t.c.id) if front else (t.c.pos.desc(), t.c.id.desc())
            row = (
                await conn.execute(
                    sa.select(t.c.id, t.c.value).where(t.c.list_key == key).order_by(*order).limit(1)
                )
            ).first()
            if row is None:
                return None
            await conn.execute(sa.delete(t).where(t.c.id == row.id))
        return row.value

    async def list_pop_front(self, key):
        return await self._pop(key, front=True)

    async def list_pop_back(self, key):
        return await self._pop(key, front=False)

    async def list_insert(self, key, where, pivot, value):
        if where.upper() not in ("BEFORE", "AFTER"):
            raise ValueError(f"Unsupported insert position: {where!r}")
        before = where.upper() == "BEFORE"
        sa = self._sa
        t = self._t_list
        async with self._engine.begin() as conn:
            rows = await self._rows(conn, key)
            pivot_idx = next((i for i, r in enumerate(rows) if r.value == pivot), None)
            if pivot_idx is None:
                return -1
            neighbor_idx = pivot_idx - 1 if before else pivot_idx + 1
            pivot_pos = rows[pivot_idx].pos
            if 0 <= neighbor_idx < len(rows):
                pos = (pivot_pos + rows[neighbor_idx].pos) / 2.0
            else:
                pos = pivot_pos - 1.0 if before else pivot_pos + 1.0
            if any(pos == r.pos for r in rows):
                # Midpoint collided with an existing position (float precision
                # exhausted) — renumber the whole list and take clean bounds.
                for i, r in enumerate(rows):
                    await conn.execute(sa.update(t).where(t.c.id == r.id).values(pos=float(i)))
                pos = pivot_idx - 0.5 if before else pivot_idx + 0.5
            await conn.execute(sa.insert(t).values(list_key=key, pos=pos, value=value))
        return len(rows) + 1

    async def list_remove(self, key, value):
        sa = self._sa
        t = self._t_list
        async with self._engine.begin() as conn:
            result = await conn.execute(
                sa.delete(t).where(t.c.list_key == key, t.c.value == value)
            )
        return int(result.rowcount or 0)

    async def list_trim(self, key, start, stop):
        sa = self._sa
        t = self._t_list
        async with self._engine.begin() as conn:
            rows = await self._rows(conn, key)
            start, stop = self._resolve_range(len(rows), start, stop)
            doomed = [r.id for i, r in enumerate(rows) if not (start <= i <= stop)]
            if doomed:
                await conn.execute(sa.delete(t).where(t.c.id.in_(doomed)))


def create_queue_store(uri: str) -> "QueueStore":
    """Map a storage URI to a ``QueueStore`` implementation.

    ``redis://host[:port]`` (or a bare ``host[:port]``) selects redis;
    SQLAlchemy async URIs (``sqlite+aiosqlite://...``,
    ``postgresql+psycopg://...``) select the SQL backend. Anything else is an
    error — there is no fallback.
    """
    if "://" not in uri or uri.startswith("redis://"):
        return RedisQueueStore(redis_host=uri)
    scheme = uri.split("://", 1)[0]
    if scheme.startswith("sqlite") or scheme.startswith("postgresql"):
        return SqlQueueStore(uri)
    raise ValueError(
        f"Unrecognized queue-store URI scheme {scheme!r} — expected redis://, "
        f"sqlite+aiosqlite:// or postgresql+psycopg://"
    )
