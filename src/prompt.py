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


# --- 导演 Agent Prompt ---

DIRECTOR_PROMPT = """\
你是一名资深导演。你的任务是通读小说片段，提取全局信息以便后续编剧团队使用。

**当前片段是小说的第 {chunk_index} / {total_chunks} 部分。**

**已有角色注册表（请在此基础上更新）：**
{current_characters}

请输出合法的 YAML，格式如下：

```yaml
characters:
  - name: "角色名"
    description: "角色简介（性格、外貌、身份、关系）"
    is_main: true/false
plot_events: "本片段发生的关键剧情事件（1-3句话）"
```

规则：
- 列出本片段中出现的所有角色，is_main=true 标记主角（戏份多、推动剧情）
- 已在角色注册表中的角色：如有新信息则更新 description，否则仅列出 name
- plot_events 聚焦于情节推进和人物关系变化，简洁概括
- 不要遗漏重要角色或关键剧情转折
"""


def build_director_prompt(
    novel_text: str,
    current_characters: dict[str, dict],
    chunk_index: int,
    total_chunks: int,
) -> list[dict]:
    """构建导演 Agent 的 prompt。

    Args:
        novel_text: 当前块的小说文本
        current_characters: 当前已积累的角色注册表
        chunk_index: 当前块序号（从1开始）
        total_chunks: 总块数
    """
    char_text = _format_character_registry(current_characters)

    system_content = DIRECTOR_PROMPT.format(
        current_characters=char_text,
        chunk_index=chunk_index,
        total_chunks=total_chunks,
    )

    return [
        {"role": "system", "content": system_content},
        {
            "role": "user",
            "content": "请分析以下小说片段，提取角色和剧情信息：\n\n" + novel_text,
        },
    ]


# --- 编排 Agent Prompt ---

ORCHESTRATOR_PROMPT = """\
你是一名剧本编排师。你的任务是分析小说片段的结构，确定场景划分和段落类型。

**角色注册表（已知角色）：**
{current_characters}

请输出合法的 YAML，格式如下：

```yaml
scenes:
  - scene_number: {start_scene_number}
    slugline: "内/外. 地点 - 时间"
    paragraphs:
      - para_idx: 0
        content_type: "action"
      - para_idx: 1
        content_type: "dialogue"
      - para_idx: 2
        content_type: "action"
```

规则：
- 按场景切换（地点变化、时间跳跃）划分场景
- slugline 格式: "内/外. 具体地点 - 时间"
- 对每个段落标记 content_type：包含角色对话的标为 "dialogue"，其余标为 "action"
- para_idx 从 0 开始，按原文段落顺序递增
- scene_number 从 {start_scene_number} 开始
- 不要生成任何剧本内容，只做结构分析
"""


def build_orchestrator_prompt(
    novel_text: str,
    current_characters: dict[str, dict],
    start_scene_number: int,
) -> list[dict]:
    """构建编排 Agent 的 prompt。"""
    char_text = _format_character_registry(current_characters)

    system_content = ORCHESTRATOR_PROMPT.format(
        current_characters=char_text,
        start_scene_number=start_scene_number,
    )

    # 为段落编号，方便 LLM 引用 para_idx
    paragraphs = novel_text.split("\n\n")
    numbered_text = ""
    for i, para in enumerate(paragraphs):
        para = para.strip()
        if para:
            numbered_text += f"[段落{i}] {para}\n\n"

    return [
        {"role": "system", "content": system_content},
        {
            "role": "user",
            "content": "请分析以下小说片段的结构：\n\n" + numbered_text,
        },
    ]


# --- 对白专家 Prompt ---

DIALOGUE_AGENT_PROMPT = """\
你是一名专业编剧，专门负责编写对白。你的任务是根据小说片段中的对话内容，提取并优化角色台词。

**角色注册表：**
{current_characters}

请输出合法的 YAML，格式如下：

```yaml
items:
  - scene_number: 1
    para_idx: 1
    character: "角色名"
    line: "台词正文"
    parenthetical: "情绪或动作提示"
```

规则：
- 只处理标为 dialogue 的段落（包含角色对话的段落）
- para_idx 必须与输入中标注的段落编号一致
- character 必须与角色注册表中的名称一致
- line 是角色的实际台词，忠实还原原著对话
- parenthetical 是情绪/动作提示（可选），如"愤怒地"、"低声"、"转身"
- 如果一个段落包含多句对白，拆分为多个 items（相同 para_idx）
- 不要输出任何 action/舞台指示内容
"""


def build_dialogue_agent_prompt(
    novel_text: str,
    current_characters: dict[str, dict],
    dialogue_paragraphs: list[tuple[int, str]],
    start_scene_number: int,
) -> list[dict]:
    """构建对白专家的 prompt。

    Args:
        novel_text: 原始小说文本（用于上下文参考）
        current_characters: 角色注册表
        dialogue_paragraphs: [(para_idx, text), ...] 对白段落列表
        start_scene_number: 起始场景号
    """
    char_text = _format_character_registry(current_characters)
    # 将对白段落带编号拼接
    para_text = "\n\n".join(f"[段落{idx}] {text}" for idx, text in dialogue_paragraphs)

    system_content = DIALOGUE_AGENT_PROMPT.format(current_characters=char_text)

    return [
        {"role": "system", "content": system_content},
        {
            "role": "user",
            "content": "请从以下对白段落中提取角色台词：\n\n" + para_text,
        },
    ]


# --- 动作专家 Prompt ---

ACTION_AGENT_PROMPT = """\
你是一名专业编剧，专门负责编写动作和舞台指示。你的任务是根据小说片段中的描写内容，转换为剧本中的动作描述。

**角色注册表：**
{current_characters}

请输出合法的 YAML，格式如下：

```yaml
items:
  - scene_number: 1
    para_idx: 0
    text: "动作或舞台指示的描述文字"
```

规则：
- 只处理标为 action 的段落（场景描写、动作描述、心理活动等）
- para_idx 必须与输入中标注的段落编号一致
- text 是舞台指示或场景描写，用现在时态
- 忠实还原原著的场景氛围和人物动作
- 不要输出任何对白/台词内容
- 如果一个段落包含多个动作描述，可以拆分为多个 items（相同 para_idx）
"""


def build_action_agent_prompt(
    novel_text: str,
    current_characters: dict[str, dict],
    action_paragraphs: list[tuple[int, str]],
    start_scene_number: int,
) -> list[dict]:
    """构建动作专家的 prompt。

    Args:
        novel_text: 原始小说文本（用于上下文参考）
        current_characters: 角色注册表
        action_paragraphs: [(para_idx, text), ...] 动作段落列表
        start_scene_number: 起始场景号
    """
    char_text = _format_character_registry(current_characters)
    # 将动作段落带编号拼接
    para_text = "\n\n".join(f"[段落{idx}] {text}" for idx, text in action_paragraphs)

    system_content = ACTION_AGENT_PROMPT.format(current_characters=char_text)

    return [
        {"role": "system", "content": system_content},
        {
            "role": "user",
            "content": "请将以下动作段落转换为舞台指示：\n\n" + para_text,
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


def build_fix_prompt(
    script_fragment_yaml: str,
    review_issues: list[str],
    review_suggestions: str,
    original_novel_text: str,
) -> list[dict]:
    """构建修正 prompt，将审查问题注入上下文供编剧针对性修改。

    Args:
        script_fragment_yaml: 当前剧本 YAML 文本
        review_issues: 审查发现的问题列表
        review_suggestions: 审查给出的修正建议
        original_novel_text: 原始小说片段（供编剧参考还原）
    """
    issues_text = "\n".join(f"- {issue}" for issue in review_issues)

    return [
        {"role": "system", "content": (
            "你是一名专业编剧。审查员发现了以下问题，请针对性修正剧本。\n"
            "只输出修正后的完整 YAML，不要添加额外文字。"
        )},
        {"role": "user", "content": (
            f"审查发现的问题：\n{issues_text}\n\n"
            f"修正建议：{review_suggestions}\n\n"
            f"原始小说片段（供参考）：\n{original_novel_text}\n\n"
            f"当前剧本 YAML：\n{script_fragment_yaml}"
        )},
    ]
