#!/usr/bin/env python
"""
Novel2Script CLI
================
将小说文本转换为结构化剧本（基于 LLM）的命令行入口。

这是项目的最简使用方式，直接读取本地 .txt 文件并输出剧本。
Web 服务模式请使用 server.py。

用法：
    python main.py <小说.txt>

示例：
    python main.py "遮天.txt"
    python main.py "三体.txt"

输出目录默认为 ./output/<小说名>/，可在 config.py 中修改。
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
    args = parser.parse_args()

    try:
        script = run_pipeline(args.novel_path)
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
