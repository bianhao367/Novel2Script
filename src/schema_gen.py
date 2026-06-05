"""Schema 文档生成器 —— 根据 Pydantic 模型生成 JSON Schema 和 Markdown 参考文档。"""

import json
from pathlib import Path

from src.parser import SCRIPT_SCHEMA


def generate_json_schema(output_path: str | Path) -> dict:
    """从 Pydantic Script 模型生成 JSON Schema 并写入文件。"""
    schema = SCRIPT_SCHEMA.model_json_schema()

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(schema, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return schema


def generate_markdown_doc(output_path: str | Path) -> str:
    """生成可读的 Markdown 格式 Schema 参考文档。"""

    schema = SCRIPT_SCHEMA.model_json_schema()

    lines = [
        "# 剧本 Schema 参考",
        "",
        f"**标题**: {schema.get('title', 'Script')}",
        f"**描述**: {schema.get('description', '小说转剧本生成结果的 Schema 定义。')}",
        "",
        "## 顶层字段",
        "",
    ]

    props = schema.get("properties", {})
    for field_name, field_info in props.items():
        field_type = _describe_type(field_info)
        lines.append(f"- **`{field_name}`** ({field_type})")

        if "description" in field_info:
            lines.append(f"  - {field_info['description']}")

        ref = _resolve_ref(field_info, schema)
        if ref:
            lines.append("")
            lines.append(_render_nested(ref, schema, indent="    "))

    doc = "\n".join(lines)

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(doc, encoding="utf-8")
    return doc


def _describe_type(field_info: dict) -> str:
    """将 JSON Schema 的字段类型转为可读的中文描述。"""
    t = field_info.get("type", "object")
    if t == "array":
        items = field_info.get("items", {})
        item_type = items.get("$ref", items.get("type", "?"))
        if isinstance(item_type, str) and "/" in item_type:
            item_type = item_type.rsplit("/", 1)[-1]
        return f"{item_type} 数组"
    type_map = {
        "string": "字符串",
        "integer": "整数",
        "object": "对象",
    }
    return type_map.get(t, t)


def _resolve_ref(field_info: dict, root_schema: dict) -> dict | None:
    """如果字段是 $ref 引用或 $ref 数组，返回对应的定义。"""
    t = field_info.get("type", "object")
    if t == "array":
        field_info = field_info.get("items", {})
    ref = field_info.get("$ref", "")
    if ref:
        name = ref.rsplit("/", 1)[-1]
        return root_schema.get("$defs", {}).get(name)
    return None


def _render_nested(defn: dict, root_schema: dict, indent: str = "") -> str:
    """递归渲染子模型定义，生成 Markdown 嵌套列表。"""
    lines: list[str] = []
    props = defn.get("properties", {})
    required = defn.get("required", [])

    for field_name, field_info in props.items():
        field_type = _describe_type(field_info)
        req_mark = " *(必填)*" if field_name in required else ""
        lines.append(f"{indent}- **`{field_name}`** ({field_type}){req_mark}")

        if "description" in field_info:
            lines.append(f"{indent}  - {field_info['description']}")

        ref = _resolve_ref(field_info, root_schema)
        if ref:
            lines.append(_render_nested(ref, root_schema, indent=indent + "    "))

    return "\n".join(lines)
