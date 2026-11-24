from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


@dataclass
class Approval:
    tool_name: str
    arguments: dict
    operator_id: int
    id: str = field(default_factory=lambda: f"ap_{uuid4().hex}")
    status: ApprovalStatus = ApprovalStatus.PENDING
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class ApprovalService:
    """开发阶段内存审批仓库；生产环境替换为 MySQL。"""

    def __init__(self) -> None:
        self._items: dict[str, Approval] = {}

    def create(self, tool_name: str, arguments: dict, operator_id: int) -> Approval:
        approval = Approval(tool_name, arguments, operator_id)
        self._items[approval.id] = approval
        return approval

    def get(self, approval_id: str) -> Approval | None:
        return self._items.get(approval_id)

