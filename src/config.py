"""
配置加载器
==========
从 .env 读取 API 凭据（OPENAI_BASE_URL、OPENAI_API_KEY），
从根目录 config.py 读取运行参数（MODEL、CHUNK_SIZE 等），
统一为 Config 对象供 Pipeline 和 LLMClient 使用。

配置优先级：用户前端设置（localStorage） > .env > config.py > 代码默认值
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# 项目根目录（main.py 所在位置）
ROOT_DIR = Path(__file__).resolve().parent.parent

# 加载 .env 中的环境变量
load_dotenv(ROOT_DIR / ".env")


@dataclass
class ApiConfig:
    """OpenAI 兼容 API 的连接配置。"""
    base_url: str = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    api_key: str = os.getenv("OPENAI_API_KEY", "")


@dataclass
class PipelineConfig:
    """小说转换流程的参数配置。"""
    chunk_size: int = 3000     # 小说分块大小（字符数），每块独立送 LLM 处理
    output_dir: str = "./output"  # 输出目录，每本小说生成独立子目录
    output_format: str = "yaml"   # 剧本输出格式（yaml/json）


@dataclass
class RedisConfig:
    """Redis 连接配置，用于异步任务队列和进度推送。"""
    url: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    enabled: bool = os.getenv("REDIS_ENABLED", "true").lower() == "true"


@dataclass
class Config:
    """全局配置对象，聚合 API、Pipeline、Redis 三部分配置。"""
    api: ApiConfig = field(default_factory=ApiConfig)
    pipeline: PipelineConfig = field(default_factory=PipelineConfig)
    redis: RedisConfig = field(default_factory=RedisConfig)
    model: str = ""            # LLM 模型名称（从 config.py 读取）
    temperature: float = 0.7   # 生成温度，越高越随机
    max_tokens: int = 4096     # 单次最大生成 token 数


def load_config() -> Config:
    """
    加载配置：API 凭据来自 .env，运行参数来自根目录 config.py。

    根目录 config.py 是一个普通 Python 文件，通过 importlib 动态加载，
    读取其中的 MODEL、CHUNK_SIZE、OUTPUT_DIR 等变量。
    """
    import importlib.util

    root_config_path = ROOT_DIR / "config.py"
    spec = importlib.util.spec_from_file_location("root_config", root_config_path)
    root_cfg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(root_cfg)

    api_cfg = ApiConfig()
    pipeline_cfg = PipelineConfig(
        chunk_size=getattr(root_cfg, "CHUNK_SIZE", 3000),
        output_dir=getattr(root_cfg, "OUTPUT_DIR", "./output"),
        output_format=getattr(root_cfg, "OUTPUT_FORMAT", "yaml"),
    )

    return Config(
        api=api_cfg,
        pipeline=pipeline_cfg,
        model=getattr(root_cfg, "MODEL", "gpt-4o"),
        temperature=getattr(root_cfg, "TEMPERATURE", 0.7),
        max_tokens=getattr(root_cfg, "MAX_TOKENS", 4096),
    )
