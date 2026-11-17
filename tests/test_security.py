"""路径只要求是绝对路径；其余交给 QGIS 与 provider。

这个文件守两件都不需要 QGIS 的事：``absolute_path`` 的判据，以及 ``Settings`` 从
mapping 里取计数的规则。

A path only has to be absolute; QGIS and the provider decide the rest.

Two things that need no QGIS are guarded here: what ``absolute_path`` accepts, and how
``Settings`` takes counts out of a mapping.
"""

from __future__ import annotations

import pytest  # pyright: ignore[reportMissingImports]

from qgis_agent_plugin.core.config import Settings
from qgis_agent_plugin.core.models import PATH_NOT_ALLOWED, FrameworkError
from qgis_agent_plugin.core.security import absolute_path


def refused(raw: str) -> FrameworkError:
    with pytest.raises(FrameworkError) as caught:
        absolute_path(raw)
    assert caught.value.code == PATH_NOT_ALLOWED
    return caught.value


@pytest.mark.parametrize(
    "raw",
    [
        r"C:/data/roads.gpkg",
        r"C:\data\roads.gpkg",
        r"\\host\share\roads.gpkg",
        "/vsicurl/https://example.com/a.tif",
        "/vsizip/C:/data/archive.zip/roads.shp",
    ],
)
def test_an_absolute_path_in_either_spelling_is_accepted(raw: str) -> None:
    # UNC 与 /vsi 都是 provider 打得开的绝对数据源 / a UNC share and a /vsi path are both
    # absolute data sources a provider can open
    assert str(absolute_path(raw))


@pytest.mark.parametrize("raw", ["", "roads.gpkg", "C:roads.gpkg", r"\roads.gpkg", "a\0b"])
def test_an_empty_null_or_relative_path_is_refused(raw: str) -> None:
    refused(raw)


def test_a_path_that_does_not_exist_is_accepted() -> None:
    # 存在与否由 ``read()`` 与 provider 回答；预检不碰文件系统
    # existence is what ``read()`` and the provider answer; the pre-check touches no
    # file system
    assert absolute_path(r"C:/data/does-not-exist.gpkg")


def test_unset_values_fall_back_to_the_defaults() -> None:
    assert Settings.from_mapping({"max_pending": None}) == Settings()


def test_a_key_this_build_does_not_declare_is_ignored() -> None:
    # 旧配置里留下的键不该让加载失败 / a key left over from an older config must not fail loading
    assert Settings.from_mapping({"allowed_data_roots": "C:/data"}) == Settings()


def test_settings_take_counts_as_strings_and_keep_the_other_defaults() -> None:
    settings = Settings.from_mapping({"max_pending": "8"})
    assert settings.max_pending == 8
    assert settings.max_active_background == 2


COUNT_KEYS = (
    "max_active_background",
    "max_pending",
    "max_terminal_records",
    "terminal_ttl_seconds",
)


@pytest.mark.parametrize("key", COUNT_KEYS)
@pytest.mark.parametrize("value", [0, -1, "0", "-3"])
def test_a_count_below_one_is_refused_and_names_the_key(key: str, value: object) -> None:
    # 计数为 0 是静默失败：后台名额永不释放，于是每个后台调用都等不到
    # a count of 0 fails silently, because no background slot would ever free, leaving every
    # call waiting for one
    with pytest.raises(RuntimeError) as caught:
        Settings.from_mapping({key: value})
    assert key in str(caught.value)


@pytest.mark.parametrize("key", COUNT_KEYS)
def test_a_count_that_is_not_a_number_names_the_key_and_the_value(key: str) -> None:
    with pytest.raises(RuntimeError) as caught:
        Settings.from_mapping({key: "32 (default)"})
    assert key in str(caught.value)
    assert "32 (default)" in str(caught.value)


def test_a_count_of_one_is_accepted() -> None:
    # 下界是闭区间：只有一个后台名额也是一种可用配置
    # the lower bound is inclusive: one background slot is a usable configuration too
    settings = Settings.from_mapping({"max_active_background": "1"})
    assert settings.max_active_background == 1
