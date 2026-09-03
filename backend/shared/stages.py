"""注册任务阶段标签。

浏览器运行时（backend.automation.session）在拉起 Camoufox 之前打一行心跳日志，
Web 协调器（backend.web.jobs）按同一批字符串识别阶段，使快照能离开
"任务启动中"。两侧只依赖本模块，避免 Web 层为了一个常量导入 Camoufox。
"""

# 浏览器启动阶段标签。Camoufox 的 mmdb 缓存未命中时，__enter__() 会先同步下载
# MaxMind GeoLite2（IPv4 约 28 MB + IPv6 约 17 MB），慢速出口下可达数分钟。
STAGE_BROWSER_LAUNCHING = "浏览器启动中"

# 心跳日志行前缀。协调器以此判定阶段变更，改动需同步 backend/tests。
LOG_BROWSER_LAUNCHING_PREFIX = f"[*] {STAGE_BROWSER_LAUNCHING}："

__all__ = ["STAGE_BROWSER_LAUNCHING", "LOG_BROWSER_LAUNCHING_PREFIX"]
