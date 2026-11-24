from collections import defaultdict, deque


class SessionMemory:
    """开发阶段短期记忆；生产环境替换为 Redis。"""

    def __init__(self, max_messages: int = 20) -> None:
        self._messages: dict[str, deque[dict[str, str]]] = defaultdict(
            lambda: deque(maxlen=max_messages)
        )

    def add(self, session_id: str, role: str, content: str) -> None:
        self._messages[session_id].append({"role": role, "content": content})

    def get(self, session_id: str) -> list[dict[str, str]]:
        return list(self._messages[session_id])

