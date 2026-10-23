"""令牌签发与校验。owner_id 只从这里产生。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from typing import Any

from knowledge.config import Settings, get_settings
from knowledge.domain.entities import Actor
from knowledge.domain.errors import AuthenticationFailed


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _sign(payload: bytes, secret: str) -> str:
    return _b64encode(hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest())


def issue_token(
    subject_id: str,
    tenant_id: str = "default",
    ttl_seconds: int | None = None,
    settings: Settings | None = None,
) -> str:
    resolved = settings or get_settings()
    if not subject_id.strip():
        raise ValueError("subject_id 不能为空")

    expires_at = int(time.time()) + (ttl_seconds or resolved.token_ttl_seconds)
    claims: dict[str, Any] = {
        "sub": subject_id,
        "tid": tenant_id,
        "exp": expires_at,
        "jti": uuid.uuid4().hex[:12],
    }
    payload = json.dumps(claims, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return f"{_b64encode(payload)}.{_sign(payload, resolved.token_secret)}"


def verify_token(token: str, settings: Settings | None = None) -> Actor:
    resolved = settings or get_settings()
    try:
        payload_b64, signature = token.split(".", 1)
    except ValueError as exc:
        raise AuthenticationFailed("令牌格式不正确") from exc

    payload = _b64decode(payload_b64)
    if not hmac.compare_digest(_sign(payload, resolved.token_secret), signature):
        raise AuthenticationFailed("令牌签名无效")

    try:
        claims = json.loads(payload.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise AuthenticationFailed("令牌内容不可解析") from exc

    if int(claims.get("exp", 0)) < int(time.time()):
        raise AuthenticationFailed("令牌已过期")

    subject_id = str(claims.get("sub") or "").strip()
    if not subject_id:
        raise AuthenticationFailed("令牌缺少主体标识")

    return Actor(
        subject_id=subject_id,
        tenant_id=str(claims.get("tid") or "default"),
        token_id=str(claims.get("jti") or ""),
    )


def bearer_actor(authorization: str | None, settings: Settings | None = None) -> Actor:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise AuthenticationFailed("缺少访问令牌")
    return verify_token(authorization.split(" ", 1)[1].strip(), settings)


def main(argv: list[str] | None = None) -> int:
    """命令行签发开发令牌：python -m knowledge.security issue --subject dev"""
    import argparse

    parser = argparse.ArgumentParser(description="签发开发用访问令牌")
    sub = parser.add_subparsers(dest="command", required=True)
    issue = sub.add_parser("issue", help="签发令牌")
    issue.add_argument("--subject", required=True)
    issue.add_argument("--tenant", default="default")
    issue.add_argument("--ttl", type=int, default=None)

    args = parser.parse_args(argv)
    if args.command == "issue":
        print(issue_token(args.subject, args.tenant, args.ttl))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
