"""Novel2Script API —— 将小说 .txt 文件转换为结构化剧本的 HTTP 服务。"""

import json
import tempfile
import traceback
import uuid
from pathlib import Path

import redis as redis_lib
from fastapi import FastAPI, File, UploadFile, HTTPException, Form
from fastapi.responses import JSONResponse, FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from rq import Queue

from src.config import Config, ApiConfig, PipelineConfig, load_config
from src.llm_client import LLMError, LLMClient
from src.parser import SCRIPT_SCHEMA
from src.pipeline import Pipeline
from src.response import script_to_response

app = FastAPI(
    title="Novel2Script",
    description="将小说文本转换为结构化剧本",
    version="0.1.0",
)

app.mount("/static", StaticFiles(directory="static"), name="static")

MAX_FILE_SIZE = 5 * 1024 * 1024  # 5 MB


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
    """异步转换：提交任务到 rq 队列，立即返回 task_id。"""
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

    r.hset(f"task:{task_id}", mapping={
        "status": "queued",
        "step": "queued",
        "percent": "0",
        "novel_name": novel_name,
    })
    r.expire(f"task:{task_id}", 3600)

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
