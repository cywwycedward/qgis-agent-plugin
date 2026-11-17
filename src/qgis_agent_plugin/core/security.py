"""操作接受哪种路径：绝对路径。

一个入口，一个问题：``raw`` 是不是一条绝对路径。不是就拒，是就原样返回一个 ``Path``。这一层不做
I/O，不问文件是否存在、在不在某个目录里、指向本地还是远程——那些是 QGIS 与 provider 自己的判断，
而它们的答案比预检更新。

留下这一条的理由不是访问控制，是可复现：相对路径由 QGIS 按进程工作目录解析，同一个调用换个目录就
有不同结果，所以它在参数校验处就被挡掉，而不是在下游变成一个说不清的结果。

Which paths an operation accepts: absolute ones.

One entry point, one question: is ``raw`` an absolute path?  If not it is refused; if it is, a
``Path`` comes back unchanged.  This layer does no I/O and asks nothing about existence,
containment or locality -- those are QGIS's and the provider's judgments, and their answer is
fresher than a pre-check.

The one rule that stays is not access control but reproducibility: a relative path is resolved
by QGIS against the process working directory, so the same call would mean different things
from different directories.  It is refused where arguments are validated, rather than becoming
an unexplainable result downstream.
"""

from __future__ import annotations

from pathlib import Path

from .models import PATH_NOT_ALLOWED, FrameworkError

__all__ = ["absolute_path"]

# GDAL 把远程文件写成 `/vsi…` 的绝对字形：它不是 Windows 的绝对路径，却是 provider 打得开的
# 绝对数据源，所以按绝对路径收下。
# GDAL spells a remote file as `/vsi…`, an absolute spelling that is not a Windows absolute path
# but is an absolute data source a provider can open, so it is accepted as one.
_VSI = "/vsi"


def absolute_path(raw: str) -> Path:
    """``raw`` 作为 ``Path`` 返回；不是绝对路径就抛 ``PATH_NOT_ALLOWED``。

    空字符串、含 NUL、相对路径都被拒；其余原样返回，不解析、不推断、不碰文件系统。

    Return ``raw`` as a ``Path``, or raise ``PATH_NOT_ALLOWED``.

    An empty string, a NUL byte or a relative path is refused; everything else comes back
    unchanged: nothing is resolved, guessed at, or touched on disk.
    """

    absolute = raw.lower().startswith(_VSI) or Path(raw).is_absolute()
    if not raw or "\0" in raw or not absolute:
        raise FrameworkError(PATH_NOT_ALLOWED, "the path must be an absolute path")
    return Path(raw)
