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
    """管理所有 WebSocket 连接及其任务订阅。

    数据结构：
    - _connections: {client_id: WebSocket} — 知道怎么发消息给谁
    - _task_subs: {task_id: {client_id, ...}} — 知道这个任务谁在看
    - _client_tasks: {client_id: {task_id, ...}} — 反向索引，知道这个客户端订阅了哪些任务
    - _pubsub_listeners: {task_id: asyncio.Task} — 每个任务一个 Redis 监听协程

    并发安全：
    - 用 asyncio.Lock 保护上述 dict 的并发访问
    - 多个协程可能同时修改这些 dict，Lock 保证同一时刻只有一个协程在修改
    """

    def __init__(self):
        self._connections: dict[str, WebSocket] = {}           # client_id -> WebSocket 连接
        self._task_subs: dict[str, set[str]] = {}              # task_id -> {client_id, ...}
        self._client_tasks: dict[str, set[str]] = {}           # client_id -> {task_id, ...}
        self._pubsub_listeners: dict[str, asyncio.Task] = {}   # task_id -> 监听协程
        self._lock = asyncio.Lock()  # 保护上述 dict 的并发访问
        self._redis: redis_lib.Redis | None = None       # Redis 连接池实例（单例）
        self._redis_initialized = False  # 标记 Redis 是否已初始化（只初始化一次）

    def _get_redis(self):
        """返回 Redis 连接池客户端（单例），不可用时返回 None。

        初始化流程：
        1. 检查 _redis_initialized 标记，已初始化直接返回 _redis
        2. 读取配置，如果 config.redis.enabled=False 直接返回 None
        3. 用 Redis.from_url() 创建连接池，ping() 测试连通性
        4. 成功 → 返回连接池；失败 → _redis 设为 None
        5. 设置 _redis_initialized=True，后续调用不再尝试连接
        """
        if self._redis_initialized:
            return self._redis  # 已初始化，直接返回（可能是 None）

        config = load_config()
        if not config.redis.enabled:
            self._redis_initialized = True
            return None  # 配置禁用了 Redis

        try:
            self._redis = redis_lib.Redis.from_url(
                config.redis.url,
                decode_responses=False,  # 返回 bytes 而不是 str（需要手动 decode）
                max_connections=10,      # 连接池最多 10 个连接
            )
            self._redis.ping()  # 测试连通性
            self._redis_initialized = True
            return self._redis
        except Exception:
            # Redis 不可用，标记为已初始化（避免重试），返回 None
            self._redis_initialized = True
            return None

    async def connect(self, websocket: WebSocket, client_id: str):  # websocket: WebSocket 连接对象, client_id: 客户端唯一标识
        """接受 WebSocket 连接并注册到管理器。

        执行步骤：
        1. await websocket.accept() — 接受 TCP 连接（在网络 IO 时不做锁，避免阻塞其他协程）
        2. async with self._lock — 加锁保护字典修改
        3. 存入 _connections[client_id] = websocket
        4. 初始化 _client_tasks[client_id] = set()（空订阅列表）
        """
        await websocket.accept()
        async with self._lock:
            self._connections[client_id] = websocket
            self._client_tasks[client_id] = set()  # 初始化空的订阅列表

    async def disconnect(self, client_id: str):  # client_id: 要断开的客户端 ID
        """断开连接，清理该客户端的所有订阅。

        清理步骤（持锁）：
        1. self._client_tasks.pop(client_id) — 取出该客户端订阅的所有任务列表
        2. self._connections.pop(client_id) — 移除 WebSocket 连接
        3. 遍历 tasks，对每个 task_id：
           - self._task_subs[task_id].discard(client_id) — 从订阅者集合移除
           - 如果 subs 为空（最后一个订阅者断开）→ 删除 task_id 的所有数据
           - 取消对应的 Redis 监听协程（listener.cancel() 触发 CancelledError）
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
                        # 取消 Redis 监听协程，释放资源
                        listener = self._pubsub_listeners.pop(task_id, None)
                        if listener:
                            listener.cancel()  # asyncio.Task.cancel() 触发 CancelledError

    async def send_to_client(self, client_id: str, message: dict):  # client_id: 目标客户端 ID, message: 要发送的消息字典
        ws = self._connections.get(client_id)
        if ws:
            await ws.send_json(message)

    async def subscribe_task(self, client_id: str, task_id: str):  # client_id: 客户端 ID, task_id: 要订阅的任务 ID
        """订阅某个任务的进度推送。

        执行步骤（持锁）：
        1. 在 _task_subs 中注册：task_subs[task_id].add(client_id)
        2. 在 _client_tasks 中注册：client_tasks[client_id].add(task_id)
        3. 如果是该任务的第一个订阅者（task_id not in _pubsub_listeners）：
           → 创建 asyncio.Task 启动 _pubsub_listener(task_id) 协程
        """
        async with self._lock:
            # 双向索引：task -> clients 和 client -> tasks
            if task_id not in self._task_subs:
                self._task_subs[task_id] = set()
            self._task_subs[task_id].add(client_id)
            self._client_tasks.setdefault(client_id, set()).add(task_id)

            # 第一个订阅者 → 启动 Redis 监听协程
            # 不在启动时就监听，因为可能根本没人订阅，浪费资源
            if task_id not in self._pubsub_listeners:
                self._pubsub_listeners[task_id] = asyncio.create_task(
                    self._pubsub_listener(task_id)
                )

    async def unsubscribe_task(self, client_id: str, task_id: str):  # client_id: 客户端 ID, task_id: 要取消订阅的任务 ID
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

    async def _pubsub_listener(self, task_id: str):  # task_id: 要监听的任务 ID
        """监听 Redis Pub/Sub channel，将消息推送给订阅的客户端。

        执行流程：
        1. 获取 Redis 连接，订阅 channel "task_events:{task_id}"
        2. 进入 while True 循环：
           a. 用 loop.run_in_executor() 在线程池中调用 pubsub.get_message(timeout=1.0)
              — 因为 get_message() 是阻塞调用，不能直接 await
           b. 收到消息后 json.loads(msg["data"]) 解析为 dict
           c. async with self._lock 获取订阅者快照（set 复制），然后释放锁
           d. 遍历 subscribers，按 status 推送不同消息类型：
              - "done" → task_done, "failed" → task_failed, 其他 → task_progress
           e. 如果 status 是 "done" 或 "failed" → 清理订阅 + 退出循环
        3. finally: 取消 Redis 订阅 + 关闭连接
        """
        r = self._get_redis()
        if not r:
            return  # Redis 不可用，直接退出

        pubsub = r.pubsub()
        pubsub.subscribe(f"task_events:{task_id}")  # 订阅该任务的 Redis channel

        loop = asyncio.get_running_loop()
        try:
            while True:
                # 在线程中阻塞读取（timeout=1.0 秒超时，避免永久阻塞）
                msg = await loop.run_in_executor(None, lambda: pubsub.get_message(timeout=1.0))
                if msg and msg["type"] == "message":
                    data = json.loads(msg["data"])
                    status = data.get("status", "")

                    # 获取当前订阅者快照（持锁时间极短）
                    async with self._lock:
                        subscribers = set(self._task_subs.get(task_id, set()))

                    # 按消息类型推送给所有订阅者
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
                        else:  # "processing" 状态
                            await self.send_to_client(cid, {
                                "type": "task_progress",
                                "task_id": data["task_id"],
                                "step": data.get("step", ""),
                                "percent": data.get("percent", 0),
                            })

                    # 任务结束（done/failed），清理所有订阅并退出 listener
                    if status in ("done", "failed"):
                        async with self._lock:
                            # 从每个客户端的订阅列表中移除该任务
                            for cid in subscribers:
                                ct = self._client_tasks.get(cid)
                                if ct:
                                    ct.discard(task_id)
                            self._task_subs.pop(task_id, None)
                            self._pubsub_listeners.pop(task_id, None)
                        break  # 退出监听循环

                await asyncio.sleep(0.1)  # 没消息时短暂等待，避免 CPU 空转
        except asyncio.CancelledError:
            pass  # 被 cancel() 取消时静默退出
        finally:
            pubsub.unsubscribe()  # 取消 Redis 订阅
            pubsub.close()        # 关闭连接
