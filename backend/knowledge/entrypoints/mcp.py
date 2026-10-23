"""MCP 进程入口（M2 接入协议实现）。

当前只固定工具面与启动约定：工具定义见 knowledge.mcp.tools.TOOLS。
协议实现（stdio / SSE）落地后在此注册，业务层无需改动。
"""

from __future__ import annotations

from knowledge.mcp.tools import TOOLS


def main() -> None:
    names = ", ".join(TOOLS)
    raise SystemExit(
        f"MCP 协议实现待接入（M2）。已定义工具：{names}\n"
        "在此之前请通过 REST 接口调用同一套 services。"
    )


if __name__ == "__main__":  # pragma: no cover
    main()
