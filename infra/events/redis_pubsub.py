from __future__ import annotations

import json
from typing import Any

import redis.asyncio as aioredis


class RedisPubSubChannel:
    """Redis 事件通道包装。"""

    def __init__(self, redis: aioredis.Redis, channel_name: str) -> None:
        self.redis = redis
        self.channel_name = channel_name

    async def publish(self, payload: dict[str, Any]) -> None:
        await self.redis.publish(self.channel_name, json.dumps(payload, ensure_ascii=False))

    async def subscribe(self) -> aioredis.client.PubSub:
        pubsub = self.redis.pubsub()
        await pubsub.subscribe(self.channel_name)
        return pubsub

    async def unsubscribe(self, pubsub: aioredis.client.PubSub) -> None:
        await pubsub.unsubscribe(self.channel_name)
        await pubsub.aclose()
