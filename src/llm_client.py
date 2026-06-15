"""
LLM 客户端
==========
封装 OpenAI（兼容）API 的调用，支持同步和流式两种模式。

特性：
- 自动重试：速率限制、超时、连接失败时指数退避重试（最多 3 次）
- 双通道流式：chat_stream() 同时输出 reasoning（思考过程）和 content（正文）
- 兼容 DeepSeek R1 等支持 reasoning_content 的模型

使用方式：
    client = LLMClient(config)
    reply = client.chat(messages)              # 同步调用
    for chunk in client.chat_stream(messages): # 流式调用
        print(chunk["type"], chunk["content"])
"""

import time

# openai 库提供 OpenAI 客户端和各种异常类型
# 大部分 LLM 服务（OpenAI、DeepSeek、通义千问等）都兼容 OpenAI API 格式
from openai import (
    OpenAI,              # 核心客户端，发 HTTP 请求给 LLM API
    APIError,            # 通用 API 错误（非特定类型）
    APIConnectionError,  # 网络连接失败
    APITimeoutError,     # 请求超时
    RateLimitError,      # 速率限制（请求太频繁）
    AuthenticationError, # 认证失败（API Key 错误）
)

from src.config import Config
from src.logger import get_logger

logger = get_logger()


class LLMError(Exception):
    """LLM 调用失败（已重试后仍失败）。

    上层代码（pipeline.py）捕获此异常来区分"LLM 调用失败"和其他 Python 错误。
    LLMError 表示可以重试或跳过，其他异常可能需要终止流程。
    """


class LLMClient:
    """OpenAI Python SDK 的封装，带自动重试和流式支持。

    封装了以下功能：
    - 统一错误处理：把各种 API 错误转为 LLMError
    - 自动重试：速率限制/超时/连接失败时指数退避重试（2s → 4s → 8s）
    - 流式支持：chat_stream() 是生成器，逐块 yield，适合 SSE/WebSocket 推送
    """

    def __init__(self, config: Config):  # config: 全局配置对象（含 API 地址、密钥、模型名等）
        self.config = config               # 全局配置，用于获取 model、temperature、max_tokens
        self._client = OpenAI(             # OpenAI SDK 客户端实例，负责发 HTTP 请求
            base_url=config.api.base_url,  # 兼容 OpenAI 格式的 API 地址（可以是 OpenAI、DeepSeek、本地 Ollama 等）
            api_key=config.api.api_key,
            timeout=120.0,   # 单次请求超时 120 秒（LLM 生成可能很慢）
            max_retries=0,   # 禁用 SDK 自带重试，由我们自己的 chat() 方法控制重试逻辑
        )

    def chat(self, messages: list[dict], max_tokens: int | None = None) -> str:  # messages: OpenAI 格式消息列表, max_tokens: 最大生成 token 数（覆盖配置默认值）
        """同步调用 LLM，返回完整文本响应。失败时自动重试。

        执行流程：
        1. 进入 for attempt in range(3) 重试循环
        2. 调用 self._client.chat.completions.create() 发送 HTTP 请求
        3. 从 response.choices[0].message.content 提取回复文本
        4. 如果 content 为 None（被安全过滤拦截），抛出 LLMError

        可重试的错误（临时性故障，等 2s → 4s → 8s 再试）：
        - RateLimitError: 速率限制，API 要求等一会儿
        - APITimeoutError: 请求超时（默认 120 秒），服务端负载高
        - APIConnectionError: 网络连接失败，重连可能恢复

        不可重试的错误（立即抛出 LLMError）：
        - AuthenticationError: API Key 错误
        - APIError: 其他服务端错误
        """
        delay = 2.0  # 初始退避时间（秒），每次重试翻倍

        for attempt in range(3):  # 最多重试 3 次（含首次）
            try:
                response = self._client.chat.completions.create(
                    model=self.config.model,          # 模型名，如 "gpt-4o"、"deepseek-chat"
                    messages=messages,                 # 消息列表：[{"role": "system", "content": "..."}, {"role": "user", "content": "..."}]
                    temperature=self.config.temperature,  # 温度：0=确定性，1=随机，0.7 是平衡点
                    max_tokens=max_tokens or self.config.max_tokens,  # 最大输出 token 数
                )
                content = response.choices[0].message.content
                if content is None:
                    raise LLMError("LLM 返回了空内容（可能被安全过滤拦截）")
                return content

            except AuthenticationError as e:
                # 认证失败直接报错，不重试（换 Key 才能解决）
                raise LLMError(f"API 认证失败，请检查 .env 中的 OPENAI_API_KEY: {e}") from e

            except RateLimitError as e:
                if attempt < 2:  # 还有重试机会
                    logger.warning(f"速率限制，{delay:.0f}s 后重试 (第{attempt+1}/3次)...")
                    time.sleep(delay)  # 阻塞等待，不能用 asyncio.sleep（这是同步方法）
                    delay *= 2  # 指数退避：2s → 4s → 8s
                else:
                    raise LLMError(f"速率限制，已重试3次仍失败: {e}") from e

            except APITimeoutError as e:
                if attempt < 2:
                    logger.warning(f"请求超时，{delay:.0f}s 后重试 (第{attempt+1}/3次)...")
                    time.sleep(delay)
                    delay *= 2
                else:
                    raise LLMError(f"请求超时，已重试3次仍失败: {e}") from e

            except APIConnectionError as e:
                if attempt < 2:
                    logger.warning(f"连接失败，{delay:.0f}s 后重试 (第{attempt+1}/3次)...")
                    time.sleep(delay)
                    delay *= 2
                else:
                    raise LLMError(f"网络连接失败，已重试3次仍失败: {e}") from e

            except APIError as e:
                # 其他 API 错误（非速率/连接/超时/认证），通常不可恢复，直接抛出
                raise LLMError(f"API 错误: {e}") from e

    def chat_stream(self, messages: list[dict]):  # messages: OpenAI 格式的消息列表
        """流式调用 LLM，逐块 yield {"type": "reasoning"|"content", "content": str}。

        执行流程：
        1. 调用 self._client.chat.completions.create(stream=True) 开启流式模式
        2. API 返回一个 SSE 迭代器，每次循环拿到一个 chunk（一小段文本）
        3. 从 chunk.choices[0].delta 中提取文本：
           - delta.reasoning_content → yield {"type": "reasoning", "content": ...}（思考过程）
           - delta.content → yield {"type": "content", "content": ...}（正式回答）
        4. 流式传输完成后 return，退出重试循环

        两种输出类型：
        - "reasoning": 模型的思考过程（如 DeepSeek R1 的思维链），用户通常看不到
        - "content": 模型的正式回答，推送给用户的内容
        """
        delay = 2.0

        for attempt in range(3):
            try:
                stream = self._client.chat.completions.create(
                    model=self.config.model,
                    messages=messages,
                    temperature=self.config.temperature,
                    max_tokens=self.config.max_tokens,
                    stream=True,  # 关键：开启流式模式，API 会返回 SSE 事件流而不是完整 JSON
                    stream_options={"include_usage": False},  # 不返回 token 用量统计（省带宽）
                )
                # stream 是一个迭代器，每次循环拿到一个 SSE 事件（一小段文本）
                for chunk in stream:
                    delta = chunk.choices[0].delta if chunk.choices else None
                    if delta:
                        # reasoning_content 是 DeepSeek R1 等模型特有的"思考过程"字段
                        # 普通模型（GPT-4o 等）没有这个字段，getattr 返回 None
                        rc = getattr(delta, 'reasoning_content', None)
                        if rc:
                            yield {"type": "reasoning", "content": rc}
                        if delta.content:
                            yield {"type": "content", "content": delta.content}
                return  # 流式传输成功完成，退出重试循环

            # 以下重试逻辑与 chat() 完全相同
            except AuthenticationError as e:
                raise LLMError(f"API 认证失败: {e}") from e

            except RateLimitError as e:
                if attempt < 2:
                    logger.warning(f"速率限制，{delay:.0f}s 后重试 (第{attempt+1}/3次)...")
                    time.sleep(delay)
                    delay *= 2
                else:
                    raise LLMError(f"速率限制，已重试3次: {e}") from e

            except APITimeoutError as e:
                if attempt < 2:
                    logger.warning(f"请求超时，{delay:.0f}s 后重试 (第{attempt+1}/3次)...")
                    time.sleep(delay)
                    delay *= 2
                else:
                    raise LLMError(f"请求超时，已重试3次: {e}") from e

            except APIConnectionError as e:
                if attempt < 2:
                    logger.warning(f"连接失败，{delay:.0f}s 后重试 (第{attempt+1}/3次)...")
                    time.sleep(delay)
                    delay *= 2
                else:
                    raise LLMError(f"连接失败，已重试3次: {e}") from e

            except APIError as e:
                raise LLMError(f"API 错误: {e}") from e
