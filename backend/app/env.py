"""读 .env，省得每次开终端都手工 export 一遍。

只做最小的一件事：把 `KEY=VALUE` 填进 `os.environ`。

三条规矩：

1. **已经存在的环境变量不覆盖。** 命令行里临时设的值优先级更高 ——
   `.env` 里放一份常用的，想临时换 key 就在终端里设，不用改文件。
2. **按文件粒度看顺序。** `backend/.env` 比仓库根目录的 `.env` 更具体，
   先读它；两个文件都有同一个键时，后读的不覆盖先读的。
3. **只认三种行。** 空行、`#` 注释、`KEY=VALUE`。多行值、变量展开、
   行尾注释都不支持 —— 这个项目用不上，实现了只会让人猜哪些语法有效。

找不到文件就什么都不做，不报错 —— `.env` 是可选的。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

_APP_DIR = Path(__file__).resolve().parent

#: 查找顺序，先找到的键优先（不覆盖已有值）
_CANDIDATES = (
    _APP_DIR.parent / ".env",      # backend/.env
    _APP_DIR.parents[1] / ".env",  # 仓库根目录/.env
)


def load_env() -> list[str]:
    """把 .env 里的键补进环境变量。返回这次真正填进去的键名。"""
    added: list[str] = []

    for path in _CANDIDATES:
        if not path.is_file():
            continue
        for key, value in _parse(path):
            if key in os.environ:
                continue
            os.environ[key] = value
            added.append(key)

    return added


def _parse(path: Path) -> Iterator[tuple[str, str]]:
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()

        key, sep, value = line.partition("=")
        if not sep:
            continue

        key = key.strip()
        if not key:
            continue

        # 引号只是包着值，取完就扔掉；不带引号的值原样保留
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]

        yield key, value
