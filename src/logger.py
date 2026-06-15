"""
统一日志模块
============
提供控制台 + 文件双通道日志。控制台保持原有 print 级别的输出，
文件记录完整时间戳和级别，便于事后排查。

使用方式：
    from src.logger import get_logger, setup_log_file

    logger = get_logger()
    logger.info("处理第 1 块")
    logger.warning("速率限制，2s 后重试")
    logger.error("编排 Agent 解析失败", exc_info=True)  # 带 traceback

    # 确定输出目录后，开启文件日志：
    setup_log_file(Path("output/小说名"))
"""

import logging
from pathlib import Path

_logger: logging.Logger | None = None
_file_handler: logging.FileHandler | None = None


def get_logger() -> logging.Logger:
    """获取全局 logger 实例，首次调用时初始化控制台输出。"""
    global _logger

    if _logger is None:
        _logger = logging.getLogger("novel2script")
        _logger.setLevel(logging.DEBUG)

        # 控制台 handler：INFO 及以上，简洁格式
        console = logging.StreamHandler()
        console.setLevel(logging.INFO)
        console.setFormatter(logging.Formatter("%(message)s"))
        _logger.addHandler(console)

    return _logger


def setup_log_file(log_dir: Path) -> None:
    """在输出目录下创建 run.log，记录完整时间戳和级别。"""
    global _file_handler

    logger = get_logger()

    # 先移除旧的 file handler（如果重新设置）
    if _file_handler is not None:
        logger.removeHandler(_file_handler)

    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "run.log"

    _file_handler = logging.FileHandler(str(log_path), encoding="utf-8")
    _file_handler.setLevel(logging.DEBUG)
    _file_handler.setFormatter(logging.Formatter(
        "[%(asctime)s] [%(levelname)-7s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logger.addHandler(_file_handler)
    logger.info(f"日志文件: {log_path}")
