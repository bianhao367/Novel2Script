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

from openai import (
    OpenAI,
    APIError,
    APIConnectionError,
    APITimeoutError,
    RateLimitError,
    AuthenticationError,
)

from src.config import Config


class LLMError(Exception):
    """LLM 调用失败（已重试后仍失败）。"""


class LLMClient:
    """OpenAI Python SDK 的封装，带自动重试和流式支持。"""

    def __init__(self, config: Config):
        self.config = config
        self._client = OpenAI(
            base_url=config.api.base_url,
            api_key=config.api.api_key,
            timeout=120.0,   # 单次请求超时 120 秒
            max_retries=0,   # 禁用 SDK 自带重试，由我们自己控制
        )

    def chat(self, messages: list[dict]) -> str:
        """发送 chat completion 请求，失败时自动重试。"""
        last_error: Exception | None = None
        delay = 2.0  # 初始退避时间（秒）

        for attempt in range(3):
            try:
                response = self._client.chat.completions.create(
                    model=self.config.model,
                    messages=messages,
                    temperature=self.config.temperature,
                    max_tokens=self.config.max_tokens,
                )
                content = response.choices[0].message.content
                if content is None:
                    raise LLMError("LLM 返回了空内容（可能被安全过滤拦截）")
                return content

            except AuthenticationError as e:
                raise LLMError(f"API 认证失败，请检查 .env 中的 OPENAI_API_KEY: {e}") from e

            except RateLimitError as e:
                last_error = e
                if attempt < 2:
                    print(f"速率限制，{delay:.0f}s 后重试 (第{attempt+1}/3次)...")
                    time.sleep(delay)
                    delay *= 2
                else:
                    raise LLMError(f"速率限制，已重试3次仍失败: {e}") from e

            except APITimeoutError as e:
                last_error = e
                if attempt < 2:
                    print(f"请求超时，{delay:.0f}s 后重试 (第{attempt+1}/3次)...")
                    time.sleep(delay)
                    delay *= 2
                else:
                    raise LLMError(f"请求超时，已重试3次仍失败: {e}") from e

            except APIConnectionError as e:
                last_error = e
                if attempt < 2:
                    print(f"连接失败，{delay:.0f}s 后重试 (第{attempt+1}/3次)...")
                    time.sleep(delay)
                    delay *= 2
                else:
                    raise LLMError(f"网络连接失败，已重试3次仍失败: {e}") from e

            except APIError as e:
                # 其他 API 错误（非速率/连接/超时/认证），通常不可恢复
                raise LLMError(f"API 错误: {e}") from e

        # 理论上不会走到这里，但兜底
        raise LLMError(f"调用失败: {last_error}")

    def chat_stream(self, messages: list[dict]):
        """Streaming chat completion，逐个 yield {"type": "reasoning"|"content", "content": str}。"""
        last_error: Exception | None = None
        delay = 2.0

        for attempt in range(3):
            try:
                stream = self._client.chat.completions.create(
                    model=self.config.model,
                    messages=messages,
                    temperature=self.config.temperature,
                    max_tokens=self.config.max_tokens,
                    stream=True,
                    stream_options={"include_usage": False},
                )
                for chunk in stream:
                    delta = chunk.choices[0].delta if chunk.choices else None
                    if delta:
                        rc = getattr(delta, 'reasoning_content', None)
                        if rc:
                            yield {"type": "reasoning", "content": rc}
                        if delta.content:
                            yield {"type": "content", "content": delta.content}
                return  # 成功完成

            except AuthenticationError as e:
                raise LLMError(f"API 认证失败: {e}") from e

            except RateLimitError as e:
                last_error = e
                if attempt < 2:
                    print(f"速率限制，{delay:.0f}s 后重试 (第{attempt+1}/3次)...")
                    time.sleep(delay)
                    delay *= 2
                else:
                    raise LLMError(f"速率限制，已重试3次: {e}") from e

            except APITimeoutError as e:
                last_error = e
                if attempt < 2:
                    print(f"请求超时，{delay:.0f}s 后重试 (第{attempt+1}/3次)...")
                    time.sleep(delay)
                    delay *= 2
                else:
                    raise LLMError(f"请求超时，已重试3次: {e}") from e

            except APIConnectionError as e:
                last_error = e
                if attempt < 2:
                    print(f"连接失败，{delay:.0f}s 后重试 (第{attempt+1}/3次)...")
                    time.sleep(delay)
                    delay *= 2
                else:
                    raise LLMError(f"连接失败，已重试3次: {e}") from e

            except APIError as e:
                raise LLMError(f"API 错误: {e}") from e

        raise LLMError(f"调用失败: {last_error}")
