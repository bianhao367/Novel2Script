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
    base_url: str = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")  # API 基础地址
    api_key: str = os.getenv("OPENAI_API_KEY", "")  # API 密钥


@dataclass
class PipelineConfig:
    """小说转换流程的参数配置。"""
    chunk_size: int = 3000     # 小说分块大小（字符数），每块独立送 LLM 处理
    output_dir: str = "./output"  # 输出目录，每本小说生成独立子目录
    overlap_size: int = 500          # 滑动窗口重叠字符数，保证上下文连贯
    event_memory_max_chars: int = 1500  # 事件记忆最大字符数，超过自动压缩
    director_enabled: bool = True    # 是否启用导演 Agent 全局预读
    director_chunk_size: int = 15000  # 导演预读的分块大小
    review_max_rounds: int = 3       # 审查最大轮次


@dataclass
class RedisConfig:
    """Redis 连接配置，用于异步任务队列和进度推送。"""
    url: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    enabled: bool = os.getenv("REDIS_ENABLED", "false").lower() == "true"


@dataclass
class Config:
    """全局配置对象，聚合 API、Pipeline、Redis 三部分配置。"""
    api: ApiConfig = field(default_factory=ApiConfig)  # API 连接配置
    pipeline: PipelineConfig = field(default_factory=PipelineConfig)  # 流程参数配置
    redis: RedisConfig = field(default_factory=RedisConfig)  # Redis 配置
    model: str = ""            # LLM 模型名称（从 config.py 读取）
    temperature: float = 0.7   # 生成温度，越高越随机
    max_tokens: int = 4096     # 单次最大生成 token 数


# 配置缓存：避免每次请求都重新执行 config.py
# load_config() 每次调用都会动态 import config.py，如果不缓存既慢又可能有副作用
# 通过比较文件修改时间（mtime）判断是否需要重新加载
_cached_config: Config | None = None
_config_mtime: float = 0.0  # 记录 config.py 的文件修改时间，用于判断是否需要重新加载


def load_config() -> Config:
    """加载配置：API 凭据来自 .env，运行参数来自根目录 config.py。

    加载流程：
    1. 获取 config.py 的文件修改时间（mtime）
    2. 如果缓存存在且 mtime 未变 → 直接返回缓存
    3. 用 importlib.util.spec_from_file_location() 动态加载 config.py
    4. 从 config.py 中读取变量（getattr 读取，不存在用默认值）
    5. 组装 Config 对象，缓存到 _cached_config

    配置分层：
    - .env 存敏感信息（API Key），不提交 Git
    - config.py 存运行参数（模型名、分块大小），可提交 Git 供团队共享
    """
    global _cached_config, _config_mtime

    root_config_path = ROOT_DIR / "config.py"
    current_mtime = root_config_path.stat().st_mtime  # 获取文件最后修改时间

    # 如果缓存存在且文件没被修改过，直接返回缓存
    if _cached_config is not None and current_mtime == _config_mtime:
        return _cached_config

    # 文件被修改了，重新加载
    import importlib.util

    spec = importlib.util.spec_from_file_location("root_config", root_config_path)
    root_cfg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(root_cfg)  # 执行 config.py，里面的变量就变成了 root_cfg 的属性

    api_cfg = ApiConfig()  # API 配置从 .env 读取，这里不需要再覆盖
    # getattr(root_cfg, "变量名", 默认值)：从 config.py 中读取变量，不存在则用默认值
    # 这样用户只改 config.py 中关心的参数，其他保持默认
    pipeline_cfg = PipelineConfig(
        chunk_size=getattr(root_cfg, "CHUNK_SIZE", 3000),
        output_dir=getattr(root_cfg, "OUTPUT_DIR", "./output"),
        overlap_size=getattr(root_cfg, "OVERLAP_SIZE", 500),
        event_memory_max_chars=getattr(root_cfg, "EVENT_MEMORY_MAX_CHARS", 1500),
        director_enabled=getattr(root_cfg, "DIRECTOR_ENABLED", True),
        director_chunk_size=getattr(root_cfg, "DIRECTOR_CHUNK_SIZE", 15000),
        review_max_rounds=getattr(root_cfg, "REVIEW_MAX_ROUNDS", 3),
    )

    _cached_config = Config(
        api=api_cfg,
        pipeline=pipeline_cfg,
        model=getattr(root_cfg, "MODEL", "gpt-4o"),
        temperature=getattr(root_cfg, "TEMPERATURE", 0.7),
        max_tokens=getattr(root_cfg, "MAX_TOKENS", 4096),
    )
    _config_mtime = current_mtime
    return _cached_config
