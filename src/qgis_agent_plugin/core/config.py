"""引擎读的配置，以及它们的出厂默认值。

五项都是计数：并发、排队、记录保留和一次后台快照的要素上限。没有路径策略的配置项——路径只要求是
绝对路径，就在参数校验处判断。

从 ``QgsSettings`` 里读设置属于插件生命周期；本模块只负责把普通 mapping 变成这些值，所以它在
裸解释器里也能导入。

The settings the engine reads, and their product defaults.

Five counts: concurrency, queueing, record retention and the feature cap of one
background snapshot.  There is no path-policy setting: a path only has to be
absolute, which is decided where arguments are validated.

Reading the settings out of ``QgsSettings`` belongs to the plugin lifecycle; this
module only turns a plain mapping into the values, so it stays importable in a
bare interpreter.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields
from typing import Any


@dataclass(frozen=True)
class Settings:
    """一份解析好的配置：五个计数。

    One resolved configuration: five counts.
    """

    max_active_background: int = 2
    max_pending: int = 32
    max_terminal_records: int = 200
    terminal_ttl_seconds: int = 1800
    max_snapshot_features: int = 10000

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> Settings:
        """从映射构建设置，每个已声明的默认值都保留。

        一个键可以缺席或为 ``None`` —— 这就是类 QSettings 存储对没人写过的键返回的东西 —— 两者都
        表示「没设过」，于是 dataclass 的默认值留下。计数可能是存储返回的字符串，所以在这里统一转
        一次，而不是让每个调用方各转一遍。

        Build the settings from a mapping, keeping every declared default.

        A key may be absent or ``None`` -- that is what a QSettings-like store
        returns for something nobody wrote -- and both mean "not set", so the
        dataclass default stays.  A count may arrive as a string, which is why it
        is converted here, once, instead of in every caller.
        """

        set_values = {key: value for key, value in values.items() if value is not None}
        return cls(
            **{
                field.name: _count(field.name, set_values[field.name])
                for field in fields(cls)
                if field.name in set_values
            }
        )


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
