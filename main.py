#!/usr/bin/env python
"""Novel2Script —— 将小说文本转换为结构化剧本（基于 LLM）。

用法:
    python main.py <小说.txt> [--config .config]
"""

import argparse
import sys

from src.pipeline import run_pipeline


def main():
    parser = argparse.ArgumentParser(
        description="将小说文本转换为结构化剧本（基于 LLM）。",
    )
    parser.add_argument(
        "novel_path",
        help="小说 .txt 文件路径",
    )
    parser.add_argument(
        "--config", "-c",
        default=".config",
        help="配置文件路径（默认: .config）",
    )
    args = parser.parse_args()

    try:
        script = run_pipeline(args.novel_path, config_path=args.config)
        print(f"\n完成。剧本标题: {script.title}")
    except FileNotFoundError as e:
        print(f"错误: 文件未找到 — {e}", file=sys.stderr)
        sys.exit(1)
    except ValueError as e:
        print(f"错误: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"未知错误: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
