"""rq worker — 后台执行 Pipeline.run，将进度写入 Redis。"""

import json
import traceback
from pathlib import Path

import redis

from src.config import load_config
from src.pipeline import Pipeline
from src.response import script_to_response


def run_conversion(task_id: str, tmp_path: str, model: str, base_url: str, api_key: str):
    """rq worker 入口函数。通过 Redis hash task:{task_id} 跟踪进度。"""
    r = redis.Redis.from_url(load_config().redis.url)
    key = f"task:{task_id}"

    def progress_callback(step: str, percent: int):
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
