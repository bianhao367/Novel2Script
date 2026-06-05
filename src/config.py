"""配置加载器 —— 读取 .config 文件（INI 格式）并解析 ${VAR} 环境变量占位符。"""

import os
import re
from configparser import ConfigParser
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ApiConfig:
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    model: str = "gpt-4o"
    temperature: float = 0.7
    max_tokens: int = 4096


@dataclass
class PipelineConfig:
    chunk_size: int = 3000
    output_dir: str = "./output"
    output_format: str = "yaml"


@dataclass
class Config:
    api: ApiConfig = field(default_factory=ApiConfig)
    pipeline: PipelineConfig = field(default_factory=PipelineConfig)


def _resolve_env(value: str) -> str:
    """将字符串中的 ${VAR_NAME} 替换为对应的环境变量值。"""
    pattern = re.compile(r"\$\{(\w+)\}")
    for var in pattern.findall(value):
        value = value.replace(f"${{{var}}}", os.environ.get(var, ""))
    return value


def load_config(path: str | Path = ".config") -> Config:
    """从 .config 文件加载配置。"""
    cp = ConfigParser()
    cp.read(path, encoding="utf-8")

    api_section = dict(cp.items("api")) if cp.has_section("api") else {}
    pipeline_section = dict(cp.items("pipeline")) if cp.has_section("pipeline") else {}

    api_cfg = ApiConfig(
        base_url=_resolve_env(api_section.get("base_url", ApiConfig.base_url)),
        api_key=_resolve_env(api_section.get("api_key", "")),
        model=api_section.get("model", ApiConfig.model),
        temperature=float(api_section.get("temperature", ApiConfig.temperature)),
        max_tokens=int(api_section.get("max_tokens", ApiConfig.max_tokens)),
    )

    pipeline_cfg = PipelineConfig(
        chunk_size=int(pipeline_section.get("chunk_size", PipelineConfig.chunk_size)),
        output_dir=pipeline_section.get("output_dir", PipelineConfig.output_dir),
        output_format=pipeline_section.get("output_format", PipelineConfig.output_format),
    )

    return Config(api=api_cfg, pipeline=pipeline_cfg)
