import asyncio
import pytest
from app.memory.session_store import SessionStore, SessionBusyError, SessionCapacityError


def test_ttl_refresh_capacity_and_disposal():
    async def run():
        now = [0.0]
        closed = []
        async def dispose(value):
            closed.append(value)
        store = SessionStore(1, 10, clock=lambda: now[0], disposer=dispose)
        async with store.use(('customer', 1, 'a'), object) as first:
            pass
        now[0] = 9
        async with store.use(('customer', 1, 'a'), object) as again:
            assert again is first
        with pytest.raises(SessionCapacityError):
            async with store.use(('customer', 2, 'a'), object):
                pass
        now[0] = 19
        async with store.use(('customer', 2, 'a'), object) as second:
            assert second is not first
        assert closed == [first]
        assert await store.clear(('customer', 2, 'a')) is True
        assert await store.clear(('customer', 2, 'a')) is False
        assert closed == [first, second]
    asyncio.run(run())


def test_active_and_waiting_sessions_are_never_replaced():
    async def run():
        now = [0.0]
        store = SessionStore(1, 10, clock=lambda: now[0])
        key = ('merchant', 1, 'same')
        started, release, queued = asyncio.Event(), asyncio.Event(), asyncio.Event()
        seen = []
        async def first():
            async with store.use(key, object) as agent:
                seen.append(agent)
                started.set()
                await release.wait()
            # The holder runs this before the awakened waiter gets CPU time.
            with pytest.raises(SessionBusyError):
                await store.clear(key)
        async def second():
            queued.set()
            async with store.use(key, object) as agent:
                seen.append(agent)
        task = asyncio.create_task(first())
        await started.wait()
        waiter = asyncio.create_task(second())
        await queued.wait()
        now[0] = 100
        with pytest.raises(SessionBusyError):
            await store.clear(key)
        with pytest.raises(SessionCapacityError):
            async with store.use(('merchant', 2, 'other'), object):
                pass
        release.set()
        # The waiter has reserved the entry even between lock release and acquisition.
        await task
        await waiter
        assert len(seen) == 2 and seen[0] is seen[1]
        assert await store.clear(key)
    asyncio.run(run())


@pytest.mark.parametrize('cancel_holder', [False, True])
def test_cancelled_requests_release_usage(cancel_holder):
    async def run():
        store = SessionStore(1, 10)
        key = ('customer', 1, 'a')
        ready, release, queued = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def holder():
            async with store.use(key, object):
                ready.set()
                await release.wait()
        async def waiter():
            queued.set()
            async with store.use(key, object):
                pass
        task = asyncio.create_task(holder())
        await ready.wait()
        waiting = asyncio.create_task(waiter())
        await queued.wait()
        victim = task if cancel_holder else waiting
        victim.cancel()
        with pytest.raises(asyncio.CancelledError):
            await victim
        release.set()
        await asyncio.gather(task, waiting, return_exceptions=True)
        assert await store.clear(key)
    asyncio.run(run())


def test_factory_failure_does_not_leave_capacity_occupied():
    async def run():
        store = SessionStore(1, 10)
        def fail():
            raise ValueError('synthetic factory error')
        with pytest.raises(ValueError):
            async with store.use(('customer', 1, 'a'), fail):
                pass
        async with store.use(('customer', 1, 'b'), object):
            pass
    asyncio.run(run())


def test_tool_failure_preserves_idle_session_and_can_clear_it():
    async def run():
        store = SessionStore(1, 10)
        with pytest.raises(ValueError):
            async with store.use(('merchant', 1, 'a'), object) as first:
                raise ValueError('synthetic reply error')
        async with store.use(('merchant', 1, 'a'), object) as second:
            assert second is first
        assert await store.clear(('merchant', 1, 'a'))
    asyncio.run(run())


def test_close_failure_does_not_leak_details_or_prevent_clear(capsys):
    async def run():
        async def fail(value):
            raise OSError('synthetic-private-close-detail')
        store = SessionStore(1, 10, disposer=fail)
        async with store.use(('customer', 1, 'a'), object):
            pass
        assert await store.clear(('customer', 1, 'a'))
        assert len(store) == 0
    asyncio.run(run())
    assert 'synthetic-private-close-detail' not in capsys.readouterr().err


@pytest.mark.parametrize('size,ttl', [(0, 10), (1, 0), (-1, 10), (True, 10)])
def test_invalid_store_configuration_is_rejected(size, ttl):
    with pytest.raises(ValueError):
        SessionStore(size, ttl)


def test_expired_resources_close_before_waiting_on_another_session():
    async def run():
        now = [0.0]
        disposed, entered, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        closed = []
        async def dispose(agent):
            closed.append(agent)
            disposed.set()
        store = SessionStore(2, 10, clock=lambda: now[0], disposer=dispose)
        async with store.use(('customer', 1, 'expired'), object) as old:
            pass
        async def holder():
            async with store.use(('customer', 1, 'active'), object):
                entered.set()
                await release.wait()
        async def waiter():
            async with store.use(('customer', 1, 'active'), object):
                pass
        task = asyncio.create_task(holder())
        await entered.wait()
        now[0] = 10
        waiting = asyncio.create_task(waiter())
        try:
            await asyncio.wait_for(disposed.wait(), 0.1)
            assert closed == [old] and not waiting.done()
        finally:
            release.set()
            await asyncio.gather(task, waiting)
        assert closed == [old]
    asyncio.run(run())


def test_resource_close_timeout_does_not_block_clear_forever(capsys):
    async def run():
        stopped = asyncio.Event()
        async def dispose(agent):
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        store = SessionStore(1, 10, disposer=dispose, close_timeout=0.01)
        async with store.use(('merchant', 1, 'a'), object):
            pass
        assert await asyncio.wait_for(store.clear(('merchant', 1, 'a')), 0.5)
        assert stopped.is_set()
    asyncio.run(run())


def test_cancellation_during_retired_cleanup_releases_reserved_slot():
    async def run():
        now = [0.0]
        started, stopped = asyncio.Event(), asyncio.Event()
        async def dispose(agent):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        store = SessionStore(1, 10, clock=lambda: now[0], disposer=dispose)
        async with store.use(('customer', 1, 'old'), object):
            pass
        now[0] = 10
        async def reserve_new():
            async with store.use(('customer', 1, 'new'), object):
                pytest.fail('cancelled request must not construct/yield a new agent')
        task = asyncio.create_task(reserve_new())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(stopped.wait(), 0.5)
        assert len(store) == 0
    asyncio.run(run())
