"""领域错误。每个错误自带 HTTP 语义，供 api 层映射；领域层不认识 HTTP。"""

from __future__ import annotations


class DomainError(Exception):
    code = "INTERNAL_ERROR"
    http_status = 500
    retryable = False

    def __init__(self, message: str = "", **details: object) -> None:
        super().__init__(message or self.code)
        self.message = message or self.code
        self.details = details


class AuthenticationFailed(DomainError):
    code = "UNAUTHENTICATED"
    http_status = 401


class NotFound(DomainError):
    """无权与不存在统一返回 404，不泄露资源是否存在。"""

    code = "RESOURCE_NOT_FOUND"
    http_status = 404


class ValidationFailed(DomainError):
    code = "VALIDATION_ERROR"
    http_status = 422


class UnsupportedFormat(DomainError):
    code = "UNSUPPORTED_FORMAT"
    http_status = 415


class PasswordProtected(DomainError):
    """加密文档属确定性错误：不重试，任务直接置 failed（design §5.3）。"""

    code = "PASSWORD_PROTECTED"
    http_status = 422


class ParseFailed(DomainError):
    code = "PARSE_FAILED"
    http_status = 422


class FileTooLarge(DomainError):
    code = "FILE_TOO_LARGE"
    http_status = 413


class VersionConflict(DomainError):
    code = "VERSION_CONFLICT"
    http_status = 409


class SourceChanged(DomainError):
    code = "SOURCE_CHANGED"
    http_status = 409
    retryable = True


class StaleJob(DomainError):
    code = "STALE_JOB"
    http_status = 409


class CitationValidationFailed(DomainError):
    code = "CITATION_VALIDATION_FAILED"
    http_status = 502


class ModelUnavailable(DomainError):
    code = "MODEL_UNAVAILABLE"
    http_status = 503
    retryable = True


class ModelTimeout(DomainError):
    code = "MODEL_TIMEOUT"
    http_status = 504
    retryable = True


class TransientError(DomainError):
    """未预期但可重试的失败，例如数据库瞬时错误。"""

    code = "INTERNAL_ERROR"
    http_status = 500
    retryable = True


class NotImplementedYet(DomainError):
    code = "NOT_IMPLEMENTED"
    http_status = 501


_JOB_MESSAGES = {
    "INTERNAL_ERROR": "任务处理失败",
    "UNSUPPORTED_FORMAT": "文件格式不受支持",
    "PASSWORD_PROTECTED": "文件已加密",
    "EMPTY_CONTENT": "未提取到正文",
    "PARSE_FAILED": "文件解析失败",
    "MODEL_UNAVAILABLE": "模型服务不可用",
    "MODEL_TIMEOUT": "模型服务超时",
    "STALE_JOB": "任务执行资格已失效",
    "UNKNOWN_KIND": "任务类型不受支持",
}

# 仅保留受控操作提示，不透传解析器、SDK或历史数据库中的任意异常正文。
_PARSE_MESSAGES = frozenset(
    {
        "MinerU服务未就绪，请先启动本机解析服务",
        "MinerU接口不匹配，需要4.1文件解析服务",
    }
)


def public_job_error(error: dict | None) -> dict | None:
    """仅返回白名单中的固定说明，历史持久化错误也需再次脱敏。"""
    if error is None:
        return None
    code = error.get("code")
    if not isinstance(code, str) or code not in _JOB_MESSAGES:
        code = "INTERNAL_ERROR"
    return {
        "code": code,
        "message": error["message"]
        if code == "PARSE_FAILED"
        and isinstance(error.get("message"), str)
        and error.get("message") in _PARSE_MESSAGES
        else _JOB_MESSAGES[code],
        "retryable": error.get("retryable") is True,
    }
