"""
流程编排
========
串联"读取 → 滑动窗口分块 → 逐块调 LLM → 记忆管理 → 合并校验 → 输出"的完整流程。

Pipeline 是核心业务逻辑的入口，负责协调各模块完成小说到剧本的转换：
1. NovelReader 读取并分块小说文本（支持滑动窗口重叠）
2. 逐块构造带记忆上下文的 prompt（角色档案 + 事件记忆）
3. LLMClient 调用大模型生成 YAML 剧本片段
4. 解析并用 Pydantic 校验，更新角色档案和事件记忆
5. 合并所有块的剧本，写入 YAML 文件并生成 Schema 文档

使用方式：
    pipeline = Pipeline(config, progress_callback=my_callback)
    script = pipeline.run("novel.txt")

进度回调（progress_callback）在关键步骤触发，可用于前端进度条展示。
"""

from pathlib import Path
from typing import Callable, Optional

from src.config import Config, load_config
from src.llm_client import LLMClient
from src.parser import (
    Script, parse_yaml, script_to_yaml,
    parse_memory_update, merge_scripts, compress_event_memory,
)
from src.prompt import build_memory_prompt, build_memory_update_prompt
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
        """对一部小说执行完整流程：多块滑动窗口 + 记忆管理。"""
        novel_path = Path(novel_path)
        novel_name = novel_path.stem
        novel_dir = self.output_dir / novel_name
        novel_dir.mkdir(parents=True, exist_ok=True)

        # 1. 读取小说
        reader = NovelReader(novel_path)
        self._progress("reading", 5)
        print(f"已读取小说: {novel_path} ({reader.char_count} 字符)")

        # 2. 滑动窗口分块
        overlap = self.config.pipeline.overlap_size
        chunks = reader.chunks_with_overlap(self.config.pipeline.chunk_size, overlap)
        total_chunks = len(chunks)
        self._progress("chunking", 10)
        print(f"小说共 {total_chunks} 块（overlap={overlap} 字符）")

        # 3. 初始化记忆状态
        character_registry: dict[str, dict] = {}  # name -> {description, is_main}
        event_summaries: list[str] = []
        chunk_scripts: list[Script] = []
        next_scene_number = 1

        # 4. 逐块处理
        for i, chunk in enumerate(chunks):
            chunk_idx = i + 1
            base_pct = 10 + int(80 * i / total_chunks)
            self._progress("processing", base_pct)
            print(f"\n--- 处理第 {chunk_idx}/{total_chunks} 块 ({len(chunk)} 字符) ---")

            # ---- LLM Call 1: 生成剧本片段 ----
            event_memory_text = compress_event_memory(
                event_summaries,
                self.config.pipeline.event_memory_max_chars,
            )

            messages = build_memory_prompt(
                novel_text=chunk,
                character_registry=character_registry,
                event_memory=event_memory_text,
                chunk_index=chunk_idx,
                total_chunks=total_chunks,
                start_scene_number=next_scene_number,
            )

            self._progress("calling_llm", base_pct + 2)
            raw_output = self.llm.chat(messages)

            raw_path = novel_dir / f"raw_chunk_{chunk_idx}.txt"
            raw_path.write_text(raw_output, encoding="utf-8")

            # YAML 解析 + 重试
            self._progress("parsing", base_pct + 4)
            try:
                script_fragment = parse_yaml(raw_output)
            except ValueError as parse_err:
                print(f"块 {chunk_idx} YAML 解析失败，尝试纠错: {parse_err}")
                fix_messages = messages + [
                    {"role": "assistant", "content": raw_output},
                    {"role": "user", "content": (
                        f"YAML 解析错误：\n{parse_err}\n"
                        "请重新输出修正后的完整 YAML，不要添加额外文字。"
                    )},
                ]
                raw_output = self.llm.chat(fix_messages)
                raw_path.write_text(raw_output, encoding="utf-8")
                script_fragment = parse_yaml(raw_output)

            # 更新场景编号
            if script_fragment.scenes:
                next_scene_number = max(s.scene_number for s in script_fragment.scenes) + 1

            chunk_scripts.append(script_fragment)
            print(f"  块 {chunk_idx} 剧本: {len(script_fragment.scenes)} 场, "
                  f"{len(script_fragment.characters)} 角色")

            # ---- LLM Call 2: 更新记忆 ----
            self._progress("updating_memory", base_pct + 6)
            memory_messages = build_memory_update_prompt(
                script_fragment_yaml=raw_output,
                current_characters=character_registry,
            )
            memory_raw = self.llm.chat(memory_messages)

            try:
                memory_update = parse_memory_update(memory_raw)
            except ValueError as mem_err:
                print(f"  块 {chunk_idx} 记忆更新解析失败（跳过）: {mem_err}")
                memory_update = None

            if memory_update:
                # 更新角色注册表
                for char in memory_update.characters:
                    existing = character_registry.get(char.name)
                    if existing is None:
                        character_registry[char.name] = {
                            "description": char.description,
                            "is_main": char.is_main,
                        }
                    else:
                        if len(char.description) > len(existing["description"]):
                            character_registry[char.name]["description"] = char.description
                        if char.is_main:
                            character_registry[char.name]["is_main"] = True

                if memory_update.event_summary:
                    event_summaries.append(memory_update.event_summary)

                print(f"  记忆更新: {len(memory_update.characters)} 角色, "
                      f"事件: {memory_update.event_summary[:60]}...")

        # 5. 合并所有块的剧本
        self._progress("merging", 90)
        final_script = merge_scripts(chunk_scripts)
        print(f"\n合并完成: {len(final_script.scenes)} 场, "
              f"{len(final_script.characters)} 角色")

        # 6. 保存最终剧本
        yaml_path = novel_dir / "script.yaml"
        yaml_path.write_text(script_to_yaml(final_script), encoding="utf-8")
        self._progress("saving", 95)
        print(f"剧本已保存至: {yaml_path}")

        # 7. 生成 Schema 文档
        schema_dir = self.output_dir / "schema"
        schema_dir.mkdir(parents=True, exist_ok=True)
        generate_json_schema(schema_dir / "script_schema.json")
        generate_markdown_doc(schema_dir / "script_schema.md")
        print(f"Schema 文档已保存至: {schema_dir}")

        self._progress("done", 100)
        return final_script


def run_pipeline(novel_path: str) -> Script:
    """快捷函数：加载配置并运行流程。"""
    config = load_config()
    pipeline = Pipeline(config)
    return pipeline.run(novel_path)
