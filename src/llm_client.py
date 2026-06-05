"""LLM 客户端 —— 封装 OpenAI（兼容）API 的调用。"""

from openai import OpenAI

from src.config import ApiConfig


class LLMClient:
    """OpenAI Python SDK 的薄封装。"""

    def __init__(self, config: ApiConfig):
        self.config = config
        self._client = OpenAI(
            base_url=config.base_url,
            api_key=config.api_key,
        )

    def chat(self, messages: list[dict]) -> str:
        """发送 chat completion 请求，返回模型生成的文本。"""
        response = self._client.chat.completions.create(
            model=self.config.model,
            messages=messages,
            temperature=self.config.temperature,
            max_tokens=self.config.max_tokens,
        )
        return response.choices[0].message.content or ""
