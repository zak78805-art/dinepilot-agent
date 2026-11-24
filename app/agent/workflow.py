from enum import StrEnum


class RiskLevel(StrEnum):
    READ = "read"
    ADVICE = "advice"
    WRITE = "write"


def requires_approval(risk_level: RiskLevel) -> bool:
    """只有会修改真实业务数据的操作必须审批。"""
    return risk_level == RiskLevel.WRITE

