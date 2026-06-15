"""
YAML 解析与校验
===============
解析 LLM 返回的 YAML 文本，并用 Pydantic v2 模型校验剧本结构。

剧本结构（Script）包含：
- scenes: 场景列表，每场包含 content（对白/动作交替）
- characters: 角色信息列表
- title: 剧本标题

使用 Pydantic v2 的 Discriminated Union 特性，content 列表中的每个元素
根据 type 字段（"dialogue" / "action"）自动路由到对应模型。

使用方式：
    script = parse_yaml(llm_output)    # 解析并校验
    yaml_str = script_to_yaml(script)   # 序列化回 YAML
"""

import yaml

from typing import Literal, Annotated, Union
from pydantic import BaseModel, Field, ValidationError, Discriminator


# ============================================================
# Pydantic 模型：定义剧本的数据结构
# ============================================================
# 这些模型定义了剧本的"数据合同"：
# - LLM 返回的 YAML 文本先用 yaml.safe_load() 解析为 Python dict
# - 再用这些 Pydantic 模型校验：字段类型是否正确、必填字段是否缺失
# - 不合法时抛出 ValidationError，pipeline 会重试 LLM 调用
# ============================================================

class DialogueItem(BaseModel):
    """场景中的一句对白"""
    type: Literal["dialogue"]      # 固定值 "dialogue"，用于 Discriminated Union 路由
    character: str                 # 说话角色名，必须与角色注册表一致
    line: str                      # 台词正文
    parenthetical: str = ""        # 情绪/动作提示（如"愤怒地"），可选


class ActionItem(BaseModel):
    """场景中的一条动作/舞台指示"""
    type: Literal["action"]        # 固定值 "action"，用于 Discriminated Union 路由
    text: str                      # 动作描述（如"叶凡站起身来，望向远方"）


# --- Discriminated Union（判别联合类型）---
# Pydantic v2 的特性：根据 type 字段的值自动路由到对应模型
# 解析时：type="dialogue" → DialogueItem, type="action" → ActionItem
# 不需要手写 if/else，Pydantic 根据 type 字段自动选择
ContentItem = Annotated[
    Union[DialogueItem, ActionItem],  # 两种类型二选一
    Discriminator("type"),            # 用 "type" 字段来区分选哪个
]


class Scene(BaseModel):
    """单场戏"""
    scene_number: int
    slugline: str = ""                         # 场标（如"内. 古宅 - 夜"）
    content: list[ContentItem] = Field(default_factory=list)


class CharacterInfo(BaseModel):
    """角色信息"""
    name: str                      # 角色名
    description: str = ""          # 角色简介


class Script(BaseModel):
    """完整剧本"""
    title: str = "Untitled"        # 剧本标题
    scenes: list[Scene] = Field(default_factory=list)  # 场景列表
    characters: list[CharacterInfo] = Field(default_factory=list)  # 角色列表


# 导出供 schema_gen 使用
SCRIPT_SCHEMA = Script


# --- 多块记忆相关模型 ---

class CharacterUpdate(BaseModel):
    """单个角色的更新信息（来自记忆提取 LLM 调用）"""
    name: str                      # 角色名
    description: str = ""          # 角色简介（有新信息时更新）
    is_main: bool = False          # 是否为主要角色


class MemoryUpdate(BaseModel):
    """记忆更新 LLM 调用的输出结构"""
    characters: list[CharacterUpdate] = Field(default_factory=list)  # 角色更新列表
    event_summary: str = ""        # 本片段事件摘要（2-3句话）


class ReviewResult(BaseModel):
    """审查 Agent 的输出结构"""
    valid: bool = True             # 是否通过审查
    issues: list[str] = Field(default_factory=list)  # 发现的问题列表
    suggestions: str = ""          # 修正建议


class NovelAnalysis(BaseModel):
    """导演 Agent 的输出结构：小说片段分析"""
    characters: list[CharacterUpdate] = Field(default_factory=list)  # 提取的角色列表
    plot_events: str = ""          # 本片段关键剧情事件（1-3句话）


# --- 多 Agent 并行专家模型 ---

class ParagraphAssignment(BaseModel):
    """编排 Agent 输出的段落分类"""
    para_idx: int                                  # 段落在原文中的序号（从0开始）
    content_type: Literal["dialogue", "action"]    # 段落类型

class SceneOutline(BaseModel):
    """编排 Agent 输出的单个场景大纲"""
    scene_number: int              # 场景编号
    slugline: str = ""             # 场标（如"内. 古宅 - 夜"）
    paragraphs: list[ParagraphAssignment] = Field(default_factory=list)  # 段落分类列表

class ChunkOutline(BaseModel):
    """编排 Agent 对一个块的完整分析"""
    scenes: list[SceneOutline] = Field(default_factory=list)

class DialogueOutputItem(BaseModel):
    """对白专家的单条输出"""
    scene_number: int              # 所属场景编号
    para_idx: int                  # 段落序号
    character: str                 # 说话角色名
    line: str                      # 台词正文
    parenthetical: str = ""        # 情绪/动作提示

class ActionOutputItem(BaseModel):
    """动作专家的单条输出"""
    scene_number: int              # 所属场景编号
    para_idx: int                  # 段落序号
    text: str                      # 动作/舞台指示描述

class DialogueScript(BaseModel):
    """对白专家的完整输出"""
    items: list[DialogueOutputItem] = Field(default_factory=list)  # 对白条目列表

class ActionScript(BaseModel):
    """动作专家的完整输出"""
    items: list[ActionOutputItem] = Field(default_factory=list)  # 动作条目列表


def _strip_code_fences(text: str) -> str:
    """去除 LLM 输出中可能包含的 markdown 代码块标记。

    处理两种情况：
    1. 整个文本就是 ```yaml...``` — 直接剥掉
    2. LLM 在代码块前写了说明文字（如"好的，以下是YAML："）— 找到 ``` 并提取中间内容
    """
    cleaned = text.strip()

    # 情况1：文本以 ``` 开头
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        if lines and lines[0].startswith("```"):
            lines = lines[1:]  # 去掉第一行（```yaml 或 ```）
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]  # 去掉最后一行（```）
        return "\n".join(lines).strip()

    # 情况2：``` 不在开头，查找文本中间的代码块
    import re
    match = re.search(r'```(?:yaml|yml)?\s*\n(.*?)\n```', cleaned, re.DOTALL)
    if match:
        return match.group(1).strip()

    return cleaned


def parse_yaml(yaml_text: str) -> Script:  # yaml_text: LLM 返回的 YAML 文本
    """解析 YAML 文本并校验是否符合 Script 模型。

    处理流程：
    1. 调用 _strip_code_fences() 去除 ``` 标记
    2. yaml.safe_load() 将 YAML 文本解析为 Python dict
    3. Script(**raw) 用 Pydantic 校验字段类型和必填项
    4. 校验失败抛出 ValueError，pipeline 据此重试

    Raises:
        ValueError: YAML 格式非法或数据不符合模型约束。
    """
    cleaned = _strip_code_fences(yaml_text)

    try:
        raw = yaml.safe_load(cleaned)
    except yaml.YAMLError as e:
        raise ValueError(f"YAML 格式错误: {e}") from e

    if raw is None:
        raise ValueError("解析后的 YAML 为空")

    try:
        script = Script(**raw)
    except ValidationError as e:
        raise ValueError(f"Schema 校验失败:\n{e}") from e

    return script


def script_to_yaml(script: Script) -> str:  # script: 已校验的 Script 对象
    """将 Script 对象序列化为格式化的 YAML 字符串。

    调用 Pydantic 的 model_dump() 将模型转为 dict，
    再用 yaml.dump() 序列化为 YAML（allow_unicode 保留中文）。
    """
    return yaml.dump(
        script.model_dump(),
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
    )


def parse_memory_update(yaml_text: str) -> MemoryUpdate:  # yaml_text: 记忆更新 LLM 输出的 YAML
    """解析记忆更新 LLM 输出的 YAML。

    流程：_strip_code_fences → yaml.safe_load → MemoryUpdate(**raw)
    返回包含 characters（角色更新）和 event_summary（事件摘要）的 MemoryUpdate 对象。
    """
    cleaned = _strip_code_fences(yaml_text)

    try:
        raw = yaml.safe_load(cleaned)
    except yaml.YAMLError as e:
        raise ValueError(f"记忆更新 YAML 格式错误: {e}") from e

    if raw is None:
        raise ValueError("记忆更新 YAML 为空")

    try:
        return MemoryUpdate(**raw)
    except ValidationError as e:
        raise ValueError(f"记忆更新 Schema 校验失败:\n{e}") from e


def parse_review_result(yaml_text: str) -> ReviewResult:  # yaml_text: 审查 Agent 输出的 YAML
    """解析审查 Agent 输出的 YAML。

    流程：_strip_code_fences → yaml.safe_load → ReviewResult(**raw)
    返回包含 valid（是否通过）、issues（问题列表）、suggestions（修正建议）的 ReviewResult 对象。
    """
    cleaned = _strip_code_fences(yaml_text)

    try:
        raw = yaml.safe_load(cleaned)
    except yaml.YAMLError as e:
        raise ValueError(f"审查结果 YAML 格式错误: {e}") from e

    if raw is None:
        raise ValueError("审查结果 YAML 为空")

    try:
        return ReviewResult(**raw)
    except ValidationError as e:
        raise ValueError(f"审查结果 Schema 校验失败:\n{e}") from e


def parse_novel_analysis(yaml_text: str) -> NovelAnalysis:  # yaml_text: 导演 Agent 输出的 YAML
    """解析导演 Agent 输出的 YAML。

    流程：_strip_code_fences → yaml.safe_load → NovelAnalysis(**raw)
    返回包含 characters（角色列表）和 plot_events（剧情事件）的 NovelAnalysis 对象。
    """
    cleaned = _strip_code_fences(yaml_text)

    try:
        raw = yaml.safe_load(cleaned)
    except yaml.YAMLError as e:
        raise ValueError(f"导演分析 YAML 格式错误: {e}") from e

    if raw is None:
        raise ValueError("导演分析 YAML 为空")

    try:
        return NovelAnalysis(**raw)
    except ValidationError as e:
        raise ValueError(f"导演分析 Schema 校验失败:\n{e}") from e


def parse_chunk_outline(yaml_text: str) -> ChunkOutline:  # yaml_text: 编排 Agent 输出的 YAML
    """解析编排 Agent 输出的 YAML。

    流程：_strip_code_fences → yaml.safe_load → ChunkOutline(**raw)
    返回包含 scenes（场景大纲列表，每个场景含段落分类）的 ChunkOutline 对象。
    """
    cleaned = _strip_code_fences(yaml_text)

    try:
        raw = yaml.safe_load(cleaned)
    except yaml.YAMLError as e:
        raise ValueError(f"编排分析 YAML 格式错误: {e}") from e

    if raw is None:
        raise ValueError("编排分析 YAML 为空")

    try:
        return ChunkOutline(**raw)
    except ValidationError as e:
        raise ValueError(f"编排分析 Schema 校验失败:\n{e}") from e


def parse_dialogue_script(yaml_text: str) -> DialogueScript:  # yaml_text: 对白专家输出的 YAML
    """解析对白专家输出的 YAML。

    流程：_strip_code_fences → yaml.safe_load → DialogueScript(**raw)
    返回包含 items（对白条目列表，每条含 scene_number, para_idx, character, line）的 DialogueScript 对象。
    """
    cleaned = _strip_code_fences(yaml_text)

    try:
        raw = yaml.safe_load(cleaned)
    except yaml.YAMLError as e:
        raise ValueError(f"对白 YAML 格式错误: {e}") from e

    if raw is None:
        raise ValueError("对白 YAML 为空")

    try:
        return DialogueScript(**raw)
    except ValidationError as e:
        raise ValueError(f"对白 Schema 校验失败:\n{e}") from e


def parse_action_script(yaml_text: str) -> ActionScript:  # yaml_text: 动作专家输出的 YAML
    """解析动作专家输出的 YAML。

    流程：_strip_code_fences → yaml.safe_load → ActionScript(**raw)
    返回包含 items（动作条目列表，每条含 scene_number, para_idx, text）的 ActionScript 对象。
    """
    cleaned = _strip_code_fences(yaml_text)

    try:
        raw = yaml.safe_load(cleaned)
    except yaml.YAMLError as e:
        raise ValueError(f"动作 YAML 格式错误: {e}") from e

    if raw is None:
        raise ValueError("动作 YAML 为空")

    try:
        return ActionScript(**raw)
    except ValidationError as e:
        raise ValueError(f"动作 Schema 校验失败:\n{e}") from e


def merge_expert_outputs(
    outline: ChunkOutline,           # 编排 Agent 的场景大纲
    dialogues: DialogueScript,       # 对白专家的输出
    actions: ActionScript,           # 动作专家的输出
    characters: list[CharacterInfo], # 角色信息列表
) -> Script:
    """将编排、对白、动作三个专家的输出合并为完整 Script。

    合并步骤：
    1. 用编排结果建立场景骨架：遍历 outline.scenes，创建 Scene 对象（scene_number -> Scene）
    2. 收集所有 content items，带排序优先级：对白 priority=0（在前），动作 priority=1（在后）
    3. 按 (scene_number, para_idx, priority) 排序，保证同一段落对白在动作前面
    4. 遍历排序结果，将每个 item 塞入对应 scene_map[scene_number].content
    5. 按 scene_number 排序所有场景，组装最终 Script
    """
    # Step 1: 建立 scene_number -> Scene 的映射（场景骨架）
    scene_map: dict[int, Scene] = {}
    for so in outline.scenes:
        scene_map[so.scene_number] = Scene(
            scene_number=so.scene_number,
            slugline=so.slugline,
            content=[],  # 先创建空场景，后面再填内容
        )

    # Step 2: 收集所有 content items，带排序优先级
    # 元组格式：(scene_number, para_idx, priority, content_item)
    # priority: dialogue=0 (在前), action=1 (在后)
    # 这样排序后同一段落的对白总在动作前面
    entries: list[tuple[int, int, int, ContentItem]] = []

    for item in dialogues.items:
        entries.append((
            item.scene_number,
            item.para_idx,
            0,  # 对白优先级=0
            DialogueItem(type="dialogue", character=item.character, line=item.line, parenthetical=item.parenthetical),
        ))

    for item in actions.items:
        entries.append((
            item.scene_number,
            item.para_idx,
            1,  # 动作优先级=1
            ActionItem(type="action", text=item.text),
        ))

    # Step 3: 按 (场景号, 段落号, 优先级) 排序
    entries.sort(key=lambda e: (e[0], e[1], e[2]))

    # Step 4: 把排好序的 items 分配到各场景
    for scene_num, _, _, content_item in entries:
        if scene_num in scene_map:
            scene_map[scene_num].content.append(content_item)

    # Step 5: 按场景号排序，组装最终 Script
    scenes = sorted(scene_map.values(), key=lambda s: s.scene_number)
    return Script(scenes=scenes, characters=characters)


def merge_scripts(scripts: list[Script]) -> Script:  # scripts: 多个分块生成的 Script 列表
    """将多个分块生成的 Script 合并为一个完整的 Script。

    合并步骤：
    1. 遍历所有 scripts，收集全部 scenes 和 characters
    2. scenes: 直接拼接，最后按 scene_number 排序
    3. characters: 同名角色去重，用 all_characters[name] dict 做键值存储
       - 遍历时比较 description 长度，保留更完整的那条
    4. title: 取第一个非空标题（通常只有第一块有标题）
    5. 返回合并后的 Script 对象
    """
    if not scripts:
        return Script()

    all_scenes: list[Scene] = []
    all_characters: dict[str, CharacterInfo] = {}  # name -> CharacterInfo，用于去重
    title = ""

    for script in scripts:
        # 取第一个非空标题
        if not title and script.title and script.title != "Untitled":
            title = script.title

        # 场景直接拼接
        all_scenes.extend(script.scenes)

        # 角色去重：保留 description 更长的（信息更完整的）
        for char in script.characters:
            existing = all_characters.get(char.name)
            if existing is None:
                all_characters[char.name] = char  # 新角色，直接加入
            elif len(char.description) > len(existing.description):
                all_characters[char.name] = char  # 同名角色，用描述更长的

    all_scenes.sort(key=lambda s: s.scene_number)  # 按场景号排序

    return Script(
        title=title or "Untitled",
        scenes=all_scenes,
        characters=list(all_characters.values()),
    )


def compress_event_memory(event_summaries: list[str], max_chars: int) -> str:  # event_summaries: 事件摘要列表, max_chars: 最大字符数限制
    """当事件记忆超过 max_chars 时，压缩保留最近的摘要，丢弃最早的。

    压缩算法（滑动窗口，从新到旧）：
    1. 将所有摘要 join 为一个字符串，检查总长度是否超过 max_chars
    2. 没超限 → 直接返回，不做任何处理
    3. 超限 → 从最新一条开始往回遍历，逐条累加字符数
    4. 某条加入后总长度超过 budget → 停止，丢弃更早的摘要
    5. 如果丢弃了任何摘要，在结果前面加 "[早期情节已省略]\n" 标记
    6. 特殊处理：max_chars <= 20 时直接截断最新一条（极小预算防护）
    """
    if not event_summaries:
        return ""

    full_text = "\n".join(event_summaries)
    if len(full_text) <= max_chars:
        return full_text  # 没超限，不需要压缩

    # 极小预算防护：max_chars 太小时，直接截断最近一条
    if max_chars <= 20:
        return event_summaries[-1][:max_chars]

    # 从最新往回累加，超限时停止
    prefix = "[早期情节已省略]\n"  # 告诉 LLM 前面有省略
    budget = max_chars - len(prefix)  # 去掉前缀后的可用空间
    kept: list[str] = []  # 保留的摘要（从新到旧收集，最后反转）
    total = 0

    for summary in reversed(event_summaries):  # 从最新开始往回遍历
        if total + len(summary) + 1 > budget:  # +1 是换行符
            if not kept:
                # 最新一条本身就超限，截断保留（至少留点内容）
                kept.insert(0, summary[:budget])
            break
        kept.insert(0, summary)  # insert(0, ...) 保持原始顺序
        total += len(summary) + 1

    # 如果丢弃了任何摘要，加上省略标记
    if len(kept) < len(event_summaries):
        return prefix + "\n".join(kept)
    return "\n".join(kept)
