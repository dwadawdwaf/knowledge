"""领域层：不依赖框架与第三方 SDK。"""

from knowledge.domain.entities import (
    Actor,
    Citation,
    Evidence,
    GeneratedAnswer,
    GeneratedSection,
    Locator,
    ParsedBlock,
    SearchHit,
)
from knowledge.domain.errors import (
    AuthenticationFailed,
    CitationValidationFailed,
    DomainError,
    FileTooLarge,
    ModelTimeout,
    ModelUnavailable,
    NotFound,
    SourceChanged,
    StaleJob,
    UnsupportedFormat,
    ValidationFailed,
    VersionConflict,
)

__all__ = [
    "Actor",
    "AuthenticationFailed",
    "Citation",
    "CitationValidationFailed",
    "DomainError",
    "Evidence",
    "FileTooLarge",
    "GeneratedAnswer",
    "GeneratedSection",
    "Locator",
    "ModelTimeout",
    "ModelUnavailable",
    "NotFound",
    "ParsedBlock",
    "SearchHit",
    "SourceChanged",
    "StaleJob",
    "UnsupportedFormat",
    "ValidationFailed",
    "VersionConflict",
]
