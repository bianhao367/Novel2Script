"""Prompt 构造器 —— 为小说转剧本任务构建 LLM 对话 prompt。"""

SYSTEM_PROMPT = """\
你是一名专业编剧。你的任务是将小说片段转换为结构化的 YAML 格式剧本。

请只输出合法的 YAML —— 不要用 markdown 代码块包裹，不要添加任何额外的说明文字。\
YAML 必须符合以下结构:

```yaml
title: "剧本标题"
acts:
  - act_number: 1
    scenes:
      - scene_number: 1
        setting: "场景描述（地点、氛围）"
        characters_present:
          - "角色名"
        dialogues:
          - character: "角色名"
            line: "台词"
            action: "可选的动作/情绪提示"
        stage_directions: "可选的舞台指示"
characters:
  - name: "角色名"
    description: "角色简介"
```

规则:
- 保留原著故事的主线、关键对话和情感基调。
- 根据场景切换或时间跳跃将叙事拆分为不同的场次。
- 对于小说中模糊不清的地方，按照影视剧本惯例进行合理推断。
- 所有有台词的角色必须出现在顶层 `characters` 列表中。
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
