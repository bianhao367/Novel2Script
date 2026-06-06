"""
rq Worker
=========
后台执行 Pipeline.run()，将进度和结果写入 Redis。

由 server.py 的 /api/v1/convert/async 端点通过 rq 队列调度。
每个任务通过 Redis Hash（task:{task_id}）跟踪状态，并通过
Redis Pub/Sub（task_events:{task_id}）实时推送进度给前端。

启动方式（需先启动 Redis）：
    rq worker
    # 或指定队列：rq worker default

状态流转：queued → processing → done/failed
"""

import json
import traceback
from pathlib import Path

import redis

from src.config import load_config
from src.pipeline import Pipeline
from src.response import script_to_response


def run_conversion(task_id: str, tmp_path: str, model: str, base_url: str, api_key: str):
    """rq worker 入口函数，被 server.py 通过 rq.enqueue("worker.run_conversion", ...) 调度。

    参数:
        task_id: 唯一任务 ID（UUID）
        tmp_path: 临时文件路径（上传的小说 .txt）
        model: LLM 模型名称（空则用默认配置）
        base_url: API 基础 URL（空则用默认配置）
        api_key: API 密钥（空则用默认配置）

    数据流：
    1. 通过 Redis Hash（task:{task_id}）持久化状态，供 HTTP 查询
    2. 通过 Redis Pub/Sub（task_events:{task_id}）实时推送进度给 WebSocket
    """
    r = redis.Redis.from_url(load_config().redis.url)
    key = f"task:{task_id}"

    def progress_callback(step: str, percent: int):
        """双写进度：Hash 持久化 + Pub/Sub 实时推送。"""
        r.hset(key, mapping={
            "step": step,
            "percent": str(percent),
            "status": "processing",
        })
        r.publish(f"task_events:{task_id}", json.dumps({
            "task_id": task_id,
            "step": step,
            "percent": percent,
            "status": "processing",
        }))

    try:
        r.hset(key, mapping={"status": "processing", "step": "queued", "percent": "0"})

        config = load_config()
        if model:
            config.model = model
        if base_url:
            config.api.base_url = base_url
        if api_key:
            config.api.api_key = api_key

        pipeline = Pipeline(config, progress_callback=progress_callback)
        script = pipeline.run(tmp_path)

        result = script_to_response(script, Path(tmp_path).stem)
        r.hset(key, mapping={
            "status": "done",
            "step": "done",
            "percent": "100",
            "result": json.dumps(result, ensure_ascii=False),
        })
        r.publish(f"task_events:{task_id}", json.dumps({
            "task_id": task_id,
            "status": "done",
            "step": "done",
            "percent": 100,
            "result": result,
        }, ensure_ascii=False))
    except Exception as e:
        error_msg = f"{type(e).__name__}: {e}"
        r.hset(key, mapping={
            "status": "failed",
            "step": "error",
            "percent": "0",
            "error": error_msg,
        })
        r.publish(f"task_events:{task_id}", json.dumps({
            "task_id": task_id,
            "status": "failed",
            "step": "error",
            "percent": 0,
            "error": error_msg,
        }))
        traceback.print_exc()
    finally:
        Path(tmp_path).unlink(missing_ok=True)
        r.expire(key, 3600)
