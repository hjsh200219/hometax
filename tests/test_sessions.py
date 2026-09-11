import pytest

from hometax_login.errors import LoginError
from hometax_login.sessions import SessionStore


class FakeClient:
    closed = False

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_owner_isolation_and_deletion():
    store = SessionStore(ttl=60, capacity=2)
    client = FakeClient()
    item = await store.add("owner-a", client, {"user_id": "one"})
    with pytest.raises(LoginError):
        async with store.lease(item.id, "owner-b"):
            pytest.fail("Another caller must not obtain this session")
    await store.remove(item.id, "owner-a")
    assert client.closed
    with pytest.raises(LoginError):
        async with store.lease(item.id, "owner-a"):
            pytest.fail("Deleted session must not be reused")


@pytest.mark.asyncio
async def test_expired_session_is_closed_and_capacity_freed():
    clock = [100.0]
    store = SessionStore(ttl=1, capacity=1, clock=lambda: clock[0])
    first = FakeClient()
    await store.add("owner", first, {})
    with pytest.raises(LoginError) as caught:
        await store.add("owner", FakeClient(), {})
    assert caught.value.code == "SESSION_CAPACITY"
    clock[0] = 102
    await store.prune()
    assert first.closed
    await store.add("owner", FakeClient(), {})
    await store.close()
