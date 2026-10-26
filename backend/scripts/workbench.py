"""本地页面入口：不签发新身份，不把令牌放进 URL 或持久文件。"""

from __future__ import annotations

import webbrowser

from scripts.local_runtime import LocalRuntime


def open_workbench(runtime: LocalRuntime, *, browser: bool = True) -> None:
    url = runtime.base_url + "/workbench/"
    runtime.say(f"[界面] 知页工作台：{url}")
    runtime.say("[连接] 在终端输入 /token 获取开发令牌，再粘贴到页面连接窗口；不写入浏览器存储。")
    if browser:
        try:
            if not webbrowser.open(url):
                runtime.say("[界面] 未能自动打开浏览器，请手动访问上方地址。")
        except OSError:
            runtime.say("[界面] 浏览器启动失败，请手动访问上方地址。")
