# -*- coding: utf-8 -*-
"""Sub2API Native 后端公共入口。

业务实现按 Web、注册编排、浏览器自动化、外部集成和邮箱渠道分包。
"""

__all__ = ["create_app", "main"]


def create_app():
    from .web.application import create_app as _create_app

    return _create_app()


def main(host: str = "127.0.0.1", port: int = 8787):
    from .web.application import serve

    serve(host=host, port=port)
