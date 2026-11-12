"""引擎与路径策略读的配置，以及它们的出厂默认值。

两个根目录列表故意为空：空列表拒绝任何文件，所以刚装好的插件在操作者指定目录之前既不能读也不能写。
那个默认值是安全姿态，不是缺值，因此它和一起读的那些数字放在这里。

从 ``QgsSettings`` 里读设置属于插件生命周期；本模块只负责把普通 mapping 变成这些值，所以它在
裸解释器里也能导入。

The settings the engine and the path policy read, and their product defaults.

The two root lists start empty on purpose: an empty list denies every file, so a
fresh install cannot read or write anything until an operator names a directory.
That default is the security posture, not a missing value, which is why it lives
here next to the numbers it is read with.

Reading the settings out of ``QgsSettings`` belongs to the plugin lifecycle; this
module only turns a plain mapping into the values, so it stays importable in a
bare interpreter.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

# 映射里可能出现的键名：两个根目录是路径，其余都是计数。
# The names a mapping may carry; the two roots are paths, the rest are counts.
_ROOT_KEYS = ("allowed_data_roots", "allowed_project_roots")


@dataclass(frozen=True)
class Settings:
    """一份解析好的配置；``allowed_*_roots=()`` 意味着不允许任何文件访问。

    One resolved configuration; ``allowed_*_roots=()`` means no file access.
    """

    allowed_data_roots: tuple[Path, ...] = ()
    allowed_project_roots: tuple[Path, ...] = ()
    max_active_background: int = 2
    max_pending: int = 32
    max_terminal_records: int = 200
    terminal_ttl_seconds: int = 1800
    max_snapshot_features: int = 10000

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> Settings:
        """从映射构建设置，每个已声明的默认值都保留。

        一个键可以缺席或为 ``None`` —— 这就是类 QSettings 存储对没人写过的键返回的东西 —— 两者都
        表示「没设过」，于是 dataclass 的默认值留下。计数可能以字符串形式到达，根目录列表可能是存储
        返回的单个值，所以两者都在这里统一转一次，而不是让每个调用方各转一遍。

        Build the settings from a mapping, keeping every declared default.

        A key may be absent or ``None`` -- that is what a QSettings-like store
        returns for something nobody wrote -- and both mean "not set", so the
        dataclass default stays.  A count may arrive as a string and a root list
        as the single value the store returns, which is why both are converted
        here, once, instead of in every caller.
        """

        set_values = {key: value for key, value in values.items() if value is not None}
        resolved: dict[str, Any] = {}
        for field in fields(cls):
            if field.name not in set_values:
                continue
            value = set_values[field.name]
            resolved[field.name] = (
                _roots(value) if field.name in _ROOT_KEYS else _count(field.name, value)
            )
        return cls(**resolved)


def _count(key: str, value: Any) -> int:
    """一个计数；不能用的时候宁可拒绝，也不悄悄夹到合法范围。

    小于 1 的计数是配错后静默失败的形状：``max_active_background=0`` 会让每次后台调用都在等一个
    永远不会空出来的槽，所以插件宁可加载失败，也不排一个永远不动的队。报错会点出键名，值解析不了时
    连值一起点出来。

    One count, refused rather than clamped when it cannot work.

    A count below one is a configuration mistake with a silent failure mode:
    ``max_active_background=0`` would leave every background call waiting for a
    slot that never frees, so the plugin refuses to load instead of queueing
    forever.  The error names the key and, for an unparseable value, the value too.
    """

    try:
        count = int(value)
    except (TypeError, ValueError):
        raise RuntimeError(f"{key} must be a whole number, not {value!r}") from None
    if count < 1:
        raise RuntimeError(f"{key} must be at least 1, not {count}")
    return count


def _roots(value: Any) -> tuple[Path, ...]:
    """单个根目录可能以一条路径的形式到达：QSettings 就是那样返回单个值的。

    One root may arrive as a single path: QSettings returns one value that way.
    """

    if not value:
        return ()
    if isinstance(value, (str, Path)):
        return (Path(value),)
    return tuple(Path(root) for root in value)
