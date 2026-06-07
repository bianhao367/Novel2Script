"""
WebSocket 连接管理器
====================
管理所有 WebSocket 连接及其任务订阅，负责：
- 连接生命周期管理（connect / disconnect）
- 任务进度订阅（subscribe_task / unsubscribe_task）
- Redis Pub/Sub 监听：将 worker 的进度消息实时推送给前端

架构：
- 每个客户端连接分配唯一 client_id
- 一个客户端可订阅多个任务，一个任务可被多个客户端订阅
- 任务完成/失败时自动清理订阅并停止 Redis 监听
- 使用 asyncio.Lock 保护并发访问

使用方式（在 server.py 中）：
    manager = ConnectionManager()
    await manager.connect(websocket, client_id)
    await manager.subscribe_task(client_id, task_id)
"""

import asyncio
import json

import redis as redis_lib
from starlette.websockets import WebSocket

from src.config import load_config


class ConnectionManager:
    """管理所有 WebSocket 连接及其任务订阅。"""

    def __init__(self):
        self._connections: dict[str, WebSocket] = {}
        self._task_subs: dict[str, set[str]] = {}       # task_id -> {client_id, ...}
        self._client_tasks: dict[str, set[str]] = {}     # client_id -> {task_id, ...}
        self._pubsub_listeners: dict[str, asyncio.Task] = {}  # task_id -> Task
        self._lock = asyncio.Lock()
        self._redis: redis_lib.Redis | None = None
        self._redis_initialized = False

    def _get_redis(self):
        """返回 Redis 连接池客户端（单例），不可用时返回 None。"""
        if self._redis_initialized:
            return self._redis

        config = load_config()
        if not config.redis.enabled:
            self._redis_initialized = True
            return None

        try:
            self._redis = redis_lib.Redis.from_url(
                config.redis.url,
                decode_responses=False,
                max_connections=10,
            )
            self._redis.ping()
            self._redis_initialized = True
            return self._redis
        except Exception:
            self._redis_initialized = True
            return None

    async def connect(self, websocket: WebSocket, client_id: str):
        """接受 WebSocket 连接并注册到管理器。"""
        await websocket.accept()
        async with self._lock:
            self._connections[client_id] = websocket
            self._client_tasks[client_id] = set()

    async def disconnect(self, client_id: str):
        """断开连接，清理该客户端的所有订阅。

        如果某个任务的最后一个订阅者断开，同时取消 Redis Pub/Sub 监听。
        """
        async with self._lock:
            tasks = self._client_tasks.pop(client_id, set())
            self._connections.pop(client_id, None)
            for task_id in tasks:
                subs = self._task_subs.get(task_id)
                if subs:
                    subs.discard(client_id)
                    if not subs:  # 最后一个订阅者断开
                        del self._task_subs[task_id]
                        listener = self._pubsub_listeners.pop(task_id, None)
                        if listener:
                            listener.cancel()

    async def send_to_client(self, client_id: str, message: dict):
        ws = self._connections.get(client_id)
        if ws:
            await ws.send_json(message)

    async def subscribe_task(self, client_id: str, task_id: str):
        async with self._lock:
            if task_id not in self._task_subs:
                self._task_subs[task_id] = set()
            self._task_subs[task_id].add(client_id)
            self._client_tasks.setdefault(client_id, set()).add(task_id)

            if task_id not in self._pubsub_listeners:
                self._pubsub_listeners[task_id] = asyncio.create_task(
                    self._pubsub_listener(task_id)
                )

    async def unsubscribe_task(self, client_id: str, task_id: str):
        async with self._lock:
            subs = self._task_subs.get(task_id)
            if subs:
                subs.discard(client_id)
                if not subs:
                    del self._task_subs[task_id]
                    listener = self._pubsub_listeners.pop(task_id, None)
                    if listener:
                        listener.cancel()
            client_tasks = self._client_tasks.get(client_id)
            if client_tasks:
                client_tasks.discard(task_id)

    async def _pubsub_listener(self, task_id: str):
        """监听 Redis Pub/Sub channel，将消息推送给订阅的客户端。

        每个任务对应一个独立的 listener asyncio.Task。
        使用 run_in_executor 避免 Redis 阻塞读取阻塞事件循环。
        任务完成/失败后自动清理订阅并退出。
        """
        r = self._get_redis()
        if not r:
            return

        pubsub = r.pubsub()
        pubsub.subscribe(f"task_events:{task_id}")

        loop = asyncio.get_running_loop()
        try:
            while True:
                # 在线程中阻塞读取，避免阻塞事件循环
                msg = await loop.run_in_executor(None, lambda: pubsub.get_message(timeout=1.0))
                if msg and msg["type"] == "message":
                    data = json.loads(msg["data"])
                    status = data.get("status", "")

                    # 获取当前订阅者快照，避免长时间持锁
                    async with self._lock:
                        subscribers = set(self._task_subs.get(task_id, set()))

                    # 按状态类型推送给所有订阅者
                    for cid in subscribers:
                        if status == "done":
                            await self.send_to_client(cid, {
                                "type": "task_done",
                                "task_id": data["task_id"],
                                "result": data.get("result"),
                            })
                        elif status == "failed":
                            await self.send_to_client(cid, {
                                "type": "task_failed",
                                "task_id": data["task_id"],
                                "error": data.get("error", "Unknown error"),
                            })
                        else:
                            await self.send_to_client(cid, {
                                "type": "task_progress",
                                "task_id": data["task_id"],
                                "step": data.get("step", ""),
                                "percent": data.get("percent", 0),
                            })

                    # 任务结束，清理所有订阅并退出 listener
                    if status in ("done", "failed"):
                        async with self._lock:
                            for cid in subscribers:
                                ct = self._client_tasks.get(cid)
                                if ct:
                                    ct.discard(task_id)
                            self._task_subs.pop(task_id, None)
                            self._pubsub_listeners.pop(task_id, None)
                        break

                await asyncio.sleep(0.1)  # 避免忙等
        except asyncio.CancelledError:
            pass
        finally:
            pubsub.unsubscribe()
            pubsub.close()
