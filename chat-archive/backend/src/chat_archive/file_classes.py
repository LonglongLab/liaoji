"""File-manager style classes for file messages (PSU-2.66).

WeChat's file manager sorts attachments into 文稿 / 表格 / 幻灯片 / 压缩文件 /
音频 / 应用程序. The archive stores a message type (`file`) and the attachment's
own name, so the class has to be derived from the extension.

**This table lives here and nowhere else.** The browser never needs it: it sends
a class id and renders a label, and the icon on each row comes from the
frontend's own `fileVisualOf`, which answers a different question (what mark to
print on the paper icon) from a different input (the extension itself). Two
tables would have been the thing to avoid; there is only one because the second
consumer turned out not to exist.

Extensions are ordered by what the real archive actually holds -- measured over
its 6,327 `kind='file'` attachments: xlsx 1556, docx 964, txt 690, zip 652,
pdf 566, doc 285, sql 150, md 142, xls 111, rar 107, pptx 92, json 92, java 58,
jar 53, exe 50, mp4 48, csv 45, xml 40, log 39, html 38, m4a 38, ipa 33, yml 22,
rp 19, xmind 15, wgt 14, pom 12, gif 11, plus 25 with no usable extension.
"""

from __future__ import annotations

from typing import Final, Mapping

#: 分类 → 归它的扩展名（全小写，不带点）。
FILE_CLASS_EXTENSIONS: Final[Mapping[str, tuple[str, ...]]] = {
    "doc": ("doc", "docx", "pdf", "txt", "md", "markdown", "rtf", "odt", "log", "wps"),
    "sheet": ("xls", "xlsx", "xlsm", "csv", "et", "ods"),
    "slide": ("ppt", "pptx", "pps", "ppsx", "dps", "odp"),
    "archive": ("zip", "rar", "7z", "tar", "gz", "bz2", "xz", "jar", "war"),
    "audio": ("mp3", "m4a", "wav", "flac", "aac", "ogg", "amr", "wma"),
    "app": ("exe", "msi", "apk", "ipa", "dmg", "pkg", "deb", "rpm", "wgt", "appx"),
}

#: 契约里的枚举值。`other` 是「以上都不是」，不是一列扩展名——归档里真实出现的
#: 就有 sql / java / json / xml / html / yml / rp / xmind / pom，一一列举既列不完
#: 也没必要，读的人要的只是「不属于上面那几类」。
FILE_CLASSES: Final[tuple[str, ...]] = (*FILE_CLASS_EXTENSIONS, "other")

_KNOWN_EXTENSIONS: Final[frozenset[str]] = frozenset(
    extension for group in FILE_CLASS_EXTENSIONS.values() for extension in group
)


def file_class_extensions(file_class: str) -> tuple[str, ...]:
    """归这一类的扩展名；`other` 返回空元组（它靠「不属于其余各类」定义）。"""
    return FILE_CLASS_EXTENSIONS.get(file_class, ())


def known_extensions() -> tuple[str, ...]:
    """所有被归过类的扩展名，`other` 的判定要用它取反。"""
    return tuple(sorted(_KNOWN_EXTENSIONS))
