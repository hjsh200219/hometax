import asyncio
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from .errors import LoginError


@dataclass(repr=False)
class Session:
    id: str
    owner: str
    client: object
    identity: dict
    expires_at: float
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class SessionStore:
    """Process-local bounded storage; session cookies never become API responses."""

    def __init__(self, ttl=600, capacity=128, clock=time.time):
        self.ttl = ttl
        self.capacity = capacity
        self.clock = clock
        self.items: dict[str, Session] = {}
        self.lock = asyncio.Lock()

    async def add(self, owner, client, identity):
        await self.prune()
        async with self.lock:
            if len(self.items) >= self.capacity:
                raise LoginError("SESSION_CAPACITY", "세션 한도에 도달했습니다.", 503)
            item = Session(
                secrets.token_urlsafe(32), owner, client, identity, self.clock() + self.ttl
            )
            self.items[item.id] = item
            return item

    def _lookup(self, session_id, owner):
        item = self.items.get(session_id)
        if item is None or item.owner != owner or item.expires_at <= self.clock():
            raise LoginError("SESSION_NOT_FOUND", "세션이 없거나 만료됐습니다.", 404)
        return item

    @asynccontextmanager
    async def lease(self, session_id, owner):
        await self.prune()
        async with self.lock:
            item = self._lookup(session_id, owner)
        async with item.lock:
            async with self.lock:
                self._lookup(session_id, owner)
            yield item

    async def remove(self, session_id, owner):
        async with self.lock:
            item = self._lookup(session_id, owner)
            self.items.pop(session_id)
        async with item.lock:
            await item.client.close()

    async def prune(self):
        async with self.lock:
            expired = [s for s in self.items.values() if s.expires_at <= self.clock()]
            for s in expired:
                self.items.pop(s.id)
        for s in expired:
            async with s.lock:
                await s.client.close()

    async def close(self):
        async with self.lock:
            items = list(self.items.values())
            self.items.clear()
        for item in items:
            async with item.lock:
                await item.client.close()
