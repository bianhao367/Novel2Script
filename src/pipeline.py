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
# ThreadPoolExecutor: 线程池，用于并行调用 LLM（对白专家 + 动作专家同时工作）
# as_completed: 等待多个 Future 中任意一个完成（用于导演预读的进度汇报）
from pathlib import Path
from typing import Callable, Optional

from src.config import Config, load_config
from src.llm_client import LLMClient, LLMError
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
    build_memory_update_prompt,
    build_review_prompt, build_director_prompt, build_fix_prompt,
    build_orchestrator_prompt, build_dialogue_agent_prompt, build_action_agent_prompt,
)
from src.reader import NovelReader
from src.schema_gen import generate_json_schema, generate_markdown_doc
from src.logger import get_logger, setup_log_file


_MEMORY_CHAR_THRESHOLD = 40  # 角色数超过此值时压缩次要角色

logger = get_logger()


def _compress_registry(registry: dict[str, dict], compressed_chars: set[str]) -> None:  # registry: 角色注册表, compressed_chars: 已压缩过的角色名集合（避免重复截断）
    """压缩角色注册表：主要角色保留完整描述，次要角色截断（只截断一次）。

    压缩逻辑：
    1. 角色数 <= _MEMORY_CHAR_THRESHOLD → 不压缩，直接返回
    2. 遍历 registry，主角（is_main=True）保留完整 description，跳过
    3. 次要角色：检查 name 是否在 compressed_chars 中
       - 已在 → 跳过（之前已截断过）
       - 不在 → 截断 description 到 20 字符，加入 compressed_chars
    4. compressed_chars 集合跨调用共享，避免反复截断同一角色
    """
    if len(registry) <= _MEMORY_CHAR_THRESHOLD:
        return  # 角色数没超阈值，不需要压缩
    main_count = 0
    compressed = 0
    for name, info in registry.items():
        if info.get("is_main"):
            main_count += 1  # 主角不压缩
        elif name not in compressed_chars and len(info.get("description", "")) > 120:
            info["description"] = info["description"][:117] + "..."  # 截断到 120 字符
            compressed_chars.add(name)  # 标记为已压缩，下次跳过
            compressed += 1
    if compressed:
        logger.info(f"  角色注册表压缩: {main_count} 主角保留完整, {compressed} 次要角色截断描述")


class Pipeline:
    """编排小说到剧本的完整转换流程。

    这是整个项目的核心类，负责协调所有模块完成转换：
    reader（读取）→ prompt（构造）→ llm（调用）→ parser（解析）→ 输出

    Pipeline 的职责：
    - 编排执行顺序：导演预读 → 分块处理（编排→对白/动作并行→审查→记忆→预取下一编排）
    - 管理记忆状态：character_registry 和 event_summaries 跨块传递
    - 上层（server.py / main.py）只需要调用 pipeline.run() 就能完成全部流程
    """

    def __init__(
        self,
        config: Config,                                          # 全局配置对象
        progress_callback: Optional[Callable[[str, int], None]] = None,  # 进度回调 (step_name, percent)，前端进度条用
        chunk_result_callback: Optional[Callable[[dict], None]] = None,  # 每块中间结果回调，前端增量渲染用
        cancel_event: Optional[object] = None,                   # 取消事件（threading.Event），置位时中断流程
    ):
        self.config = config                                       # 全局配置
        self.llm = LLMClient(config)                               # LLM 客户端，所有大模型调用通过它
        self.output_dir = Path(config.pipeline.output_dir)         # 剧本输出目录（如 ./output/小说名/）
        self._progress = progress_callback or (lambda step, pct: None)  # 进度回调，通知前端当前进度
        self._chunk_result = chunk_result_callback or (lambda data: None)  # 块结果回调，前端增量渲染用
        self._cancel_event = cancel_event                          # 取消信号（threading.Event），置位时中断流程
        self._compressed_chars: set[str] = set()                   # 已压缩过的角色名集合，避免重复截断

    def run(self, novel_path: str | Path, novel_name: str | None = None) -> Script:  # novel_path: 小说文件路径, novel_name: 输出目录名（默认取文件名）
        """对一部小说执行完整流程：导演预读 → 滑动窗口分块 → 逐块生成 → 多轮审查 → 合并输出。

        整体流程（7 步）：
        1. 读取小说文件
        2. 初始化"记忆"状态（角色档案 + 事件摘要）
        3. Phase 1: 导演预读（全局分析角色和剧情）
        4. Phase 2: 逐块生成剧本（编排 → 并行专家 → 审查 → 记忆更新）
        5. 合并所有块的剧本
        6. 保存 YAML 文件
        7. 生成 Schema 文档
        """
        novel_path = Path(novel_path)
        if novel_name is None:
            novel_name = novel_path.stem  # 去掉扩展名，如 "遮天.txt" → "遮天"
        novel_dir = self.output_dir / novel_name
        novel_dir.mkdir(parents=True, exist_ok=True)
        setup_log_file(novel_dir)
        # ---- Step 1: 读取小说 ----
        reader = NovelReader(novel_path)
        self._progress("reading", 5)
        logger.info(f"已读取小说: {novel_path} ({reader.char_count} 字符)")

        # ---- Step 2: 初始化记忆状态 ----
        character_registry: dict[str, dict] = {}  # {角色名: {"description": "简介", "is_main": True/False}}
        event_summaries: list[str] = []  # ["[第1段] 叶凡拜入太玄门", "叶凡与强敌激战"]
        chunk_scripts: list[Script] = []  # [Script块1, Script块2, ...]，最后合并成一个完整 Script
        next_scene_number = 1  # 全局递增：块1用 1,2,3，块2用 4,5,6，保证场景号不重复

        # 3. Phase 1: 导演预读（全局分析）
        if self.config.pipeline.director_enabled:
            self._run_director_phase(reader, novel_dir, character_registry, event_summaries)

        # ---- Step 4: Phase 2 - 滑动窗口分块 + 逐块生成剧本 ----
        overlap = self.config.pipeline.overlap_size
        chunks = reader.chunks_with_overlap(self.config.pipeline.chunk_size, overlap)
        total_chunks = len(chunks)
        phase2_start_pct = 40 if self.config.pipeline.director_enabled else 10
        self._progress("chunking", phase2_start_pct)
        logger.info(f"小说共 {total_chunks} 块（overlap={overlap} 字符）")

        if total_chunks == 0:
            logger.warning("警告：小说内容为空，无法生成剧本")
            self._progress("done", 100)
            return Script()

        # 线程池：同时跑多个 LLM 请求
        chunk_executor = ThreadPoolExecutor(max_workers=3)

        def _submit_orchestrator(chunk_text: str, registry: dict, scene_num: int, summaries: list[str]):
            """把编排 Agent 提交到线程池执行。返回 Future 对象。"""
            deep_copy = {k: dict(v) for k, v in registry.items()}  # 复制一份角色注册表，防止线程间互相修改
            return chunk_executor.submit(
                self._call_orchestrator, chunk_text, deep_copy, scene_num, summaries,
            )

        # ---- 预取：在处理块1时，提前让 LLM 分析块2的结构 ----
        prefetch_future = _submit_orchestrator(chunks[0], character_registry, next_scene_number, event_summaries)

        # ---- 主循环：逐块处理 ----
        for i, chunk in enumerate(chunks):
            # 检查是否被取消
            if self._cancel_event and self._cancel_event.is_set():
                prefetch_future.cancel()
                logger.info(f"\n--- 转换已取消（第 {i+1}/{total_chunks} 块前）---")
                break
            chunk_idx = i + 1  #（从1开始）
            base_pct = phase2_start_pct + int(50 * i / total_chunks)
            self._progress("processing", base_pct)
            logger.info(f"\n--- 处理第 {chunk_idx}/{total_chunks} 块 ({len(chunk)} 字符) ---")

            # ---- Step 1: 取编排结果 + 预取下一块 ----
            self._progress("orchestrating", base_pct + 1)
            outline = prefetch_future.result()  # 阻塞，等编排 Agent 返回结果

            # 立即提交下一块的编排到线程池（与当前块的专家处理并行）
            if i + 1 < total_chunks:
                prefetch_future = _submit_orchestrator(
                    chunks[i + 1], character_registry, next_scene_number, event_summaries,
                )

            if not outline.scenes:
                logger.warning(f"  块 {chunk_idx} 编排结果为空，跳过")
                continue

            # 按段落序号切分原文，建立 {段落序号: 段落文本} 的映射
            paragraphs = chunk.split("\n\n")
            para_map: dict[int, str] = {}  # {0: "叶凡站在山巅...", 1: "你来了？他说...", 2: "..."}
            for idx, para in enumerate(paragraphs):
                cleaned = para.strip()
                if cleaned:
                    para_map[idx] = cleaned

            # 按编排结果把段落分成"对白段"和"动作段"两组
            # outline.scenes 里有每个段落的 content_type 标记
            dialogue_paras: list[tuple[int, str]] = []  # [(段落序号, 段落文本), ...]
            action_paras: list[tuple[int, str]] = []    # [(段落序号, 段落文本), ...]
            for scene in outline.scenes:
                for pa in scene.paragraphs:
                    text = para_map.get(pa.para_idx, "")  # 按序号取段落文本
                    if text:
                        if pa.content_type == "dialogue":
                            dialogue_paras.append((pa.para_idx, text))
                        else:
                            action_paras.append((pa.para_idx, text))

            logger.info(f"  编排: {len(outline.scenes)} 场, "
                  f"对白段 {len(dialogue_paras)}, 动作段 {len(action_paras)}")

            # ---- Step 2: 并行专家（对白 + 动作同时工作）----
            self._progress("calling_experts", base_pct + 3)

            # 构建场景摘要文本，供专家参考（如"- 第1场: 内. 古宅 - 夜"）
            scene_summary = "\n".join(
                f"- 第{s.scene_number}场: {s.slugline}"
                for s in outline.scenes
            )

            def call_dialogue():
                """对白专家：构造 prompt → 调用 LLM → 解析返回的 YAML"""
                if not dialogue_paras:
                    return DialogueScript(items=[])
                msgs = build_dialogue_agent_prompt(
                    chunk, character_registry, dialogue_paras, next_scene_number,
                    event_summaries=event_summaries,
                    scene_summary=scene_summary,
                )
                return parse_dialogue_script(self.llm.chat(msgs))

            def call_action():
                """动作专家：构造 prompt → 调用 LLM → 解析返回的 YAML"""
                if not action_paras:
                    return ActionScript(items=[])
                msgs = build_action_agent_prompt(
                    chunk, character_registry, action_paras, next_scene_number,
                    event_summaries=event_summaries,
                    scene_summary=scene_summary,
                )
                return parse_action_script(self.llm.chat(msgs))

            # 两个 submit 同时提交，线程池并行执行
            f_dialogue = chunk_executor.submit(call_dialogue)
            f_action = chunk_executor.submit(call_action)

            # 分别等待结果，失败时用空 Script 兜底
            try:
                dialogue_script = f_dialogue.result()
            except (ValueError, LLMError) as e:
                logger.warning(f"  块 {chunk_idx} 对白专家失败: {e}")
                dialogue_script = DialogueScript(items=[])

            try:
                action_script = f_action.result()
            except (ValueError, LLMError) as e:
                logger.warning(f"  块 {chunk_idx} 动作专家失败: {e}")
                action_script = ActionScript(items=[])

            logger.info(f"  专家结果: 对白 {len(dialogue_script.items)} 条, "
                  f"动作 {len(action_script.items)} 条")

            # ---- Step 3: 合并专家输出 ----
            # 把角色注册表转成 CharacterInfo 列表（Pydantic 模型）
            characters = [
                CharacterInfo(name=name, description=info["description"])
                for name, info in character_registry.items()
            ]
            # 合并：编排骨架 + 对白 + 动作 → 一个完整的 Script 片段
            script_fragment = merge_expert_outputs(
                outline, dialogue_script, action_script, characters,
            )

            # 序列化为 YAML 字符串（供审查 Agent 和记忆专家阅读）
            raw_output = script_to_yaml(script_fragment)
            # 同时保存到磁盘（调试用，出问题可以看中间结果）
            raw_path = novel_dir / f"raw_chunk_{chunk_idx}.txt"
            raw_path.write_text(raw_output, encoding="utf-8")

            # ---- Step 4: 多轮审查闭环 ----
            raw_output_before_review = raw_output  # 保存原始版本，用于回滚
            script_fragment_before_review = script_fragment
            max_rounds = self.config.pipeline.review_max_rounds

            for review_round in range(max_rounds):
                self._progress("reviewing", min(base_pct + 5 + review_round, 89))

                # 调用审查 Agent：把剧本 YAML + 原文 + 角色表 发给 LLM
                review_messages = build_review_prompt(
                    script_fragment_yaml=raw_output,        # 当前剧本
                    current_characters=character_registry,  # 角色表（检查角色名一致性）
                    start_scene_number=next_scene_number,   # 场景起始编号（检查编号连续性）
                    original_novel_text=chunk,              # 原文（检查是否忠实还原）
                )
                review_raw = self.llm.chat(review_messages, max_tokens=2048)

                # 解析审查结果：valid=True 表示通过，False 表示有问题
                try:
                    review_result = parse_review_result(review_raw)
                except ValueError as rev_err:
                    logger.warning(f"  块 {chunk_idx} 审查结果解析失败（跳过审查）: {rev_err}")
                    break

                if review_result.valid:
                    break  # 通过，退出审查循环

                logger.warning(f"  块 {chunk_idx} 审查第 {review_round + 1} 轮不通过: {review_result.issues}")

                if review_round == max_rounds - 1:
                    continue  # 最后一轮，不再修正（触发 for...else 回滚）

                # 调用修正 Agent：把剧本 + 审查问题 + 建议 + 原文 发给 LLM
                fix_messages = build_fix_prompt(
                    script_fragment_yaml=raw_output,           # 当前剧本
                    review_issues=review_result.issues,        # 审查发现的问题列表
                    review_suggestions=review_result.suggestions,  # 修正建议
                    original_novel_text=chunk,                 # 原文（供参考还原）
                    current_characters=character_registry,     # 角色表（供角色名一致性参考）
                )
                fix_raw = self.llm.chat(fix_messages, max_tokens=4096)

                # 解析修正后的剧本
                try:
                    script_fragment = parse_yaml(fix_raw)  # 修正后的 Script 对象
                    raw_output = fix_raw                   # 修正后的 YAML 字符串
                except ValueError as e:
                    logger.warning(f"  块 {chunk_idx} 修正后解析失败: {e}")
                    break
            else:
                # for...else：循环没被 break 时执行 → 所有轮次都失败了
                logger.warning(f"  块 {chunk_idx} 审查 {max_rounds} 轮均未通过，回滚到原始版本")
                raw_output = raw_output_before_review        # 用审查前的 YAML
                script_fragment = script_fragment_before_review  # 用审查前的 Script

            raw_path.write_text(raw_output, encoding="utf-8")

            # ---- Step 4.5: 记忆专家（用审查后的最终 YAML）----
            memory_update = None
            for mem_attempt in range(2):  # 最多重试 1 次
                try:
                    # 把审查通过的剧本 YAML + 当前角色表 发给记忆专家 LLM
                    memory_msgs = build_memory_update_prompt(raw_output, character_registry)
                    memory_update = parse_memory_update(self.llm.chat(memory_msgs, max_tokens=2048))
                    # memory_update = MemoryUpdate(
                    #     characters=[CharacterUpdate(name="姬紫月", description="...", is_main=False)],
                    #     event_summary="叶凡与强敌激战"
                    # )
                    break
                except (ValueError, LLMError) as e:
                    if mem_attempt == 0:
                        logger.warning(f"  块 {chunk_idx} 记忆专家失败，重试...")
                    else:
                        logger.warning(f"  块 {chunk_idx} 记忆专家重试也失败: {e}")

            # 下一块的场景编号 = 当前块最大场景号 + 1
            if script_fragment.scenes:
                next_scene_number = max(s.scene_number for s in script_fragment.scenes) + 1

            chunk_scripts.append(script_fragment)
            logger.info(f"  块 {chunk_idx} 剧本: {len(script_fragment.scenes)} 场, "
                  f"{len(script_fragment.characters)} 角色")

            # 把本块结果推送给前端（增量渲染）
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
                        # 新角色，直接加入
                        character_registry[char.name] = {
                            "description": char.description,
                            "is_main": char.is_main,
                        }
                    else:
                        # 已有角色，保留更长的 description
                        if len(char.description) > len(existing["description"]):
                            character_registry[char.name]["description"] = char.description
                        # 已有角色，一旦标记主角就永远是主角
                        if char.is_main:
                            character_registry[char.name]["is_main"] = True

                # 追加事件摘要
                if memory_update.event_summary:
                    event_summaries.append(memory_update.event_summary)

                # 压缩：超过 max_chars 时，保留最近的，丢弃最早的
                max_chars = self.config.pipeline.event_memory_max_chars
                compressed = compress_event_memory(event_summaries, max_chars)
                event_summaries.clear()          # 清空原始列表
                event_summaries.append(compressed)  # 替换为压缩后的单条文本

                logger.info(f"  记忆更新: {len(memory_update.characters)} 角色, "
                      f"事件: {memory_update.event_summary[:60]}...")

            _compress_registry(character_registry, self._compressed_chars)

        # 等所有线程池任务完成，然后关闭
        chunk_executor.shutdown(wait=True)

        # ---- Step 5: 合并所有块 ----
        self._progress("merging", 90)
        final_script = merge_scripts(chunk_scripts)
        # scenes 按编号排序拼接，characters 按名字去重
        logger.info(f"\n合并完成: {len(final_script.scenes)} 场, "
              f"{len(final_script.characters)} 角色")

        # ---- Step 6: 保存 YAML 文件 ----
        yaml_path = novel_dir / f"{novel_name}.yaml"
        yaml_path.write_text(script_to_yaml(final_script), encoding="utf-8")
        self._progress("saving", 95)
        logger.info(f"剧本已保存至: {novel_name}.yaml -> {yaml_path}")

        # ---- Step 7: 生成 Schema 文档 ----
        schema_dir = self.output_dir / "schema"
        schema_dir.mkdir(parents=True, exist_ok=True)
        generate_json_schema(schema_dir / "script_schema.json")
        generate_markdown_doc(schema_dir / "script_schema.md")
        logger.info(f"Schema 文档已保存至: {schema_dir}")

        self._progress("done", 100)
        return final_script

    def _call_orchestrator(
        self,
        chunk: str,                        # 当前块的小说文本
        character_registry: dict[str, dict],  # 角色注册表（深拷贝，无竞态）
        start_scene_number: int,           # 本块场景起始编号
        event_summaries: list[str] | None = None,  # 已发生的事件摘要
    ) -> ChunkOutline:
        """调用编排 Agent 分析场景结构和段落分类。

        编排 Agent 的职责：
        - 分析小说文本的结构，划分场景（场景切换 = 地点变化/时间跳跃）
        - 给每个段落标记类型：dialogue（对话）或 action（描写）
        - 产出 ChunkOutline（场景大纲），供后续对白/动作专家使用

        失败重试：最多 2 次，因为编排是后续所有步骤的基础
        """
        for attempt in range(2):
            messages = build_orchestrator_prompt(
                novel_text=chunk,
                current_characters=character_registry,
                start_scene_number=start_scene_number,
                event_summaries=event_summaries,
            )
            raw = self.llm.chat(messages, max_tokens=2048)
            try:
                result = parse_chunk_outline(raw)
                if result.scenes:  # 确保解析出了至少一个场景
                    return result
            except ValueError:
                pass  # 解析失败，重试
            logger.warning(f"  编排 Agent 第 {attempt + 1} 次失败，重试...")
        # 两次都失败，返回空大纲（这块会被跳过）
        return ChunkOutline(scenes=[])

    def _run_director_phase(
        self,
        reader: NovelReader,             # 小说读取器实例
        novel_dir: Path,                 # 输出目录（保存调试文件）
        character_registry: dict[str, dict],  # 角色注册表（就地更新）
        event_summaries: list[str],      # 事件摘要列表（就地追加）
    ):
        """Phase 1: 导演预读——并行遍历全文，提取角色和剧情信息。"""
        dir_chunk_size = self.config.pipeline.director_chunk_size  # 默认 15000
        dir_overlap = self.config.pipeline.overlap_size
        # 用更大的块（15000字）粗读，比正常块（3000字）大 5 倍
        dir_chunks = reader.chunks_with_overlap(dir_chunk_size, dir_overlap)
        total_dir_chunks = len(dir_chunks)

        self._progress("directing", 10)
        logger.info(f"\n=== Phase 1: 导演预读（{total_dir_chunks} 块, chunk_size={dir_chunk_size}）===")

        if total_dir_chunks == 0:
            logger.warning("警告：小说内容过短，跳过导演预读")
            return

        def analyze_chunk(args):
            """分析单个导演块：构造 prompt → 调 LLM → 解析返回的角色和剧情"""
            i, chunk = args
            chunk_idx = i + 1
            messages = build_director_prompt(
                novel_text=chunk,
                current_characters={},  # 并行时不传累积注册表，最后统一合并
                chunk_index=chunk_idx,
                total_chunks=total_dir_chunks,
            )
            raw_output = self.llm.chat(messages, max_tokens=2048)

            # 保存 LLM 原始返回（调试用）
            debug_path = novel_dir / f"director_chunk_{chunk_idx}.txt"
            debug_path.write_text(raw_output, encoding="utf-8")

            try:
                analysis = parse_novel_analysis(raw_output)
                # analysis = NovelAnalysis(characters=[...], plot_events="...")
                return chunk_idx, analysis
            except ValueError as e:
                logger.warning(f"  导演分析第 {chunk_idx} 块解析失败（跳过）: {e}")
                return chunk_idx, None

        # 并行执行：所有导演块同时调 LLM
        with ThreadPoolExecutor(max_workers=min(total_dir_chunks, 4)) as executor:
            futures = {executor.submit(analyze_chunk, args): args[0] for args in enumerate(dir_chunks)}
            done_count = 0
            results = []
            for future in as_completed(futures):
                results.append(future.result())
                done_count += 1
                pct = 10 + int(25 * done_count / total_dir_chunks)
                self._progress("directing", pct)
        results.sort(key=lambda x: x[0])  # 按块序号排序，保持叙事顺序

        # 合并所有块的结果到 character_registry 和 event_summaries
        for chunk_idx, analysis in results:
            if analysis is None:
                continue

            # 合并角色：新角色加入，已有角色保留更长的 description
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

            # 追加剧情摘要
            if analysis.plot_events:
                event_summaries.append(f"[第{chunk_idx}段] {analysis.plot_events}")

            logger.info(f"  第{chunk_idx}块: {len(analysis.characters)} 角色, "
                  f"剧情: {analysis.plot_events[:50] if analysis.plot_events else '无'}...")

        self._progress("directing", 35)
        logger.info(f"导演预读完成: 共 {len(character_registry)} 个角色, "
              f"{len(event_summaries)} 段剧情摘要")


def run_pipeline(novel_path: str) -> Script:  # novel_path: 小说 .txt 文件路径
    """快捷函数：加载配置并运行流程。"""
    config = load_config()
    pipeline = Pipeline(config)
    return pipeline.run(novel_path)
