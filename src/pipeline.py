"""
流程编排
========
串联"读取 → 构造 prompt → 调用 LLM → 解析校验 → 输出"的完整流程。

Pipeline 是核心业务逻辑的入口，负责协调各模块完成小说到剧本的转换：
1. NovelReader 读取并分块小说文本
2. prompt 构造器组装 LLM 对话消息
3. LLMClient 调用大模型生成 YAML 剧本
4. parser 解析并用 Pydantic 校验输出
5. 结果写入 YAML 文件并生成 Schema 文档

使用方式：
    pipeline = Pipeline(config, progress_callback=my_callback)
    script = pipeline.run("novel.txt")

进度回调（progress_callback）在关键步骤触发，可用于前端进度条展示。
"""

from pathlib import Path
from typing import Callable, Optional

from src.config import Config, load_config
from src.llm_client import LLMClient
from src.parser import Script, parse_yaml, script_to_yaml
from src.prompt import build_prompt
from src.reader import NovelReader
from src.schema_gen import generate_json_schema, generate_markdown_doc


class Pipeline:
    """编排小说到剧本的完整转换流程。

    属性:
        config: 全局配置对象
        llm: LLM 客户端实例
        output_dir: 输出根目录
        _progress: 进度回调函数 (step_name, percent)
    """

    def __init__(self, config: Config, progress_callback: Optional[Callable[[str, int], None]] = None):
        self.config = config
        self.llm = LLMClient(config)
        self.output_dir = Path(config.pipeline.output_dir)
        self._progress = progress_callback or (lambda step, pct: None)

    def run(self, novel_path: str | Path) -> Script:
        """对一部小说执行完整流程，返回校验后的 Script 对象并写入输出文件。"""
        novel_path = Path(novel_path)
        novel_name = novel_path.stem  # 取文件名（不含扩展名）

        # 每本小说独立的输出目录
        novel_dir = self.output_dir / novel_name
        novel_dir.mkdir(parents=True, exist_ok=True)

        # 1. 读取小说
        reader = NovelReader(novel_path)
        self._progress("reading", 10)
        print(f"已读取小说: {novel_path} ({reader.char_count} 字符)")

        # 2. 分块（目前仅处理第一块，多块合并功能待实现）
        chunks = reader.chunks(self.config.pipeline.chunk_size)
        chunk = chunks[0]
        self._progress("chunking", 20)
        if len(chunks) > 1:
            print(f"小说共 {len(chunks)} 块；当前仅处理第一块（多块合并功能尚未实现）")

        # 3. 构造 prompt
        messages = build_prompt(chunk)
        self._progress("prompting", 30)
        print(f"已构造 prompt: {len(messages)} 条消息, 约 {len(chunk)} 字符的小说文本")

        # 4. 调用 LLM
        self._progress("calling_llm", 40)
        print(f"正在调用 {self.config.model} ...")
        raw_output = self.llm.chat(messages)
        self._progress("llm_complete", 70)
        print(f"收到响应: {len(raw_output)} 字符")

        # 保存原始输出（便于调试）
        raw_path = novel_dir / "raw.txt"
        raw_path.write_text(raw_output, encoding="utf-8")
        print(f"原始输出已保存至: {raw_path}")

        # 5. 解析与校验
        script = parse_yaml(raw_output)
        self._progress("parsing", 85)
        dialogue_count = sum(
            1 for s in script.scenes for c in s.content if c.type == "dialogue"
        )
        action_count = sum(
            1 for s in script.scenes for c in s.content if c.type == "action"
        )
        print(f"已校验剧本: {len(script.scenes)} 场, {dialogue_count} 句对白, {action_count} 条动作, {len(script.characters)} 个角色")

        # 6. 保存规范化 YAML
        yaml_path = novel_dir / "script.yaml"
        yaml_path.write_text(script_to_yaml(script), encoding="utf-8")
        self._progress("saving", 95)
        print(f"剧本已保存至: {yaml_path}")

        # 7. 生成 Schema 文档（全局共享，放在 output 根目录）
        schema_dir = self.output_dir / "schema"
        schema_dir.mkdir(parents=True, exist_ok=True)
        generate_json_schema(schema_dir / "script_schema.json")
        generate_markdown_doc(schema_dir / "script_schema.md")
        print(f"Schema 文档已保存至: {schema_dir}")

        self._progress("done", 100)
        return script


def run_pipeline(novel_path: str) -> Script:
    """快捷函数：加载配置并运行流程。"""
    config = load_config()
    pipeline = Pipeline(config)
    return pipeline.run(novel_path)
