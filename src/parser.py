"""YAML 解析与校验 —— 解析 LLM 返回的 YAML 并用 Pydantic 模型校验剧本结构。"""

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
