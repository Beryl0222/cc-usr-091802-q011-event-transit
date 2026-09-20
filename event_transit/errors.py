"""领域错误：所有拒绝原因都使用稳定的机器可读代码。"""


class TransitError(Exception):
    """业务规则被拒绝。"""

    code = "rejected"

    def __init__(self, message: str, code: str | None = None, details: dict | None = None):
        super().__init__(message)
        if code:
            self.code = code
        self.details = details or {}


class NotFound(TransitError):
    code = "not_found"


class Conflict(TransitError):
    """符合预期的冲突（重放、二次发车、容量不足等），调用方应正常处理而非崩溃。"""

    code = "conflict"


class DuplicateReplay(Conflict):
    """同一去重键再次出现：不是错误，但绝不允许产生第二次效果。"""

    code = "duplicate_replay"
