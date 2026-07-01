"""Direct unit tests for the pluggable queue-store backends.

``SqlQueueStore`` is otherwise exercised only indirectly through the
``PlanQueueOperations`` suite (parametrized over redis/sqlite). These tests pin
its redis-list semantics directly — especially the range/index/trim paths that
now push their window into SQL (LIMIT/OFFSET) instead of loading the whole
list — plus the ``create_queue_store`` URI routing that has no other coverage.
"""

import asyncio

import pytest

from queueserver_service.manager.queue_store import (
    RedisQueueStore,
    SqlQueueStore,
    create_queue_store,
)


# --- create_queue_store URI routing ---------------------------------------


@pytest.mark.parametrize(
    "uri",
    ["localhost", "localhost:6379", "redis://localhost", "redis://host:6379"],
)
def test_create_queue_store_selects_redis(uri):
    store = create_queue_store(uri)
    assert isinstance(store, RedisQueueStore)


@pytest.mark.parametrize(
    "uri",
    [
        "sqlite+aiosqlite:///tmp/q.db",
        "postgresql+psycopg://u:p@h:5432/db",
    ],
)
def test_create_queue_store_selects_sql(uri):
    store = create_queue_store(uri)
    assert isinstance(store, SqlQueueStore)


def test_create_queue_store_rejects_unknown_scheme():
    with pytest.raises(ValueError):
        create_queue_store("mysql://host/db")


# --- SqlQueueStore list semantics -----------------------------------------


def _run(coro):
    return asyncio.run(coro)


def _store(tmp_path):
    return SqlQueueStore(f"sqlite+aiosqlite:///{tmp_path}/queue.db")


async def _seed(store, key, values):
    for v in values:
        await store.list_push_back(key, v)


def test_sql_push_back_range_and_len(tmp_path):
    async def testing():
        store = _store(tmp_path)
        await store.open()
        try:
            await _seed(store, "k", ["a", "b", "c", "d"])
            assert await store.list_len("k") == 4
            assert await store.list_range("k", 0, -1) == ["a", "b", "c", "d"]
            assert await store.list_range("k", 1, 2) == ["b", "c"]
            # negative indices, redis-style inclusive stop
            assert await store.list_range("k", -2, -1) == ["c", "d"]
            # out-of-range stop is clamped
            assert await store.list_range("k", 0, 99) == ["a", "b", "c", "d"]
            # empty window
            assert await store.list_range("k", 3, 1) == []
        finally:
            await store.close()

    _run(testing())


def test_sql_push_front_orders_before_existing(tmp_path):
    async def testing():
        store = _store(tmp_path)
        await store.open()
        try:
            await _seed(store, "k", ["b", "c"])
            await store.list_push_front("k", "a")
            assert await store.list_range("k", 0, -1) == ["a", "b", "c"]
        finally:
            await store.close()

    _run(testing())


def test_sql_index_positive_negative_and_oob(tmp_path):
    async def testing():
        store = _store(tmp_path)
        await store.open()
        try:
            await _seed(store, "k", ["a", "b", "c"])
            assert await store.list_index("k", 0) == "a"
            assert await store.list_index("k", 2) == "c"
            assert await store.list_index("k", -1) == "c"
            assert await store.list_index("k", -3) == "a"
            assert await store.list_index("k", 3) is None
            assert await store.list_index("k", -4) is None
        finally:
            await store.close()

    _run(testing())


def test_sql_pop_front_and_back(tmp_path):
    async def testing():
        store = _store(tmp_path)
        await store.open()
        try:
            await _seed(store, "k", ["a", "b", "c"])
            assert await store.list_pop_front("k") == "a"
            assert await store.list_pop_back("k") == "c"
            assert await store.list_range("k", 0, -1) == ["b"]
            assert await store.list_pop_front("k") == "b"
            assert await store.list_pop_front("k") is None
        finally:
            await store.close()

    _run(testing())


def test_sql_trim_keeps_middle_window(tmp_path):
    async def testing():
        store = _store(tmp_path)
        await store.open()
        try:
            await _seed(store, "k", ["a", "b", "c", "d", "e"])
            await store.list_trim("k", 1, 3)
            assert await store.list_range("k", 0, -1) == ["b", "c", "d"]
        finally:
            await store.close()

    _run(testing())


def test_sql_trim_head_only_and_tail_only(tmp_path):
    async def testing():
        store = _store(tmp_path)
        await store.open()
        try:
            await _seed(store, "h", ["a", "b", "c", "d"])
            await store.list_trim("h", 2, -1)  # drop the head
            assert await store.list_range("h", 0, -1) == ["c", "d"]

            await _seed(store, "t", ["a", "b", "c", "d"])
            await store.list_trim("t", 0, 1)  # drop the tail
            assert await store.list_range("t", 0, -1) == ["a", "b"]
        finally:
            await store.close()

    _run(testing())


def test_sql_trim_to_empty(tmp_path):
    async def testing():
        store = _store(tmp_path)
        await store.open()
        try:
            await _seed(store, "k", ["a", "b", "c"])
            # start > stop after resolution -> whole list removed (redis LTRIM)
            await store.list_trim("k", 5, 10)
            assert await store.list_len("k") == 0
            assert await store.list_range("k", 0, -1) == []
        finally:
            await store.close()

    _run(testing())


def test_sql_trim_large_kept_window(tmp_path):
    """Trimming a window bigger than SQLite's ~999 host-parameter limit must
    not raise (regression guard: an id-list ``NOT IN`` delete would fail here).
    """

    async def testing():
        store = _store(tmp_path)
        await store.open()
        try:
            n = 1500
            await _seed(store, "big", [str(i) for i in range(n)])
            # Keep [5, 1400] -> a kept window of 1396 rows, well over 999.
            await store.list_trim("big", 5, 1400)
            assert await store.list_len("big") == 1396
            assert await store.list_index("big", 0) == "5"
            assert await store.list_index("big", -1) == "1400"
        finally:
            await store.close()

    _run(testing())


def test_sql_remove_all_occurrences(tmp_path):
    async def testing():
        store = _store(tmp_path)
        await store.open()
        try:
            await _seed(store, "k", ["a", "x", "b", "x", "x"])
            removed = await store.list_remove("k", "x")
            assert removed == 3
            assert await store.list_range("k", 0, -1) == ["a", "b"]
        finally:
            await store.close()

    _run(testing())


def test_sql_insert_before_after_and_missing_pivot(tmp_path):
    async def testing():
        store = _store(tmp_path)
        await store.open()
        try:
            await _seed(store, "k", ["a", "c"])
            n = await store.list_insert("k", "BEFORE", "c", "b")
            assert n == 3
            assert await store.list_range("k", 0, -1) == ["a", "b", "c"]
            await store.list_insert("k", "AFTER", "c", "d")
            assert await store.list_range("k", 0, -1) == ["a", "b", "c", "d"]
            # absent pivot -> -1, list unchanged
            assert await store.list_insert("k", "BEFORE", "zzz", "q") == -1
            assert await store.list_range("k", 0, -1) == ["a", "b", "c", "d"]
        finally:
            await store.close()

    _run(testing())


def test_sql_scalar_get_set_delete(tmp_path):
    async def testing():
        store = _store(tmp_path)
        await store.open()
        try:
            assert await store.get("s") is None
            await store.set("s", "v1")
            assert await store.get("s") == "v1"
            await store.set("s", "v2")  # update path
            assert await store.get("s") == "v2"
            await store.delete("s")
            assert await store.get("s") is None
        finally:
            await store.close()

    _run(testing())
