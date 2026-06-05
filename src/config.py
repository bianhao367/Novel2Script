"""配置加载器 —— 从 .env 读取 API 凭据，从 config.py 读取运行参数，统一为 Config 对象。"""

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
    base_url: str = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    api_key: str = os.getenv("OPENAI_API_KEY", "")


@dataclass
class PipelineConfig:
    chunk_size: int = 3000
    output_dir: str = "./output"
    output_format: str = "yaml"


@dataclass
class Config:
    api: ApiConfig = field(default_factory=ApiConfig)
    pipeline: PipelineConfig = field(default_factory=PipelineConfig)
    # 额外存储原始读取值供参考
    model: str = ""
    temperature: float = 0.7
    max_tokens: int = 4096


def load_config() -> Config:
    """加载配置：API 凭据来自 .env，运行参数来自根目录 config.py。"""
    import importlib.util

    # 从根目录 config.py 读取参数
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
