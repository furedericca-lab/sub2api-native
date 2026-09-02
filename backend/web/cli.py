#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""管理控制台命令行入口。

运行示例：``python -m backend.web.cli --host 0.0.0.0 --port 8787``。
"""
from __future__ import annotations

from backend.web.application import main

if __name__ == "__main__":
    main()
