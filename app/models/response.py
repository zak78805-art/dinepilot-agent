from typing import Literal

from pydantic import BaseModel, Field


ReportReviewStatus = Literal['pending', 'running', 'pass', 'revise', 'unavailable']
ReportReviewFailure = Literal[
    'bad_input',
    'timeout',
    'model_error',
    'bad_output',
    'queue_full',
    'cancelled',
    'not_found',
    'unavailable',
]
ApprovalStatus = Literal['pending', 'executing', 'executed', 'rejected', 'expired', 'failed']


class SessionClearResponse(BaseModel):
    agent_role: Literal['merchant', 'customer']
    session_id: str
    cleared: bool


class ApprovalTargetInfo(BaseModel):
    dish_id: int
    dish_name: str
    category: str | None = None
    price: float | None = None


class ApprovalStateSnapshot(BaseModel):
    status: int
    status_name: str


class ApprovalTicketResponse(BaseModel):
    agent_role: Literal['merchant'] = 'merchant'
    approval_id: str
    session_id: str
    action: Literal['change_dish_status'] = 'change_dish_status'
    status: ApprovalStatus
    summary: str
    target: ApprovalTargetInfo
    before_state: ApprovalStateSnapshot
    after_state: ApprovalStateSnapshot
    reason: str | None = None
    created_at: str
    expires_at: str
    decided_at: str | None = None
    decision_reason: str | None = None
    execution_message: str | None = None


class ReportReviewIssue(BaseModel):
    code: Literal['status_inference', 'ranking_scope', 'unsupported_cause', 'overstatement', 'other']
    quote: str
    reason: str
    suggestion: str
    evidence_ids: list[str]


class ReportReviewRef(BaseModel):
    review_id: str | None = None
    status: ReportReviewStatus
    failure: ReportReviewFailure | None = None
    poll_after_ms: int | None = None


class ReportReviewStatusResponse(BaseModel):
    agent_role: Literal['merchant'] = 'merchant'
    session_id: str
    review_id: str
    status: ReportReviewStatus
    issues: list[ReportReviewIssue] = Field(default_factory=list)
    failure: ReportReviewFailure | None = None
    evidence_ids: list[str] = Field(default_factory=list)


class AgentChatResponse(BaseModel):
    agent_role: Literal['merchant', 'customer'] = 'merchant'
    session_id: str
    answer: str
    tool_calls: list[str] = Field(default_factory=list)
    approval_required: bool = False
    approval_id: str | None = None
    approval: ApprovalTicketResponse | None = None
    report_review: ReportReviewRef | None = None
