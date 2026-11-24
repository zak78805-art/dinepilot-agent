from uuid import uuid4

from pydantic import BaseModel, Field, ConfigDict, field_validator


class AgentChatRequest(BaseModel):
    session_id: str = Field(default_factory=lambda: f"session_{uuid4().hex}", min_length=1,
                            max_length=128, pattern=r'^[a-zA-Z0-9_-]+$')
    message: str = Field(min_length=1, max_length=4000)

    @field_validator('message')
    @classmethod
    def reject_blank_message(cls, value: str) -> str:
        if not value.strip():
            raise ValueError('问题不能只包含空白字符')
        return value


class ApprovalDecisionRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    reason: str | None = Field(default=None, max_length=200)


class CustomerChatRequest(AgentChatRequest):
    model_config = ConfigDict(extra='forbid')
    session_id: str = Field(default_factory=lambda: f'customer_{uuid4().hex}', min_length=1,
                            max_length=128, pattern=r'^[a-zA-Z0-9_-]+$')

