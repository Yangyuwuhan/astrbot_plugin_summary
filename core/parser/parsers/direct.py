from __future__ import annotations

import re
from typing import ClassVar

from ..config import PluginConfig
from ..data import Platform
from ..download import Downloader
from ..exception import ParseException
from .base import BaseParser


class DirectMediaParser(BaseParser):
    """通用直链媒体解析器。

    该解析器不参与常规关键词匹配链路，仅在主流程未命中其他解析器时作为回退使用。
    """

    platform: ClassVar[Platform] = Platform(name="direct", display_name="直链媒体")

    _AUDIO_EXTS: ClassVar[tuple[str, ...]] = (
        "mp3",
        "m4a",
        "aac",
        "flac",
        "wav",
        "ogg",
        "opus",
    )
    _VIDEO_EXTS: ClassVar[tuple[str, ...]] = (
        "mp4",
        "mkv",
        "webm",
        "mov",
        "avi",
        "m4v",
        "ts",
    )
    _DIRECT_MEDIA_RE: ClassVar[re.Pattern[str]] = re.compile(
        r"(?i)^https?://[^\s?#]+\.(?P<ext>mp3|m4a|aac|flac|wav|ogg|opus|mp4|mkv|webm|mov|avi|m4v|ts)(?:\?[^\s#]*)?(?:#[^\s]*)?$"
    )

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)

    @classmethod
    def match_direct_url(cls, url: str) -> re.Match[str] | None:
        return cls._DIRECT_MEDIA_RE.search(url or "")

    async def parse_direct_url(self, url: str):
        searched = self.match_direct_url(url)
        if not searched:
            raise ParseException("URL 不是可识别的直链音频/视频地址")

        ext = searched.group("ext").lower()
        if ext in self._AUDIO_EXTS:
            content = self.create_audio_content(url)
            media_type = "音频"
        elif ext in self._VIDEO_EXTS:
            content = self.create_video_content(url)
            media_type = "视频"
        else:
            raise ParseException("URL 后缀不是支持的音频/视频类型")

        filename = url.rsplit("/", 1)[-1].split("?")[0]
        return self.result(
            title=filename,
            text=f"直链{media_type}: {filename}",
            contents=[content],
            url=url,
        )