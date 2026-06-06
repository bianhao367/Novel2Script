"""
Prompt 构造器
=============
为小说转剧本任务构建 LLM 对话 prompt。

SYSTEM_PROMPT 定义了 LLM 的角色（专业编剧）和输出格式（严格 YAML），
包括详细的字段说明和核心规则。build_prompt() 将系统提示与小说文本
组装为 OpenAI chat completion 格式的消息列表。

使用方式：
    messages = build_prompt(novel_text)
    response = llm.chat(messages)
"""

SYSTEM_PROMPT = """\
你是一名专业编剧。你的任务是将小说片段转换为结构化的 YAML 格式剧本。

请只输出合法的 YAML —— 不要用 markdown 代码块包裹，不要添加任何额外的说明文字。\
YAML 必须严格符合以下结构:

```yaml
title: "剧本标题"
scenes:
  - scene_number: 1
    slugline: "内/外. 地点 - 时间（如：内. 古宅门厅 - 夜）"
    content:
      - type: "action"
        text: "动作或舞台指示的描述文字"
      - type: "dialogue"
        character: "角色名"
        parenthetical: "情绪或动作提示（可选，无则留空）"
        line: "台词正文"
characters:
  - name: "角色名"
    description: "角色简介"
```

核心规则:
- content 列表中的项目按顺序排列，这个顺序就是剧本的演出顺序，必须忠实还原小说中动作与对话的交错节奏。
- 每个 content 项只能是 action 或 dialogue 类型，二者交替出现以呈现"描述→对白→描述→对白"的自然叙事流。
- 根据场景切换（地点变化）或时间跳跃将叙事拆分为不同的场次，每场以 slugline 标明地点和时间。
- slugline 的格式: "内/外. 具体地点 - 时间"，如 "内. 侦探办公室 - 黄昏"。
- 保留原著故事的主线、关键对话和情感基调。
- 对于小说中模糊不清的地方，按照影视剧本惯例进行合理推断。
- 所有有台词的角色必须出现在顶层 characters 列表中。
"""


def build_prompt(novel_text: str) -> list[dict]:
    """构建 chat-completion 格式的消息列表。

    返回可直接传给 OpenAI API 的 messages 列表。
    """
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                "请将以下小说片段转换为剧本，按照上述 YAML 格式输出：\n\n" + novel_text
            ),
        },
    ]


# --- 多块记忆相关 Prompt ---

MEMORY_SYSTEM_PROMPT = """\
你是一名专业编剧。你的任务是将小说片段转换为结构化的 YAML 格式剧本。

**重要上下文信息：**

{character_registry}

{event_memory}

**当前片段是小说的第 {chunk_index} / {total_chunks} 部分。**
请从上文自然衔接处开始转换，避免与已转换内容重复。
如果当前片段与前文有重叠（上下文衔接区域），请跳过已转换的部分，只处理新内容。

请只输出合法的 YAML —— 不要用 markdown 代码块包裹，不要添加任何额外的说明文字。\
YAML 必须严格符合以下结构:

```yaml
title: "剧本标题"
scenes:
  - scene_number: {start_scene_number}
    slugline: "内/外. 地点 - 时间（如：内. 古宅门厅 - 夜）"
    content:
      - type: "action"
        text: "动作或舞台指示的描述文字"
      - type: "dialogue"
        character: "角色名"
        parenthetical: "情绪或动作提示（可选，无则留空）"
        line: "台词正文"
characters:
  - name: "角色名"
    description: "角色简介"
```

核心规则:
- scene_number 从 {start_scene_number} 开始编号，不得与之前的场景编号冲突。
- content 列表中的项目按顺序排列，必须忠实还原小说中动作与对话的交错节奏。
- 每个 content 项只能是 action 或 dialogue 类型。
- 根据场景切换（地点变化）或时间跳跃将叙事拆分为不同的场次。
- slugline 格式: "内/外. 具体地点 - 时间"。
- 保留原著故事的主线、关键对话和情感基调。
- 所有有台词的角色必须出现在 characters 列表中。
- 已在角色注册表中的角色无需重新描述，仅需列出 name；如有新的角色信息变化可更新 description。
"""

MEMORY_UPDATE_PROMPT = """\
你是一名编剧助手。请根据以下剧本片段，提取角色信息和剧情摘要。

**当前角色注册表（已有角色，请在此基础上更新）：**
{current_characters}

请输出合法的 YAML，格式如下：

```yaml
characters:
  - name: "角色名"
    description: "角色的最新简介（如有新信息则更新，否则保持原样）"
    is_main: true/false
event_summary: "用 2-3 句话概括本片段发生的关键事件和情节推进"
```

规则：
- 只输出在本片段中出现或有新信息的角色。
- is_main=true 为主要角色（有大量台词和戏份），is_main=false 为配角/龙套。
- event_summary 要简洁，聚焦于情节推进和人物关系变化。
"""


def _format_character_registry(character_registry: dict[str, dict]) -> str:
    """将角色注册表格式化为 prompt 中的文本。"""
    if not character_registry:
        return "**角色注册表：**（暂无）"
    char_lines = []
    for name, info in character_registry.items():
        main_tag = "（主角）" if info.get("is_main") else ""
        char_lines.append(f"- {name}{main_tag}: {info.get('description', '')}")
    return "**角色注册表：**\n" + "\n".join(char_lines)


def build_memory_prompt(
    novel_text: str,
    character_registry: dict[str, dict],
    event_memory: str,
    chunk_index: int,
    total_chunks: int,
    start_scene_number: int,
) -> list[dict]:
    """构建带记忆上下文的剧本生成 prompt。

    Args:
        novel_text: 当前块的小说文本
        character_registry: {name: {description, is_main}} 角色注册表
        event_memory: 压缩后的事件记忆摘要文本
        chunk_index: 当前块序号（从1开始）
        total_chunks: 总块数
        start_scene_number: 本块场景的起始编号
    """
    char_text = _format_character_registry(character_registry)
    event_text = f"**已发生的情节：**\n{event_memory}" if event_memory else "**已发生的情节：**（本片段为小说开头）"

    system_content = MEMORY_SYSTEM_PROMPT.format(
        character_registry=char_text,
        event_memory=event_text,
        chunk_index=chunk_index,
        total_chunks=total_chunks,
        start_scene_number=start_scene_number,
    )

    return [
        {"role": "system", "content": system_content},
        {
            "role": "user",
            "content": "请将以下小说片段转换为剧本，按照上述 YAML 格式输出：\n\n" + novel_text,
        },
    ]


def build_memory_update_prompt(
    script_fragment_yaml: str,
    current_characters: dict[str, dict],
) -> list[dict]:
    """构建用于提取角色更新和事件摘要的 prompt。

    Args:
        script_fragment_yaml: 当前块生成的剧本 YAML 文本
        current_characters: 当前角色注册表
    """
    if current_characters:
        char_lines = []
        for name, info in current_characters.items():
            main_tag = "（主角）" if info.get("is_main") else ""
            char_lines.append(f"- {name}{main_tag}: {info.get('description', '')}")
        char_text = "\n".join(char_lines)
    else:
        char_text = "（暂无）"

    system_content = MEMORY_UPDATE_PROMPT.format(current_characters=char_text)

    return [
        {"role": "system", "content": system_content},
        {
            "role": "user",
            "content": f"以下是本片段生成的剧本 YAML：\n\n{script_fragment_yaml}",
        },
    ]


# --- 审查 Agent Prompt ---

REVIEW_PROMPT = """\
你是一名剧本审查员。请检查以下剧本片段是否符合规范。

**角色注册表（已知角色）：**
{current_characters}

**场景编号要求：** 本块的 scene_number 必须从 {start_scene_number} 开始。

**检查规则：**
1. scene_number 是否从 {start_scene_number} 开始递增
2. 所有 dialogue 中的 character 名是否出现在 characters 列表中
3. 角色名是否与注册表中的已有角色名保持一致（不要出现"叶凡"和"叶同学"等不一致写法）
4. content 列表中 action 和 dialogue 是否合理交替
5. 是否有明显的剧情逻辑问题（如已死亡角色复活、场景地点矛盾等）

请输出合法的 YAML，格式如下：

```yaml
valid: true/false
issues:
  - "问题描述 1"
  - "问题描述 2"
suggestions: "修正建议"
```

规则：
- 如果没有问题，valid=true，issues 为空列表，suggestions 留空。
- 如果有问题，valid=false，列出所有发现的问题，并给出具体修正建议。
"""


def build_review_prompt(
    script_fragment_yaml: str,
    current_characters: dict[str, dict],
    start_scene_number: int,
) -> list[dict]:
    """构建审查 Agent 的 prompt。

    Args:
        script_fragment_yaml: 当前块生成的剧本 YAML 文本
        current_characters: 当前角色注册表
        start_scene_number: 本块场景的起始编号
    """
    char_text = _format_character_registry(current_characters)

    system_content = REVIEW_PROMPT.format(
        current_characters=char_text,
        start_scene_number=start_scene_number,
    )

    return [
        {"role": "system", "content": system_content},
        {
            "role": "user",
            "content": f"以下是待审查的剧本片段 YAML：\n\n{script_fragment_yaml}",
        },
    ]
