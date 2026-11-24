"""Bounded OpenAI-compatible LLM reviewer factory (lazy client, zero tools, no retries)."""
from collections.abc import Awaitable, Callable

import httpx
from openai import AsyncOpenAI

from app.review.text_reviewer import Generate, Messages, sdk_generator


def build_llm_review_generator(
    *,
    api_key: str,
    base_url: str,
    model: str,
    max_tokens: int = 1536,
    http_timeout: float = 12.5,
    http_client: httpx.AsyncClient | None = None,
    usage_sink: Callable[[dict[str, int]], None] | None = None,
) -> tuple[Generate, Callable[[], Awaitable[None]]]:
    """Build a lazy, bounded LLM generator and its async closer.

    Does not construct a network client until a review actually executes, and
    never loads secrets or enables tools/retries.
    """
    state: dict[str, AsyncOpenAI | None] = {'client': None}

    def _get_client() -> AsyncOpenAI:
        if not isinstance(api_key, str) or not api_key.strip():
            raise RuntimeError('未配置复核模型密钥')
        if not isinstance(model, str) or not model.strip():
            raise RuntimeError('未配置复核模型名称')
        if state['client'] is None:
            client_kwargs: dict[str, object] = {
                'api_key': api_key.strip(),
                'base_url': base_url,
                'max_retries': 0,
            }
            if http_client is not None:
                client_kwargs['http_client'] = http_client
            else:
                client_kwargs['timeout'] = httpx.Timeout(
                    connect=5.0,
                    read=http_timeout,
                    write=5.0,
                    pool=5.0,
                )
            state['client'] = AsyncOpenAI(**client_kwargs)
        return state['client']

    async def generate(messages: Messages) -> str | None:
        client = _get_client()
        inner = sdk_generator(
            client,
            model.strip(),
            max_tokens=max_tokens,
            timeout=http_timeout,
            usage_sink=usage_sink,
        )
        return await inner(messages)

    async def close() -> None:
        client = state['client']
        state['client'] = None
        if client is not None:
            await client.close()

    return generate, close
