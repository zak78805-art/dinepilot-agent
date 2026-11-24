from functools import lru_cache
import math
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, field_validator, model_validator


class Settings(BaseSettings):
    dashscope_api_key: str = ""
    dashscope_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    agent_model: str = "qwen3.7-plus"
    sky_backend_base_url: str = "http://localhost:8080"
    sky_backend_token: str = ""
    redis_url: str = "redis://localhost:6379/1"
    state_store_mode: Literal['memory', 'redis'] = 'memory'
    redis_key_prefix: str = "sky_agent"
    redis_socket_timeout_seconds: float = Field(default=1.5, gt=0, le=30.0)
    backend_http_max_connections: int = Field(default=50, gt=0, le=500)
    backend_http_max_keepalive: int = Field(default=20, ge=0, le=500)
    backend_http_timeout_seconds: float = Field(default=10.0, gt=0, le=120.0)
    agent_session_idle_seconds: int = Field(default=1800, gt=0)
    agent_session_max_entries: int = Field(default=500, gt=0)
    context_max_messages: int = Field(default=12, ge=4, le=200)
    context_keep_recent_messages: int = Field(default=6, ge=2, le=100)
    context_max_chars: int = Field(default=12000, ge=1000, le=200000)
    context_summary_max_chars: int = Field(default=1600, ge=200, le=10000)
    agent_stream_enabled: bool = True
    report_review_mode: Literal['off', 'mock', 'llm'] = 'off'
    report_review_model: str = ""
    report_review_max_tokens: int = Field(default=1536, ge=128, le=4096)
    report_review_http_timeout_seconds: float = Field(default=22.0, gt=0, le=120.0)
    report_review_max_entries: int = Field(default=200, gt=0)
    report_review_max_concurrency: int = Field(default=4, gt=0)
    report_review_max_pending: int = Field(default=16, gt=0)
    report_review_ttl_seconds: float = Field(default=600.0, gt=0)
    report_review_timeout_seconds: float = Field(default=26.0, gt=0, le=120.0)
    report_review_poll_after_ms: int = Field(default=1000, gt=0, le=30000)
    approval_max_entries: int = Field(default=200, gt=0)
    approval_ttl_seconds: float = Field(default=300.0, gt=0, le=3600.0)
    chat_rate_limit_per_minute: int = Field(default=20, gt=0, le=1000)
    daily_token_budget_per_user: int = Field(default=200000, gt=0, le=100_000_000)

    @field_validator(
        'backend_http_max_connections',
        'backend_http_max_keepalive',
        'agent_session_idle_seconds',
        'agent_session_max_entries',
        'context_max_messages',
        'context_keep_recent_messages',
        'context_max_chars',
        'context_summary_max_chars',
        'report_review_max_tokens',
        'report_review_max_entries',
        'report_review_max_concurrency',
        'report_review_max_pending',
        'report_review_poll_after_ms',
        'approval_max_entries',
        'chat_rate_limit_per_minute',
        'daily_token_budget_per_user',
        mode='before',
    )
    @classmethod
    def reject_boolean_limit(cls, value):
        if isinstance(value, bool):
            raise ValueError('限制必须为正整数')
        return value

    @field_validator(
        'redis_socket_timeout_seconds',
        'backend_http_timeout_seconds',
        'report_review_ttl_seconds',
        'report_review_timeout_seconds',
        'report_review_http_timeout_seconds',
        'approval_ttl_seconds',
        mode='before',
    )
    @classmethod
    def reject_boolean_or_non_finite_float(cls, value):
        if isinstance(value, bool):
            raise ValueError('时间限制必须为正数')
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError('时间限制必须为有限正数')
        return value

    @model_validator(mode='after')
    def validate_review_bounds(self):
        if self.report_review_max_concurrency > self.report_review_max_pending:
            raise ValueError('report_review_max_concurrency 不能大于 report_review_max_pending')
        if self.report_review_max_pending > self.report_review_max_entries:
            raise ValueError('report_review_max_pending 不能大于 report_review_max_entries')
        if self.report_review_http_timeout_seconds > self.report_review_timeout_seconds:
            raise ValueError('report_review_http_timeout_seconds 不能大于 report_review_timeout_seconds')
        if self.backend_http_max_keepalive > self.backend_http_max_connections:
            raise ValueError('backend_http_max_keepalive 不能大于 backend_http_max_connections')
        if self.context_keep_recent_messages >= self.context_max_messages:
            raise ValueError('context_keep_recent_messages 必须小于 context_max_messages')
        return self

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
