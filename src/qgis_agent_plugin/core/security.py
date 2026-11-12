"""操作可以读写哪些本地路径。

只有一个入口：一条路径要么规范化成操作者允许的某个根目录下的普通文件，要么被拒绝。根目录在解析之后
按路径分量逐个比较，所以 ``..`` 和符号链接都爬不出允许的目录；错误消息从不回显原始路径，因为一行
错误信息可能落进日志或 agent 的上下文。

根目录列表为空——也就是默认值——就是全部拒绝：在操作者指定目录之前，不存在被允许的目录。

Which local paths an operation may read or write.

There is one entry point, and a path either normalises to a regular file inside
one of the operator's allowed roots or it is refused.  Roots are compared path
component by path component after resolution, so ``..`` and a symbolic link
cannot climb out of an allowed directory; the message never repeats the raw path,
because an error line may end up in a log or an agent's context.

An empty root list -- the default -- refuses everything: no directory is allowed
until an operator names one.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from typing import NoReturn

from .models import PATH_NOT_ALLOWED, FrameworkError

# URI scheme 至少两个字符且以冒号结尾；单个字母加冒号是 Windows 盘符，那是路径而不是 scheme。
# A URI scheme is at least two characters and ends in a colon; a single letter followed by a
# colon is a Windows drive, which is a path, not a scheme.
_URI = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]+:")
# GDAL 的进程内虚拟文件系统在这一层不算本地文件。
# GDAL's in-process virtual file systems are not local files at this layer.
_VSI = "/vsi"
# UNC 路径是网络 I/O 而非本地文件：两个前导分隔符指向一台主机与一个共享，
# ``\\?\`` 前缀是同一件事的长路径写法。``Path.resolve`` 会在允许列表被查之前就访问
# 那台主机。
# A UNC path is network I/O, not a local file: two leading separators name a host and a share,
# and the ``\\?\`` prefixes are the long-path spelling of the same.  ``Path.resolve`` would
# reach that host before the allow-list is ever consulted.
_UNC = ("\\\\", "//", "\\\\?\\", "//?/")


def is_remote_uri(raw: str) -> bool:
    """``raw`` 指的是网络 I/O 或虚拟文件系统、而不是本地文件吗。

    公开的，因为路径策略对每条路径只回答同一个问题：URL、provider URI、GDAL 的 ``/vsi`` 路径和
    UNC 路径都是只有在允许列表点头之后才会发生的 I/O。

    Whether ``raw`` names network I/O or a virtual file system, not a local file.

    Public because the path policy answers one question for every path: a URL, a
    provider URI, a GDAL ``/vsi`` path and a UNC path are all I/O this plugin only
    performs after the allow-list has said yes.
    """

    return raw.lower().startswith(_VSI) or raw.startswith(_UNC) or bool(_URI.match(raw))


def resolve_path(
    raw: str,
    roots: Sequence[str | Path],
    *,
    must_exist: bool,
    allow_directory: bool = False,
) -> Path:
    """返回规范化后的路径，否则抛 ``PATH_NOT_ALLOWED``。

    ``must_exist=True`` 是读：目标必须是已存在的普通文件。``must_exist=False`` 是写：目标可以不
    存在，但父目录必须存在且位于同一组根目录内，是否覆盖由操作自己决定（``TARGET_EXISTS`` 不归
    本模块管）。

    ``allow_directory=True`` 把写的形状换成数据源需要的那种：目标可以是目录，父目录也不必存在
    ——目录被搬走之后，数据源仍然是根目录内的数据源——所以只把解析后的路径与根目录比较。它对读
    没有影响：读永远是普通文件。

    Return the normalised path, or raise ``PATH_NOT_ALLOWED``.

    ``must_exist=True`` is a read: the target has to be an existing regular file.
    ``must_exist=False`` is a save: the target may be absent, but its parent must
    exist and be inside the same roots, and the overwrite decision stays with the
    operation (``TARGET_EXISTS`` is not this module's concern).

    ``allow_directory=True`` turns the save shape into the one a data source needs:
    the target may be a directory and its parent does not have to exist -- a source
    whose directory was moved away is still a source inside the roots -- so only
    the resolved path is compared with the roots.  It says nothing about a read; a
    read is always a regular file.
    """

    _refuse_remote(raw)
    path = Path(raw)
    if not raw or "\0" in raw or not path.is_absolute():
        _refuse()
    # 相对根目录属于配错：它会让策略取决于当前工作目录，所以什么都不允许。
    # A relative root is a configuration mistake: it would make the policy depend on the
    # working directory, so it allows nothing.
    allowed = tuple(Path(root).resolve() for root in roots if Path(root).is_absolute())

    if must_exist:
        target = _resolve_existing(path)
        if not target.is_file():
            _refuse()
    elif allow_directory:
        # 故意不用 strict：不存在的东西不可能跑到根目录外面，而 ``resolve()`` 仍会跟进
        # 存在的链接。不可读的路径直接拒绝，而不是像读那样报成引擎失败。
        # Non-strict on purpose: what does not exist cannot be outside the roots, and
        # ``resolve()`` still follows the links that do.  An unreadable path is refused rather
        # than reported as an engine failure, as a read would be.
        try:
            target = path.resolve()
        except OSError:
            _refuse()
    else:
        parent = _resolve_existing(path.parent)
        if not parent.is_dir():
            _refuse()
        # resolve() 也会规范结尾的 ``.``/``..``，否则它看起来在根目录内，实际指的却是父目录。
        # resolve() also normalises a trailing ``.``/``..``, which would otherwise look like it
        # lived inside the root while naming its parent.
        target = (parent / path.name).resolve()
        if target.is_dir():
            _refuse()

    if not any(target == root or root in target.parents for root in allowed):
        _refuse()
    return target


def _resolve_existing(path: Path) -> Path:
    """解析 ``path`` 时会跟进链接；目标不存在或不可读就拒绝。

    Resolve ``path`` through links; a missing or unreadable target is refused.
    """

    try:
        return path.resolve(strict=True)
    except OSError:
        _refuse()


def _refuse_remote(raw: str) -> None:
    """在任何 I/O 之前拒绝 URL、provider scheme 与虚拟文件系统。

    Refuse URLs, provider schemes and virtual file systems before any I/O.
    """

    if is_remote_uri(raw):
        _refuse()


def _refuse() -> NoReturn:
    # 原始路径不进消息：provider URI 里可能带凭据，而调用方本来就知道自己传了什么。
    # The raw path stays out of the message: it may carry credentials in a provider URI, and
    # the caller already knows what it asked for.
    raise FrameworkError(PATH_NOT_ALLOWED, "path is not an allowed local file")
