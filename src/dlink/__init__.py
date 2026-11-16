"""DLink —— 网盘分享链接 → 直链 → 多线程下载。

浏览器扩展负责在分享页取直链（能读到 HttpOnly cookie 和页面内的 jsToken），
本包负责下载、队列、续链与界面。
"""

__version__ = "0.2.9"
__all__ = ["__version__"]
