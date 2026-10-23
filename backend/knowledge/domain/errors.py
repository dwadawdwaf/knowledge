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
