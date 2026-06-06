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
import json
import tempfile
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import redis as redis_lib
from fastapi import FastAPI, File, UploadFile, HTTPException, Form
from fastapi.responses import JSONResponse, FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from rq import Queue
from starlette.websockets import WebSocket, WebSocketDisconnect

from src.config import Config, ApiConfig, PipelineConfig, load_config
from src.llm_client import LLMError, LLMClient
from src.parser import SCRIPT_SCHEMA
from src.pipeline import Pipeline
from src.response import script_to_response
from ws_manager import ConnectionManager

app = FastAPI(
    title="Novel2Script",
    description="将小说文本转换为结构化剧本",
    version="0.1.0",
)

app.mount("/static", StaticFiles(directory="static"), name="static")

MAX_FILE_SIZE = 5 * 1024 * 1024  # 5 MB

# WebSocket 连接管理器
manager = ConnectionManager()
_executor = ThreadPoolExecutor(max_workers=4)


# --- 请求模型 ---

class ChatRequest(BaseModel):
    messages: list[dict]
    script_context: str = ""
    model: str = ""
    base_url: str = ""
    api_key: str = ""
    stream: bool = True


# --- 工具 ---

def _apply_settings(base: Config, model: str, base_url: str, api_key: str) -> Config:
    """用用户提供的值覆盖默认配置。"""
    if model:
        base.model = model
    if base_url:
        base.api.base_url = base_url
    if api_key:
        base.api.api_key = api_key
    return base


# --- 响应模型 ---

def _script_to_response(script, novel_name: str) -> dict:
    return script_to_response(script, novel_name)


# --- Redis 辅助 ---

def _get_redis():
    """返回 Redis 连接，不可用时返回 None。"""
    config = load_config()
    if not config.redis.enabled:
        return None
    try:
        r = redis_lib.Redis.from_url(config.redis.url)
        r.ping()
        return r
    except Exception:
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

    消息协议（客户端→服务端）：
    - {action: "chat", request_id, messages, script_context, model, base_url, api_key}
    - {action: "subscribe_task", task_id}
    - {action: "unsubscribe_task", task_id}
    - {action: "pong"}  —— 回复服务端的心跳 ping

    消息协议（服务端→客户端）：
    - {type: "chat_chunk", request_id, chunk_type, content}
    - {type: "chat_done", request_id}
    - {type: "chat_error", request_id, error}
    - {type: "task_progress", task_id, step, percent}
    - {type: "task_done", task_id, result}
    - {type: "task_failed", task_id, error}
    - {type: "ping"}
    - {type: "health", model, base_url}
    """
    client_id = str(uuid.uuid4())
    await manager.connect(websocket, client_id)

    # 连接建立后立即发送健康信息，前端据此显示当前模型
    config = load_config()
    await manager.send_to_client(client_id, {
        "type": "health",
        "model": config.model,
        "base_url": config.api.base_url,
    })

    # 启动心跳循环，每 30 秒发一次 ping，客户端需回 pong
    heartbeat_task = asyncio.create_task(_heartbeat_loop(client_id))

    try:
        while True:
            data = await websocket.receive_json()
            action = data.get("action")
            if action == "chat":
                # 异步处理聊天，不阻塞消息接收循环
                asyncio.create_task(_handle_ws_chat(client_id, data))
            elif action == "subscribe_task":
                # 订阅异步任务的进度推送
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
        pass
    finally:
        heartbeat_task.cancel()
        await manager.disconnect(client_id)


async def _heartbeat_loop(client_id: str, interval: int = 30):
    while True:
        await asyncio.sleep(interval)
        try:
            await manager.send_to_client(client_id, {"type": "ping"})
        except Exception:
            break


async def _handle_ws_chat(client_id: str, data: dict):
    """WebSocket 聊天处理，流式推送 chunk。

    实现模式：生产者-消费者
    - 生产者：在线程池中运行同步生成器 chat_stream()
    - 消费者：在 async 上下文中等待 Queue 消息并推送给客户端
    - 使用 asyncio.Queue 桥接同步和异步世界
    - None 作为结束信号，Exception 作为错误信号
    """
    request_id = data.get("request_id", "")
    try:
        config = _apply_settings(
            load_config(),
            data.get("model", ""),
            data.get("base_url", ""),
            data.get("api_key", ""),
        )
        llm = LLMClient(config)

        system_msg = "你是一个专业的编剧助手。你可以帮助用户打磨剧本、修改对白、调整情节结构。请用中文回答。"
        if data.get("script_context"):
            system_msg += f"\n\n当前剧本上下文：\n{data['script_context']}"

        messages = [{"role": "system", "content": system_msg}] + data["messages"]

        # chat_stream 是同步生成器，用线程池 + Queue 避免阻塞事件循环
        queue: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_event_loop()

        def _produce():
            """线程池中运行：逐个 chunk 放入 Queue，None 表示结束。"""
            try:
                for chunk in llm.chat_stream(messages):
                    asyncio.run_coroutine_threadsafe(queue.put(chunk), loop)
            except Exception as e:
                asyncio.run_coroutine_threadsafe(queue.put(e), loop)
            finally:
                asyncio.run_coroutine_threadsafe(queue.put(None), loop)

        _executor.submit(_produce)

        # 消费者循环：从 Queue 取出 chunk 推送给客户端
        while True:
            item = await queue.get()
            if item is None:  # 生产者已完成
                break
            if isinstance(item, Exception):  # 生产者出错
                await manager.send_to_client(client_id, {
                    "type": "chat_error",
                    "request_id": request_id,
                    "error": str(item),
                })
                return
            await manager.send_to_client(client_id, {
                "type": "chat_chunk",
                "request_id": request_id,
                "chunk_type": item["type"],
                "content": item["content"],
            })

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
        traceback.print_exc()
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
    """上传小说 .txt 文件，返回转换后的剧本。"""
    if not file.filename or not file.filename.lower().endswith(".txt"):
        raise HTTPException(status_code=400, detail="只接受 .txt 文件")

    content = file.file.read()
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(status_code=400, detail=f"文件过大，上限 {MAX_FILE_SIZE // 1024 // 1024} MB")
    if len(content) == 0:
        raise HTTPException(status_code=400, detail="文件为空")

    novel_name = Path(file.filename).stem
    with tempfile.NamedTemporaryFile(mode="wb", suffix=".txt", delete=False) as tmp:
        tmp.write(content)
        tmp_path = tmp.name

    try:
        config = _apply_settings(load_config(), model, base_url, api_key)
        pipeline = Pipeline(config)
        script = pipeline.run(tmp_path)
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


@app.post("/api/v1/convert/async")
def convert_async(
    file: UploadFile = File(...),
    model: str = Form(""),
    base_url: str = Form(""),
    api_key: str = Form(""),
):
    """异步转换：提交任务到 rq 队列，立即返回 task_id。

    流程：
    1. 校验文件并保存到临时目录
    2. 在 Redis 中初始化任务状态（Hash）
    3. 将任务提交到 rq 队列，worker 会异步执行
    4. 前端通过 WebSocket subscribe_task 或 HTTP 轮询获取进度
    """
    r = _get_redis()
    if r is None:
        raise HTTPException(status_code=503, detail="Redis 不可用，请使用 /api/v1/convert 同步模式")

    if not file.filename or not file.filename.lower().endswith(".txt"):
        raise HTTPException(status_code=400, detail="只接受 .txt 文件")

    content = file.file.read()
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(status_code=400, detail=f"文件过大，上限 {MAX_FILE_SIZE // 1024 // 1024} MB")
    if len(content) == 0:
        raise HTTPException(status_code=400, detail="文件为空")

    novel_name = Path(file.filename).stem
    with tempfile.NamedTemporaryFile(mode="wb", suffix=".txt", delete=False) as tmp:
        tmp.write(content)
        tmp_path = tmp.name

    task_id = str(uuid.uuid4())

    # 初始化任务状态，1 小时后自动过期
    r.hset(f"task:{task_id}", mapping={
        "status": "queued",
        "step": "queued",
        "percent": "0",
        "novel_name": novel_name,
    })
    r.expire(f"task:{task_id}", 3600)

    # 提交到 rq 队列，worker.run_conversion 会在独立进程中执行
    q = Queue(connection=r)
    q.enqueue(
        "worker.run_conversion",
        task_id, tmp_path, model, base_url, api_key,
        job_id=task_id,
        job_timeout="10m",
    )

    return {"task_id": task_id, "novel_name": novel_name}


@app.get("/api/v1/tasks/{task_id}")
def get_task_status(task_id: str):
    """查询异步转换任务的进度和结果。"""
    r = _get_redis()
    if r is None:
        raise HTTPException(status_code=503, detail="Redis 不可用")

    key = f"task:{task_id}"
    data = r.hgetall(key)
    if not data:
        raise HTTPException(status_code=404, detail="任务不存在")

    info = {k.decode(): v.decode() for k, v in data.items()}
    status = info.get("status", "unknown")

    response = {
        "task_id": task_id,
        "status": status,
        "step": info.get("step", ""),
        "percent": int(info.get("percent", 0)),
        "novel_name": info.get("novel_name", ""),
    }

    if status == "done" and "result" in info:
        response["result"] = json.loads(info["result"])
    elif status == "failed":
        response["error"] = info.get("error", "Unknown error")

    return response


@app.post("/api/v1/chat")
def chat(req: ChatRequest):
    """常规 AI 对话，支持 SSE 流式输出。"""
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
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
