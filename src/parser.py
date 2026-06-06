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


# --- Pydantic 模型定义剧本结构 ---

class DialogueItem(BaseModel):
    """场景中的一句对白"""
    type: Literal["dialogue"]
    character: str                             # 说话角色名
    line: str                                  # 台词正文
    parenthetical: str = ""                    # 情绪/动作提示（括号注）


class ActionItem(BaseModel):
    """场景中的一条动作/舞台指示"""
    type: Literal["action"]
    text: str                                  # 动作描述


ContentItem = Annotated[
    Union[DialogueItem, ActionItem],
    Discriminator("type"),
]


class Scene(BaseModel):
    """单场戏"""
    scene_number: int
    slugline: str = ""                         # 场标（如"内. 古宅 - 夜"）
    content: list[ContentItem] = Field(default_factory=list)


class CharacterInfo(BaseModel):
    """角色信息"""
    name: str
    description: str = ""


class Script(BaseModel):
    """完整剧本"""
    title: str = "Untitled"
    scenes: list[Scene] = Field(default_factory=list)
    characters: list[CharacterInfo] = Field(default_factory=list)


# 导出供 schema_gen 使用
SCRIPT_SCHEMA = Script


# --- 多块记忆相关模型 ---

class CharacterUpdate(BaseModel):
    """单个角色的更新信息（来自记忆提取 LLM 调用）"""
    name: str
    description: str = ""
    is_main: bool = False


class MemoryUpdate(BaseModel):
    """记忆更新 LLM 调用的输出结构"""
    characters: list[CharacterUpdate] = Field(default_factory=list)
    event_summary: str = ""


class ReviewResult(BaseModel):
    """审查 Agent 的输出结构"""
    valid: bool = True
    issues: list[str] = Field(default_factory=list)
    suggestions: str = ""


class NovelAnalysis(BaseModel):
    """导演 Agent 的输出结构：小说片段分析"""
    characters: list[CharacterUpdate] = Field(default_factory=list)
    plot_events: str = ""


# --- 多 Agent 并行专家模型 ---

class ParagraphAssignment(BaseModel):
    """编排 Agent 输出的段落分类"""
    para_idx: int                                  # 段落在原文中的序号（从0开始）
    content_type: Literal["dialogue", "action"]    # 段落类型

class SceneOutline(BaseModel):
    """编排 Agent 输出的单个场景大纲"""
    scene_number: int
    slugline: str = ""
    paragraphs: list[ParagraphAssignment] = Field(default_factory=list)

class ChunkOutline(BaseModel):
    """编排 Agent 对一个块的完整分析"""
    scenes: list[SceneOutline] = Field(default_factory=list)

class DialogueOutputItem(BaseModel):
    """对白专家的单条输出"""
    scene_number: int
    para_idx: int
    character: str
    line: str
    parenthetical: str = ""

class ActionOutputItem(BaseModel):
    """动作专家的单条输出"""
    scene_number: int
    para_idx: int
    text: str

class DialogueScript(BaseModel):
    """对白专家的完整输出"""
    items: list[DialogueOutputItem] = Field(default_factory=list)

class ActionScript(BaseModel):
    """动作专家的完整输出"""
    items: list[ActionOutputItem] = Field(default_factory=list)


def _strip_code_fences(text: str) -> str:
    """去除 LLM 输出中可能包含的 markdown 代码块标记。"""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    return cleaned


def parse_yaml(yaml_text: str) -> Script:
    """解析 YAML 文本并校验是否符合 Script 模型。

    如果 LLM 输出包含了 markdown 代码块标记（```），会自动去除。

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


def script_to_yaml(script: Script) -> str:
    """将校验后的 Script 对象序列化为格式化的 YAML 字符串。"""
    return yaml.dump(
        script.model_dump(),
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
    )


def parse_memory_update(yaml_text: str) -> MemoryUpdate:
    """解析记忆更新 LLM 输出的 YAML。"""
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


def parse_review_result(yaml_text: str) -> ReviewResult:
    """解析审查 Agent 输出的 YAML。"""
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


def parse_novel_analysis(yaml_text: str) -> NovelAnalysis:
    """解析导演 Agent 输出的 YAML。"""
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


def parse_chunk_outline(yaml_text: str) -> ChunkOutline:
    """解析编排 Agent 输出的 YAML。"""
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


def parse_dialogue_script(yaml_text: str) -> DialogueScript:
    """解析对白专家输出的 YAML。"""
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


def parse_action_script(yaml_text: str) -> ActionScript:
    """解析动作专家输出的 YAML。"""
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
    outline: ChunkOutline,
    dialogues: DialogueScript,
    actions: ActionScript,
    characters: list[CharacterInfo],
) -> Script:
    """将编排、对白、动作专家的输出合并为完整 Script。

    - 按 scene_number 分组
    - 每场景内将 dialogue 和 action items 按 para_idx 排序
    - 相同 para_idx 时 dialogue 在前
    """
    # 建立 scene_number -> Scene 的映射
    scene_map: dict[int, Scene] = {}
    for so in outline.scenes:
        scene_map[so.scene_number] = Scene(
            scene_number=so.scene_number,
            slugline=so.slugline,
            content=[],
        )

    # 收集所有 content items 并按 (scene_number, para_idx, priority) 排序
    # priority: dialogue=0 (在前), action=1 (在后)
    entries: list[tuple[int, int, int, ContentItem]] = []

    for item in dialogues.items:
        entries.append((
            item.scene_number,
            item.para_idx,
            0,
            DialogueItem(type="dialogue", character=item.character, line=item.line, parenthetical=item.parenthetical),
        ))

    for item in actions.items:
        entries.append((
            item.scene_number,
            item.para_idx,
            1,
            ActionItem(type="action", text=item.text),
        ))

    entries.sort(key=lambda e: (e[0], e[1], e[2]))

    # 分配到各场景
    for scene_num, _, _, content_item in entries:
        if scene_num in scene_map:
            scene_map[scene_num].content.append(content_item)

    scenes = sorted(scene_map.values(), key=lambda s: s.scene_number)
    return Script(scenes=scenes, characters=characters)


def merge_scripts(scripts: list[Script]) -> Script:
    """将多个分块生成的 Script 合并为一个完整的 Script。

    - scenes: 按 scene_number 排序拼接
    - characters: 按 name 去重，保留更长的 description
    - title: 使用第一个非空标题
    """
    if not scripts:
        return Script()

    all_scenes: list[Scene] = []
    all_characters: dict[str, CharacterInfo] = {}
    title = ""

    for script in scripts:
        if not title and script.title and script.title != "Untitled":
            title = script.title

        all_scenes.extend(script.scenes)

        for char in script.characters:
            existing = all_characters.get(char.name)
            if existing is None:
                all_characters[char.name] = char
            elif len(char.description) > len(existing.description):
                all_characters[char.name] = char

    all_scenes.sort(key=lambda s: s.scene_number)

    return Script(
        title=title or "Untitled",
        scenes=all_scenes,
        characters=list(all_characters.values()),
    )


def compress_event_memory(event_summaries: list[str], max_chars: int) -> str:
    """当事件记忆超过 max_chars 时，保留最近的摘要，丢弃最早的。

    策略：从最新往回累加，超限时截断并加省略标注。
    如果最新的一条摘要本身就超限，则截断该条至 max_chars。
    """
    if not event_summaries:
        return ""

    full_text = "\n".join(event_summaries)
    if len(full_text) <= max_chars:
        return full_text

    # 极小预算防护：至少保留最近一条摘要的前 max_chars 个字符
    if max_chars <= 20:
        return event_summaries[-1][:max_chars]

    prefix = "[早期情节已省略]\n"
    budget = max_chars - len(prefix)
    kept: list[str] = []
    total = 0

    for summary in reversed(event_summaries):
        if total + len(summary) + 1 > budget:
            if not kept:
                # 最新一条本身就超限，截断保留
                kept.insert(0, summary[:budget])
            break
        kept.insert(0, summary)
        total += len(summary) + 1

    if len(kept) < len(event_summaries):
        return prefix + "\n".join(kept)
    return "\n".join(kept)
