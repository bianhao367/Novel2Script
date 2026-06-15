"""
Novel2Script API Server
=======================
将小说 .txt 文件转换为结构化剧本的 HTTP + WebSocket 服务。

架构：
- FastAPI 应用，提供 REST API 和 WebSocket 双通道
- WebSocket 用于实时聊天流式输出和异步任务进度推送
- Redis + rq 实现异步任务队列（可选，降级到同步模式）
- SSE 作为 WebSocket 的降级方案

端点：
- GET  /                     前端页面
- GET  /api/v1/health        健康检查
- POST /api/v1/chat          AI 对话（SSE 流式）
- POST /api/v1/convert       同步文件转换
- POST /api/v1/convert/async 异步文件转换（需 Redis）
- GET  /api/v1/tasks/{id}    查询异步任务状态
- GET  /api/v1/schema        获取剧本 JSON Schema
- WS   /ws                   WebSocket 双向通信

启动方式：
    python server.py
    # 或 uvicorn server:app --reload
"""

import asyncio
import atexit           # 注册退出清理函数（线程池关闭）
import json
import re
import tempfile         # 临时文件（上传的小说文件暂存）
import threading        # threading.Lock 保护内存任务状态
import time
import traceback        # 打印完整异常堆栈
import uuid             # 生成唯一 ID（client_id, task_id）
from concurrent.futures import ThreadPoolExecutor  # 线程池（运行同步 pipeline）
from copy import deepcopy  # 深拷贝配置（避免并发修改）
from pathlib import Path

import redis as redis_lib
from fastapi import FastAPI, File, UploadFile, HTTPException, Form
from fastapi.responses import JSONResponse, FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from starlette.websockets import WebSocket, WebSocketDisconnect

from src.config import Config, ApiConfig, PipelineConfig, load_config
from src.llm_client import LLMError, LLMClient
from src.parser import SCRIPT_SCHEMA, Script
from src.pipeline import Pipeline
from src.ws_manager import ConnectionManager

app = FastAPI(
    title="Novel2Script",
    description="将小说文本转换为结构化剧本",
    version="0.1.0",
)

app.mount("/static", StaticFiles(directory="static"), name="static")

MAX_FILE_SIZE = 5 * 1024 * 1024  # 5 MB，防止上传过大的文件撑爆内存

# WebSocket 连接管理器（全局单例）
manager = ConnectionManager()

# 线程池：用于运行同步的 Pipeline（Pipeline 内部调 LLM 是同步阻塞的）
# max_workers=4：同时最多处理 4 个文件转换请求
_executor = ThreadPoolExecutor(max_workers=4)
atexit.register(_executor.shutdown, wait=False)  # 服务器退出时关闭线程池

# --- 内存任务状态跟踪（Redis 不可用时的降级方案）---
# 用 threading.Lock 而不是 asyncio.Lock，因为这些 dict 会被线程池中的同步代码访问
# （convert_async 的 run_pipeline 在 ThreadPoolExecutor 线程中修改 _memory_tasks）
_memory_tasks: dict[str, dict] = {}              # {task_id: {status, step, percent, result, ...}}
_memory_tasks_lock = threading.Lock()            # 保护 _memory_tasks 的跨线程并发访问

_MEMORY_TASKS_MAX = 100  # 最多保留 100 个任务状态，防止内存泄漏

def _memory_tasks_cleanup():
    """保留最近 100 个条目，删除最旧的。调用前需持有 _memory_tasks_lock。

    清理步骤：
    1. 如果 _memory_tasks 长度 <= 100 → 不需要清理
    2. 按 _created_at 时间戳排序
    3. 删除最旧的超出 100 的条目
    """
    if len(_memory_tasks) <= _MEMORY_TASKS_MAX:
        return
    sorted_items = sorted(
        _memory_tasks.items(),
        key=lambda kv: kv[1].get("_created_at", 0),
    )
    for task_id, _ in sorted_items[:len(sorted_items) - _MEMORY_TASKS_MAX]:
        del _memory_tasks[task_id]


# --- 请求模型 ---

class ChatRequest(BaseModel):
    messages: list[dict]        # OpenAI 格式的消息列表 [{role, content}, ...]
    script_context: str = ""    # 当前剧本上下文（注入 system prompt）
    model: str = ""             # 用户自定义模型名（空则用默认配置）
    base_url: str = ""          # 用户自定义 API 地址
    api_key: str = ""           # 用户自定义 API 密钥
    stream: bool = True         # 是否使用 SSE 流式输出


# --- 工具 ---

def _sanitize_name(filename: str) -> str:  # filename: 上传的原始文件名
    """从上传文件名提取安全的目录名，过滤路径遍历字符并限制长度。"""
    name = Path(filename).stem
    name = re.sub(r'[\\/:*?"<>|]', '_', name)[:100]
    return name or "unnamed"


def _apply_settings(base: Config, model: str, base_url: str, api_key: str) -> Config:  # base: 默认配置, model/base_url/api_key: 用户覆盖值（空则不覆盖）
    """用用户提供的值覆盖默认配置（返回独立副本，避免并发竞态）。

    执行步骤：
    1. deepcopy(base) 创建独立副本（避免多个请求共享同一 Config 对象）
    2. 如果 model 非空 → config.model = model
    3. 如果 base_url 非空 → config.api.base_url = base_url
    4. 如果 api_key 非空 → config.api.api_key = api_key
    5. 返回修改后的副本

    空值不覆盖：前端可能只传了 model，没传 base_url/api_key，空值表示"用默认的"。
    """
    config = deepcopy(base)  # 创建独立副本
    if model:
        config.model = model
    if base_url:
        config.api.base_url = base_url
    if api_key:
        config.api.api_key = api_key
    return config


# --- 响应模型 ---

def _script_to_response(script: Script, novel_name: str) -> dict:  # script: 剧本对象, novel_name: 小说名（用于响应标识）
    """将 Script 对象转换为 API 响应字典。

    转换逻辑：
    1. 遍历 script.scenes，统计每个场景的 dialogue_count 和 action_count
    2. 组装 scenes_summary 列表（scene_number, slugline, 对白数, 动作数）
    3. 遍历 script.characters，提取 name 和 description
    4. 返回完整字典：novel_name, title, scene_count, character_count, scenes, characters, script
    """
    scenes_summary = []
    for s in script.scenes:
        dialogue_count = sum(1 for c in s.content if c.type == "dialogue")
        action_count = sum(1 for c in s.content if c.type == "action")
        scenes_summary.append({
            "scene_number": s.scene_number,
            "slugline": s.slugline,
            "dialogue_count": dialogue_count,
            "action_count": action_count,
        })
    return {
        "novel_name": novel_name,
        "title": script.title,
        "scene_count": len(script.scenes),
        "character_count": len(script.characters),
        "scenes": scenes_summary,
        "characters": [
            {"name": c.name, "description": c.description}
            for c in script.characters
        ],
        "script": script.model_dump(),
    }


# --- Redis 连接池（单例，启动时初始化一次） ---

_redis_pool: redis_lib.Redis | None = None
_redis_pool_initialized = False


def _get_redis() -> redis_lib.Redis | None:
    """返回 Redis 连接池客户端，不可用时返回 None。

    初始化流程：
    1. 检查 _redis_pool_initialized 标记，已初始化直接返回 _redis_pool
    2. 读取配置，如果 config.redis.enabled=False 直接返回 None
    3. Redis.from_url() 创建连接池（max_connections=10），ping() 测试连通性
    4. 成功 → 返回连接池；失败 → _redis_pool 设为 None
    5. 设置 _redis_pool_initialized=True，后续调用不再尝试连接
    """
    global _redis_pool, _redis_pool_initialized
    if _redis_pool_initialized:
        return _redis_pool

    config = load_config()
    if not config.redis.enabled:
        _redis_pool_initialized = True
        return None

    try:
        _redis_pool = redis_lib.Redis.from_url(
            config.redis.url,
            decode_responses=False,
            max_connections=10,
        )
        _redis_pool.ping()
        _redis_pool_initialized = True
        return _redis_pool
    except Exception:
        _redis_pool_initialized = True
        return None


# --- 前端入口 ---

@app.get("/")
def index():
    return FileResponse("static/index.html")


# --- 端点 ---

@app.get("/api/v1/health")
def health():
    config = load_config()
    return {
        "status": "ok",
        "model": config.model,
        "base_url": config.api.base_url,
    }


# --- WebSocket 端点 ---

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket 主端点，处理所有客户端连接。

    连接建立流程：
    1. 生成唯一 client_id（uuid4）
    2. manager.connect(websocket, client_id) — 注册连接
    3. 发送健康信息（当前模型名、API 地址）
    4. 启动心跳协程（每 30 秒 ping 一次）
    5. 进入 while True 消息接收循环

    消息协议（客户端→服务端）：
    - {action: "chat", request_id, messages, script_context, model, base_url, api_key}
    - {action: "subscribe_task", task_id}
    - {action: "unsubscribe_task", task_id}
    - {action: "pong"}  —— 回复服务端的心跳 ping

    消息协议（服务端→客户端）：
    - {type: "chat_chunk", request_id, chunk_type, content}  —— 聊天流式输出
    - {type: "chat_done", request_id}  —— 聊天完成
    - {type: "chat_error", request_id, error}  —— 聊天出错
    - {type: "task_progress", task_id, step, percent}  —— 任务进度
    - {type: "task_done", task_id, result}  —— 任务完成
    - {type: "task_failed", task_id, error}  —— 任务失败
    - {type: "ping"}  —— 心跳检测
    - {type: "health", model, base_url}  —— 健康信息
    """
    client_id = str(uuid.uuid4())  # 每个连接分配唯一 ID
    await manager.connect(websocket, client_id)

    # 连接建立后立即发送健康信息，前端据此显示当前模型
    config = load_config()
    await manager.send_to_client(client_id, {
        "type": "health",
        "model": config.model,
        "base_url": config.api.base_url,
    })

    # 启动心跳循环：每 30 秒 ping 一次，检测连接是否还活着
    heartbeat_task = asyncio.create_task(_heartbeat_loop(client_id))
    chat_tasks: list[asyncio.Task] = []  # 跟踪所有聊天任务，断开时取消

    try:
        while True:
            data = await websocket.receive_json()  # 阻塞等待客户端消息
            action = data.get("action")
            if action == "chat":
                # 用 asyncio.create_task 异步处理聊天
                # 不直接 await，因为 await 会阻塞消息接收循环，导致无法接收其他消息
                task = asyncio.create_task(_handle_ws_chat(client_id, data))
                chat_tasks.append(task)
                # 完成后自动从列表中移除（避免内存泄漏）
                task.add_done_callback(lambda t: chat_tasks.remove(t) if t in chat_tasks else None)
            elif action == "subscribe_task":
                await manager.subscribe_task(client_id, data["task_id"])
            elif action == "unsubscribe_task":
                await manager.unsubscribe_task(client_id, data["task_id"])
            elif action == "pong":
                pass  # 心跳回复，无需处理
            else:
                await manager.send_to_client(client_id, {
                    "type": "error",
                    "message": f"Unknown action: {action}",
                })
    except WebSocketDisconnect:
        pass  # 客户端断开，正常退出
    finally:
        # 清理：取消心跳、取消所有聊天任务、从管理器中移除
        heartbeat_task.cancel()
        for t in chat_tasks:
            t.cancel()
        await manager.disconnect(client_id)


async def _heartbeat_loop(client_id: str, interval: int = 30):  # client_id: 客户端 ID, interval: 心跳间隔秒数
    while True:
        await asyncio.sleep(interval)
        try:
            await manager.send_to_client(client_id, {"type": "ping"})
        except Exception:
            break


async def _handle_ws_chat(client_id: str, data: dict):  # client_id: 客户端 ID, data: 客户端发来的消息（含 messages, model 等）
    """WebSocket 聊天处理，流式推送 chunk。

    生产者-消费者模式：
    1. 创建 asyncio.Queue
    2. _produce() 在线程池中运行：同步调用 llm.chat_stream()，逐个 chunk 用
       run_coroutine_threadsafe(queue.put(chunk), loop) 放入 Queue
       - 结束放 None，出错放 Exception
    3. 消费者循环：await queue.get() 取出 item
       - None → 正常完成，发送 chat_done
       - Exception → 发送 chat_error
       - dict → 发送 chat_chunk（chunk_type + content）

    run_coroutine_threadsafe 用于在线程中调用 Queue.put() 协程，
    因为 Queue.put() 是协程，不能在普通线程中直接 await。
    """
    request_id = data.get("request_id", "")  # 前端发的请求 ID，用于匹配响应
    try:
        # 合并用户自定义配置（model/base_url/api_key）
        config = _apply_settings(
            load_config(),
            data.get("model", ""),
            data.get("base_url", ""),
            data.get("api_key", ""),
        )
        llm = LLMClient(config)

        # 构造消息：system prompt + 用户消息
        system_msg = "你是一个专业的编剧助手。你可以帮助用户打磨剧本、修改对白、调整情节结构。请用中文回答。"
        if data.get("script_context"):
            system_msg += f"\n\n当前剧本上下文：\n{data['script_context']}"

        messages = [{"role": "system", "content": system_msg}] + data["messages"]

        # --- 生产者-消费者模式 ---
        queue: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_running_loop()  # 获取当前事件循环（供线程中使用）

        def _produce():
            """生产者：在线程池中运行，逐个 chunk 放入 Queue。

            执行流程：
            1. 遍历 llm.chat_stream(messages) 同步生成器
            2. 每个 chunk 用 asyncio.run_coroutine_threadsafe(queue.put(chunk), loop) 放入 Queue
            3. 出错时把 Exception 放入 Queue
            4. finally 中放入 None（结束信号）
            """
            try:
                for chunk in llm.chat_stream(messages):  # 同步生成器，阻塞线程
                    asyncio.run_coroutine_threadsafe(queue.put(chunk), loop)
            except Exception as e:
                asyncio.run_coroutine_threadsafe(queue.put(e), loop)  # 错误也放入 Queue
            finally:
                asyncio.run_coroutine_threadsafe(queue.put(None), loop)  # 结束信号

        _executor.submit(_produce)  # 提交到线程池，立即返回

        # 消费者循环：从 Queue 取出 chunk 推送给客户端
        while True:
            item = await queue.get()  # 阻塞等待（异步阻塞，不卡事件循环）
            if item is None:  # 生产者已完成
                break
            if isinstance(item, Exception):  # 生产者出错
                await manager.send_to_client(client_id, {
                    "type": "chat_error",
                    "request_id": request_id,
                    "error": str(item),
                })
                return
            # 正常 chunk，推送给客户端
            await manager.send_to_client(client_id, {
                "type": "chat_chunk",
                "request_id": request_id,
                "chunk_type": item["type"],  # "reasoning" 或 "content"
                "content": item["content"],
            })

        # 全部推送完成
        await manager.send_to_client(client_id, {
            "type": "chat_done",
            "request_id": request_id,
        })

    except LLMError as e:
        await manager.send_to_client(client_id, {
            "type": "chat_error",
            "request_id": request_id,
            "error": str(e),
        })
    except Exception as e:
        traceback.print_exc()  # 打印完整堆栈到服务器日志
        await manager.send_to_client(client_id, {
            "type": "chat_error",
            "request_id": request_id,
            "error": f"服务器内部错误: {e}",
        })


@app.post("/api/v1/convert")
def convert(
    file: UploadFile = File(...),
    model: str = Form(""),
    base_url: str = Form(""),
    api_key: str = Form(""),
):
    """上传小说 .txt 文件，返回转换后的剧本（同步阻塞，无进度）。"""
    if not file.filename or not file.filename.lower().endswith(".txt"):
        raise HTTPException(status_code=400, detail="只接受 .txt 文件")

    content = file.file.read()
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(status_code=400, detail=f"文件过大，上限 {MAX_FILE_SIZE // 1024 // 1024} MB")
    if len(content) == 0:
        raise HTTPException(status_code=400, detail="文件为空")

    novel_name = _sanitize_name(file.filename)
    with tempfile.NamedTemporaryFile(mode="wb", suffix=".txt", delete=False) as tmp:
        tmp.write(content)
        tmp_path = tmp.name

    try:
        config = _apply_settings(load_config(), model, base_url, api_key)
        pipeline = Pipeline(config)
        script = pipeline.run(tmp_path, novel_name)
        return _script_to_response(script, novel_name)
    except LLMError as e:
        raise HTTPException(status_code=502, detail=f"LLM 调用失败: {e}")
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"服务器内部错误: {e}")
    finally:
        Path(tmp_path).unlink(missing_ok=True)


@app.post("/api/v1/convert/stream")
def convert_stream(
    file: UploadFile = File(...),       # 上传的小说 .txt 文件
    model: str = Form(""),              # 用户自定义模型名
    base_url: str = Form(""),           # 用户自定义 API 地址
    api_key: str = Form(""),            # 用户自定义 API 密钥
):
    """上传小说 .txt 文件，SSE 流式返回进度 + 最终结果（无需 Redis）。

    SSE（Server-Sent Events）流程：
    1. 上传文件暂存到临时目录
    2. 创建 queue.Queue（线程安全）和 threading.Event（取消信号）
    3. 定义回调函数：progress_callback 和 chunk_result_callback，把消息放入 Queue
    4. 在线程池中运行 pipeline，通过回调推动进度
    5. event_generator() 从 Queue 中取消息，格式化为 SSE 事件流返回

    SSE 格式：每一行以 "data: " 开头，JSON 格式，以 \n\n 结尾
    浏览器用 EventSource API 接收。

    超时处理：msg_queue.get(timeout=600) 最多等 10 分钟，超时则取消 pipeline。
    """
    if not file.filename or not file.filename.lower().endswith(".txt"):
        raise HTTPException(status_code=400, detail="只接受 .txt 文件")

    content = file.file.read()
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(status_code=400, detail=f"文件过大，上限 {MAX_FILE_SIZE // 1024 // 1024} MB")
    if len(content) == 0:
        raise HTTPException(status_code=400, detail="文件为空")

    novel_name = _sanitize_name(file.filename)
    # 上传文件暂存到临时目录（pipeline 需要文件路径，不能直接读内存）
    with tempfile.NamedTemporaryFile(mode="wb", suffix=".txt", delete=False) as tmp:
        tmp.write(content)
        tmp_path = tmp.name

    def event_generator():
        """SSE 事件生成器：从 queue.Queue 读取消息，格式化为 SSE 事件流。

        SSE 格式：每一行以 "data: " 开头，JSON 格式，以 \\n\\n 结尾
        """
        import queue
        msg_queue: queue.Queue = queue.Queue()  # 线程间通信：pipeline → 生成器
        cancel_event = threading.Event()  # 取消信号：超时或客户端断开时置位

        # 回调函数：pipeline 在关键步骤调用，把消息放入 Queue
        def progress_callback(step: str, percent: int):
            msg_queue.put({"type": "progress", "step": step, "percent": percent})

        def chunk_result_callback(data: dict):
            msg_queue.put({"type": "chunk_result", "data": data})

        def run_pipeline():
            """在线程池中运行 pipeline，通过回调把进度推入 Queue。"""
            try:
                config = _apply_settings(load_config(), model, base_url, api_key)
                pipeline = Pipeline(
                    config,
                    progress_callback=progress_callback,
                    chunk_result_callback=chunk_result_callback,
                    cancel_event=cancel_event,  # 传入取消事件，pipeline 会定期检查
                )
                script = pipeline.run(tmp_path, novel_name)
                result = _script_to_response(script, novel_name)
                msg_queue.put({"type": "done", "result": result})
            except Exception as e:
                msg_queue.put({"type": "error", "error": f"{type(e).__name__}: {e}"})
            finally:
                Path(tmp_path).unlink(missing_ok=True)  # 清理临时文件

        _executor.submit(run_pipeline)  # 提交到线程池

        try:
            while True:
                try:
                    item = msg_queue.get(timeout=600)  # 最多等 10 分钟
                except Exception:
                    # 超时：pipeline 可能卡住了，取消它
                    cancel_event.set()
                    yield f"data: {json.dumps({'type': 'error', 'error': '处理超时'})}\n\n"
                    break

                msg_type = item.get("type")
                if msg_type == "error":
                    yield f"data: {json.dumps({'type': 'error', 'error': item['error']})}\n\n"
                    break
                elif msg_type == "done":
                    yield f"data: {json.dumps({'type': 'done', 'result': item['result']}, ensure_ascii=False)}\n\n"
                    break
                elif msg_type == "chunk_result":
                    yield f"data: {json.dumps({'type': 'chunk_result', 'data': item['data']}, ensure_ascii=False)}\n\n"
                else:  # progress
                    yield f"data: {json.dumps({'type': 'progress', 'step': item['step'], 'percent': item['percent']})}\n\n"
        finally:
            cancel_event.set()  # 无论什么原因退出，都取消 pipeline

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/v1/convert/async")
def convert_async(
    file: UploadFile = File(...),
    model: str = Form(""),
    base_url: str = Form(""),
    api_key: str = Form(""),
):
    """异步转换：在服务端线程池中运行 pipeline，通过 Redis/WS 推送进度。

    执行流程：
    1. 生成唯一 task_id，初始化任务状态（Redis hset 或内存 dict）
    2. 定义 progress_callback：每次调用时更新 Redis hash + publish 到 Pub/Sub channel
       - Redis 不可用时写入 _memory_tasks dict（供 HTTP 轮询）
    3. 在线程池中运行 run_pipeline()：
       - 成功 → 更新状态为 done，写入 result
       - 失败 → 更新状态为 failed，写入 error
    4. 返回 {"task_id": task_id} 给客户端
    5. 客户端通过 GET /api/v1/tasks/{task_id} 轮询进度
    """
    r = _get_redis()

    if not file.filename or not file.filename.lower().endswith(".txt"):
        raise HTTPException(status_code=400, detail="只接受 .txt 文件")

    content = file.file.read()
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(status_code=400, detail=f"文件过大，上限 {MAX_FILE_SIZE // 1024 // 1024} MB")
    if len(content) == 0:
        raise HTTPException(status_code=400, detail="文件为空")

    novel_name = _sanitize_name(file.filename)
    with tempfile.NamedTemporaryFile(mode="wb", suffix=".txt", delete=False) as tmp:
        tmp.write(content)
        tmp_path = tmp.name

    task_id = str(uuid.uuid4())
    task_key = f"task:{task_id}"

    # 初始化任务状态
    if r:
        r.hset(task_key, mapping={
            "status": "queued", "step": "queued", "percent": "0",
            "novel_name": novel_name,
        })
        r.expire(task_key, 3600)

    def progress_callback(step: str, percent: int):
        if r:
            r.hset(task_key, mapping={
                "step": step, "percent": str(percent), "status": "processing",
            })
            r.publish(f"task_events:{task_id}", json.dumps({
                "task_id": task_id, "step": step,
                "percent": percent, "status": "processing",
            }))
        else:
            # 无 Redis：存内存供轮询
            with _memory_tasks_lock:
                _memory_tasks_cleanup()
                _memory_tasks[task_id] = {
                    "status": "processing", "step": step, "percent": percent,
                    "_created_at": time.time(),
                }

    def run_pipeline():
        try:
            config = _apply_settings(load_config(), model, base_url, api_key)
            pipeline = Pipeline(config, progress_callback=progress_callback)
            script = pipeline.run(tmp_path, novel_name)
            result = _script_to_response(script, novel_name)

            if r:
                r.hset(task_key, mapping={
                    "status": "done", "step": "done", "percent": "100",
                    "result": json.dumps(result, ensure_ascii=False),
                })
                r.publish(f"task_events:{task_id}", json.dumps({
                    "task_id": task_id, "status": "done", "step": "done",
                    "percent": 100, "result": result,
                }, ensure_ascii=False))
            else:
                with _memory_tasks_lock:
                    _memory_tasks_cleanup()
                    _memory_tasks[task_id] = {
                        "status": "done", "step": "done", "percent": 100,
                        "result": result, "novel_name": novel_name,
                        "_created_at": time.time(),
                    }
        except Exception as e:
            error_msg = f"{type(e).__name__}: {e}"
            traceback.print_exc()
            if r:
                r.hset(task_key, mapping={
                    "status": "failed", "step": "error", "percent": "0",
                    "error": error_msg,
                })
                r.publish(f"task_events:{task_id}", json.dumps({
                    "task_id": task_id, "status": "failed",
                    "step": "error", "percent": 0, "error": error_msg,
                }))
            else:
                with _memory_tasks_lock:
                    _memory_tasks_cleanup()
                    _memory_tasks[task_id] = {
                        "status": "failed", "step": "error", "percent": 0,
                        "error": error_msg,
                        "_created_at": time.time(),
                    }
        finally:
            Path(tmp_path).unlink(missing_ok=True)
            if r:
                r.expire(task_key, 3600)

    _executor.submit(run_pipeline)

    return {"task_id": task_id, "novel_name": novel_name}


@app.get("/api/v1/tasks/{task_id}")
def get_task_status(task_id: str):  # task_id: 异步任务 ID
    """查询异步转换任务的进度和结果。

    查询流程：
    1. 先查 Redis：r.hgetall(f"task:{task_id}") 获取所有字段
       - 有数据 → 解析 status/step/percent/result/error 返回
    2. Redis 没数据 → 查内存 _memory_tasks dict（需加锁）
    3. 都没有 → 抛出 404
    """
    # 先查 Redis
    r = _get_redis()
    if r:
        key = f"task:{task_id}"
        data = r.hgetall(key)
        if data:
            info = {k.decode(): v.decode() for k, v in data.items()}
            status = info.get("status", "unknown")
            resp = {
                "task_id": task_id,
                "status": status,
                "step": info.get("step", ""),
                "percent": int(info.get("percent", 0)),
                "novel_name": info.get("novel_name", ""),
            }
            if status == "done" and "result" in info:
                resp["result"] = json.loads(info["result"])
            elif status == "failed":
                resp["error"] = info.get("error", "Unknown error")
            return resp

    # 内存后备
    with _memory_tasks_lock:
        data = _memory_tasks.get(task_id)
    if not data:
        raise HTTPException(status_code=404, detail="任务不存在")

    resp = {
        "task_id": task_id,
        "status": data.get("status", "unknown"),
        "step": data.get("step", ""),
        "percent": data.get("percent", 0),
        "novel_name": data.get("novel_name", ""),
    }

    if data.get("status") == "done" and "result" in data:
        resp["result"] = data["result"]
    elif data.get("status") == "failed":
        resp["error"] = data.get("error", "Unknown error")

    return resp


@app.post("/api/v1/chat")
def chat(req: ChatRequest):
    """常规 AI 对话，支持 SSE 流式输出。

    流程：
    1. _apply_settings() 合并用户自定义配置
    2. 构造 system prompt（含可选的 script_context）
    3. 非流式 → llm.chat() 直接返回完整回复
    4. 流式 → event_generator() 从 llm.chat_stream() 逐块 yield SSE 事件
    """
    config = _apply_settings(load_config(), req.model, req.base_url, req.api_key)
    llm = LLMClient(config)

    system_msg = "你是一个专业的编剧助手。你可以帮助用户打磨剧本、修改对白、调整情节结构。请用中文回答。"
    if req.script_context:
        system_msg += f"\n\n当前剧本上下文：\n{req.script_context}"

    messages = [{"role": "system", "content": system_msg}] + req.messages

    if not req.stream:
        try:
            reply = llm.chat(messages)
            return {"reply": reply}
        except LLMError as e:
            raise HTTPException(status_code=502, detail=f"LLM 调用失败: {e}")

    # --- SSE 流式输出 ---
    def event_generator():
        try:
            for chunk in llm.chat_stream(messages):
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        except LLMError as e:
            yield f"data: {json.dumps({'error': str(e)}, ensure_ascii=False)}\n\n"
        except Exception as e:
            traceback.print_exc()
            yield f"data: {json.dumps({'error': f'服务器内部错误: {e}'}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/v1/schema")
def get_schema():
    return SCRIPT_SCHEMA.model_json_schema()


@app.get("/api/v1/download/{novel_name}")
def download_script(novel_name: str):  # novel_name: 小说名（用于定位 YAML 文件）
    """下载已生成的 YAML 剧本文件。"""
    safe_name = re.sub(r'[\\/:*?"<>|]', '_', novel_name)[:100]
    yaml_path = Path(load_config().pipeline.output_dir) / safe_name / f"{safe_name}.yaml"
    if not yaml_path.exists():
        raise HTTPException(status_code=404, detail="剧本文件不存在")
    return FileResponse(
        yaml_path,
        media_type="application/x-yaml",
        filename=f"{safe_name}.yaml",
    )


# --- 全局异常处理 ---

@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.detail},
    )


# --- 启动入口 ---

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="127.0.0.1", port=8000, reload=True)
