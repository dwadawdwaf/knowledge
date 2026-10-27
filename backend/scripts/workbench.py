"""本地页面入口：用本次运行的令牌自动连接，不签发新身份。

令牌放在 URL 片段里打开：片段不进入请求行、不写服务端日志，页面读取后立即从地址栏清除；
令牌密钥每次运行随机生成，因此历史记录里的旧令牌对下一次运行无效。
"""

from __future__ import annotations

import webbrowser

from scripts.local_runtime import LocalRuntime


def open_workbench(runtime: LocalRuntime, *, browser: bool = True) -> None:
    url = runtime.workbench_url()
    runtime.say(f"[界面] 知页工作台：{runtime.base_url}/workbench/（已带上本次令牌，无需手工填写）")
    if browser:
        try:
            if not webbrowser.open(url):
                runtime.say("[界面] 未能自动打开浏览器，请手动访问上方地址并在终端输入 /token 粘贴令牌。")
        except OSError:
            runtime.say("[界面] 浏览器启动失败，请手动访问上方地址。")
