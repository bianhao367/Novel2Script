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


def parse_yaml(yaml_text: str) -> Script:
    """解析 YAML 文本并校验是否符合 Script 模型。

    如果 LLM 输出包含了 markdown 代码块标记（```），会自动去除。

    Raises:
        ValueError: YAML 格式非法或数据不符合模型约束。
    """
    cleaned = yaml_text.strip()

    # 去掉可能的 markdown 代码块标记
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()

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
    """解析记忆更新 LLM 输出的 YAML。

    与 parse_yaml 类似的清理逻辑：去除 markdown 代码块标记。
    """
    cleaned = yaml_text.strip()

    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()

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
    """
    full_text = "\n".join(event_summaries)
    if len(full_text) <= max_chars:
        return full_text

    prefix = "[早期情节已省略]\n"
    budget = max_chars - len(prefix)
    kept: list[str] = []
    total = 0

    for summary in reversed(event_summaries):
        if total + len(summary) + 1 > budget:
            break
        kept.insert(0, summary)
        total += len(summary) + 1

    if len(kept) < len(event_summaries):
        return prefix + "\n".join(kept)
    return "\n".join(kept)
