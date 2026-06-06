"""共享的响应构建函数 —— server.py 和 worker.py 共用。"""

from src.parser import Script


def script_to_response(script: Script, novel_name: str) -> dict:
    """将 Script 对象转换为 API 响应字典。"""
    scenes_summary = []
    for s in script.scenes:
        dialogue_count = sum(1 for c in s.content if c.type == "dialogue")
        action_count = sum(1 for c in s.content if c.type == "action")
        scenes_summary.append({
            "scene_number": s.scene_number,
            "slugline": s.slugline,
            "dialogue_count": dialogue_count,
            "action_count": action_count,
        })

    return {
        "novel_name": novel_name,
        "title": script.title,
        "scene_count": len(script.scenes),
        "character_count": len(script.characters),
        "scenes": scenes_summary,
        "characters": [
            {"name": c.name, "description": c.description}
            for c in script.characters
        ],
        "script": script.model_dump(),
    }
