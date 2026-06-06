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

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Optional

from src.config import Config, load_config
from src.llm_client import LLMClient
from src.parser import (
    Script, Scene, DialogueItem, ActionItem, CharacterInfo,
    parse_yaml, script_to_yaml,
    parse_memory_update, parse_review_result, parse_novel_analysis,
    parse_chunk_outline, parse_dialogue_script, parse_action_script,
    merge_expert_outputs,
    merge_scripts, compress_event_memory,
    ChunkOutline, DialogueScript, ActionScript,
)
from src.prompt import (
    build_memory_prompt, build_memory_update_prompt,
    build_review_prompt, build_director_prompt, build_fix_prompt,
    build_orchestrator_prompt, build_dialogue_agent_prompt, build_action_agent_prompt,
)
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

    def __init__(
        self,
        config: Config,
        progress_callback: Optional[Callable[[str, int], None]] = None,
        chunk_result_callback: Optional[Callable[[dict], None]] = None,
    ):
        self.config = config
        self.llm = LLMClient(config)
        self.output_dir = Path(config.pipeline.output_dir)
        self._progress = progress_callback or (lambda step, pct: None)
        self._chunk_result = chunk_result_callback or (lambda data: None)

    def run(self, novel_path: str | Path, novel_name: str | None = None) -> Script:
        """对一部小说执行完整流程：导演预读 → 滑动窗口分块 → 逐块生成 → 多轮审查 → 合并输出。"""
        novel_path = Path(novel_path)
        if novel_name is None:
            novel_name = novel_path.stem
        novel_dir = self.output_dir / novel_name
        novel_dir.mkdir(parents=True, exist_ok=True)

        # 1. 读取小说
        reader = NovelReader(novel_path)
        self._progress("reading", 5)
        print(f"已读取小说: {novel_path} ({reader.char_count} 字符)")

        # 2. 初始化记忆状态
        character_registry: dict[str, dict] = {}  # name -> {description, is_main}
        event_summaries: list[str] = []
        chunk_scripts: list[Script] = []
        next_scene_number = 1

        # 3. Phase 1: 导演预读（全局分析）
        if self.config.pipeline.director_enabled:
            self._run_director_phase(reader, novel_dir, character_registry, event_summaries)

        # 4. Phase 2: 滑动窗口分块 + 逐块生成剧本
        overlap = self.config.pipeline.overlap_size
        chunks = reader.chunks_with_overlap(self.config.pipeline.chunk_size, overlap)
        total_chunks = len(chunks)
        phase2_start_pct = 40 if self.config.pipeline.director_enabled else 10
        self._progress("chunking", phase2_start_pct)
        print(f"小说共 {total_chunks} 块（overlap={overlap} 字符）")

        if total_chunks == 0:
            print("警告：小说内容为空，无法生成剧本")
            self._progress("done", 100)
            return Script()

        for i, chunk in enumerate(chunks):
            chunk_idx = i + 1
            base_pct = phase2_start_pct + int(50 * i / total_chunks)
            self._progress("processing", base_pct)
            print(f"\n--- 处理第 {chunk_idx}/{total_chunks} 块 ({len(chunk)} 字符) ---")

            # ---- Step 1: 编排 Agent ----
            self._progress("orchestrating", base_pct + 1)
            outline = self._call_orchestrator(chunk, character_registry, next_scene_number)

            if not outline.scenes:
                print(f"  块 {chunk_idx} 编排结果为空，跳过")
                continue

            # 将原文按段落切分，按编排结果分类
            paragraphs = chunk.split("\n\n")
            para_map: dict[int, str] = {}
            for idx, para in enumerate(paragraphs):
                cleaned = para.strip()
                if cleaned:
                    para_map[idx] = cleaned

            dialogue_paras: list[tuple[int, str]] = []
            action_paras: list[tuple[int, str]] = []
            for scene in outline.scenes:
                for pa in scene.paragraphs:
                    text = para_map.get(pa.para_idx, "")
                    if text:
                        if pa.content_type == "dialogue":
                            dialogue_paras.append((pa.para_idx, text))
                        else:
                            action_paras.append((pa.para_idx, text))

            print(f"  编排: {len(outline.scenes)} 场, "
                  f"对白段 {len(dialogue_paras)}, 动作段 {len(action_paras)}")

            # ---- Step 2: 并行专家 (对白 + 动作 + 记忆) ----
            self._progress("calling_experts", base_pct + 3)

            def call_dialogue():
                if not dialogue_paras:
                    return DialogueScript(items=[])
                msgs = build_dialogue_agent_prompt(
                    chunk, character_registry, dialogue_paras, next_scene_number,
                )
                return parse_dialogue_script(self.llm.chat(msgs))

            def call_action():
                if not action_paras:
                    return ActionScript(items=[])
                msgs = build_action_agent_prompt(
                    chunk, character_registry, action_paras, next_scene_number,
                )
                return parse_action_script(self.llm.chat(msgs))

            def call_memory():
                msgs = build_memory_update_prompt(
                    script_fragment_yaml="",  # 无剧本，用原文
                    current_characters=character_registry,
                )
                # 记忆专家用原文而非剧本提取
                memory_msgs = [
                    {"role": "system", "content": msgs[0]["content"]},
                    {"role": "user", "content": (
                        "请根据以下小说片段提取角色信息和剧情摘要：\n\n" + chunk
                    )},
                ]
                return parse_memory_update(self.llm.chat(memory_msgs, max_tokens=2048))

            with ThreadPoolExecutor(max_workers=3) as executor:
                f_dialogue = executor.submit(call_dialogue)
                f_action = executor.submit(call_action)
                f_memory = executor.submit(call_memory)

                try:
                    dialogue_script = f_dialogue.result()
                except ValueError as e:
                    print(f"  块 {chunk_idx} 对白专家解析失败: {e}")
                    dialogue_script = DialogueScript(items=[])

                try:
                    action_script = f_action.result()
                except ValueError as e:
                    print(f"  块 {chunk_idx} 动作专家解析失败: {e}")
                    action_script = ActionScript(items=[])

                try:
                    memory_update = f_memory.result()
                except ValueError as e:
                    print(f"  块 {chunk_idx} 记忆专家解析失败: {e}")
                    memory_update = None

            print(f"  专家结果: 对白 {len(dialogue_script.items)} 条, "
                  f"动作 {len(action_script.items)} 条")

            # ---- Step 3: 合并专家输出 ----
            script_fragment = merge_expert_outputs(
                outline, dialogue_script, action_script, [],
            )

            # 序列化为 YAML 供审查和记忆使用
            raw_output = script_to_yaml(script_fragment)
            raw_path = novel_dir / f"raw_chunk_{chunk_idx}.txt"
            raw_path.write_text(raw_output, encoding="utf-8")

            # ---- Step 4: 多轮审查闭环 ----
            raw_output_before_review = raw_output
            script_fragment_before_review = script_fragment
            max_rounds = self.config.pipeline.review_max_rounds

            for review_round in range(max_rounds):
                self._progress("reviewing", min(base_pct + 5 + review_round, 89))

                review_messages = build_review_prompt(
                    script_fragment_yaml=raw_output,
                    current_characters=character_registry,
                    start_scene_number=next_scene_number,
                )
                review_raw = self.llm.chat(review_messages, max_tokens=2048)

                try:
                    review_result = parse_review_result(review_raw)
                except ValueError as rev_err:
                    print(f"  块 {chunk_idx} 审查结果解析失败（跳过审查）: {rev_err}")
                    break

                if review_result.valid:
                    break

                print(f"  块 {chunk_idx} 审查第 {review_round + 1} 轮不通过: {review_result.issues}")

                if review_round == max_rounds - 1:
                    continue

                fix_messages = build_fix_prompt(
                    script_fragment_yaml=raw_output,
                    review_issues=review_result.issues,
                    review_suggestions=review_result.suggestions,
                    original_novel_text=chunk,
                )
                fix_raw = self.llm.chat(fix_messages, max_tokens=4096)

                try:
                    script_fragment = parse_yaml(fix_raw)
                    raw_output = fix_raw
                except ValueError as e:
                    print(f"  块 {chunk_idx} 修正后解析失败: {e}")
                    break
            else:
                print(f"  块 {chunk_idx} 审查 {max_rounds} 轮均未通过，回滚到原始版本")
                raw_output = raw_output_before_review
                script_fragment = script_fragment_before_review

            raw_path.write_text(raw_output, encoding="utf-8")

            # 更新场景编号
            if script_fragment.scenes:
                next_scene_number = max(s.scene_number for s in script_fragment.scenes) + 1

            chunk_scripts.append(script_fragment)
            print(f"  块 {chunk_idx} 剧本: {len(script_fragment.scenes)} 场, "
                  f"{len(script_fragment.characters)} 角色")

            # 推送本块的中间结果（前端可增量渲染）
            self._chunk_result({
                "chunk_index": chunk_idx,
                "total_chunks": total_chunks,
                "scenes": [
                    {
                        "scene_number": s.scene_number,
                        "slugline": s.slugline,
                        "content": [item.model_dump() for item in s.content],
                    }
                    for s in script_fragment.scenes
                ],
                "characters": [
                    {"name": c.name, "description": c.description}
                    for c in script_fragment.characters
                ],
            })

            # ---- Step 5: 更新记忆 ----
            if memory_update:
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
        yaml_path = novel_dir / f"{novel_name}.yaml"
        yaml_path.write_text(script_to_yaml(final_script), encoding="utf-8")
        self._progress("saving", 95)
        print(f"剧本已保存至: {novel_name}.yaml -> {yaml_path}")

        # 7. 生成 Schema 文档
        schema_dir = self.output_dir / "schema"
        schema_dir.mkdir(parents=True, exist_ok=True)
        generate_json_schema(schema_dir / "script_schema.json")
        generate_markdown_doc(schema_dir / "script_schema.md")
        print(f"Schema 文档已保存至: {schema_dir}")

        self._progress("done", 100)
        return final_script

    def _call_orchestrator(
        self,
        chunk: str,
        character_registry: dict[str, dict],
        start_scene_number: int,
    ) -> ChunkOutline:
        """调用编排 Agent 分析场景结构和段落分类。"""
        messages = build_orchestrator_prompt(
            novel_text=chunk,
            current_characters=character_registry,
            start_scene_number=start_scene_number,
        )

        raw = self.llm.chat(messages, max_tokens=2048)
        try:
            return parse_chunk_outline(raw)
        except ValueError as e:
            print(f"  编排 Agent 解析失败: {e}")
            return ChunkOutline(scenes=[])

    def _run_director_phase(
        self,
        reader: NovelReader,
        novel_dir: Path,
        character_registry: dict[str, dict],
        event_summaries: list[str],
    ):
        """Phase 1: 导演预读——并行遍历全文，提取角色和剧情信息。

        使用 director_chunk_size 分块，所有块并行调用 LLM，
        最后合并结果。不做剧本生成，只做全局分析。
        """
        dir_chunk_size = self.config.pipeline.director_chunk_size
        dir_overlap = self.config.pipeline.overlap_size
        dir_chunks = reader.chunks_with_overlap(dir_chunk_size, dir_overlap)
        total_dir_chunks = len(dir_chunks)

        self._progress("directing", 10)
        print(f"\n=== Phase 1: 导演预读（{total_dir_chunks} 块, chunk_size={dir_chunk_size}）===")

        if total_dir_chunks == 0:
            print("警告：小说内容过短，跳过导演预读")
            return

        # 并行调用导演 Agent 分析所有块
        def analyze_chunk(args):
            i, chunk = args
            chunk_idx = i + 1
            messages = build_director_prompt(
                novel_text=chunk,
                current_characters={},  # 并行模式下不传累积注册表，最后合并
                chunk_index=chunk_idx,
                total_chunks=total_dir_chunks,
            )
            raw_output = self.llm.chat(messages, max_tokens=2048)

            # 保存原始输出供调试
            debug_path = novel_dir / f"director_chunk_{chunk_idx}.txt"
            debug_path.write_text(raw_output, encoding="utf-8")

            try:
                analysis = parse_novel_analysis(raw_output)
                return chunk_idx, analysis
            except ValueError as e:
                print(f"  导演分析第 {chunk_idx} 块解析失败（跳过）: {e}")
                return chunk_idx, None

        with ThreadPoolExecutor(max_workers=min(total_dir_chunks, 4)) as executor:
            futures = {executor.submit(analyze_chunk, args): args[0] for args in enumerate(dir_chunks)}
            done_count = 0
            results = []
            for future in as_completed(futures):
                results.append(future.result())
                done_count += 1
                pct = 10 + int(25 * done_count / total_dir_chunks)
                self._progress("directing", pct)

        # 合并所有块的结果
        for chunk_idx, analysis in results:
            if analysis is None:
                continue

            for char in analysis.characters:
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

            if analysis.plot_events:
                event_summaries.append(f"[第{chunk_idx}段] {analysis.plot_events}")

            print(f"  第{chunk_idx}块: {len(analysis.characters)} 角色, "
                  f"剧情: {analysis.plot_events[:50] if analysis.plot_events else '无'}...")

        self._progress("directing", 35)
        print(f"导演预读完成: 共 {len(character_registry)} 个角色, "
              f"{len(event_summaries)} 段剧情摘要")


def run_pipeline(novel_path: str) -> Script:
    """快捷函数：加载配置并运行流程。"""
    config = load_config()
    pipeline = Pipeline(config)
    return pipeline.run(novel_path)
