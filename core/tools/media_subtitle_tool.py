from __future__ import annotations

import uuid
from dataclasses import field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from astrbot.api import logger
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext
from pydantic.dataclasses import dataclass

from ..transcriber.transcriber_model import TranscriptSegment

if TYPE_CHECKING:
    from ...main import VideoSummaryPlugin


def format_time(seconds: float) -> str:
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def segments_to_text(segments: list[TranscriptSegment]) -> str:
    return "\n".join(
        f"{format_time(seg.start)} - {seg.text.strip()}" for seg in segments
    )


async def _extract_transcript(
    plugin: "VideoSummaryPlugin", url: str, deadline: float | None = None
) -> tuple[str, str, str]:
    """解析 URL → 下载音频 → bcut 转写，返回 (title, tags, subtitle_text)。"""
    if not isinstance(url, str) or not url.startswith("http"):
        raise ValueError("URL 无效，请提供 http/https 链接")

    runtime = plugin._create_processing_runtime()
    cleanup_targets: list[Path] = []
    used_direct_fallback = False
    direct_fallback_completed = False

    try:
        parser_inst, keyword, searched, used_direct_fallback = (
            await plugin._resolve_url_with_direct_fallback(
                url,
                parser_patterns=runtime["parser_patterns"],
                direct_parser=runtime["direct_parser"],
            )
        )
        if not parser_inst:
            raise ValueError("未找到支持处理此链接的解析器")

        enable_cache = getattr(plugin.cfg, "enable_cache", True)
        url_hash = uuid.uuid5(uuid.NAMESPACE_URL, url).hex
        cache_dict = plugin._read_json_cache(url_hash)
        cache_url_match = cache_dict.get("url") == url or ("url" not in cache_dict)

        transcript: dict | None = None
        title = "未知视频"
        tags = "通用视频"
        source = "未知来源"
        if deadline is None:
            deadline = plugin._new_processing_deadline()

        if enable_cache and cache_url_match:
            cached_trs = cache_dict.get("transcript")
            if cached_trs:
                logger.info(f"LLM 工具命中字幕缓存: {url}")
                transcript = {"segments": cached_trs}
                title = str(cache_dict.get("title") or "缓存视频")
                tags = str(cache_dict.get("tags") or "通用视频")
                source = str(cache_dict.get("source") or "未知来源")
                if used_direct_fallback:
                    direct_fallback_completed = True

        if not transcript:
            try:
                parse_result = await plugin._wait_for_processing(
                    plugin._parse_result_with_parser(
                        parser_inst=parser_inst,
                        url=url,
                        keyword=keyword,
                        searched=searched,
                    ),
                    deadline,
                )
            except Exception:
                if used_direct_fallback:
                    raise ValueError("未找到支持处理此链接的解析器")
                raise

            if not parse_result.video_contents and not parse_result.audio_contents:
                raise ValueError("未解析到可供提取的音频/视频对象")

            audio_path, cleanup_targets = await plugin._materialize_audio(
                parse_result, deadline=deadline
            )
            transcript_res = await plugin._transcribe_audio(audio_path, deadline)

            if not transcript_res or not transcript_res.segments:
                raise ValueError("无法获取视频转写内容")

            transcript = {"segments": transcript_res.segments}
            title = parse_result.title or "未知视频"
            source = getattr(
                getattr(parser_inst, "platform", None),
                "display_name",
                "未知来源",
            )
            tags = "通用视频"
            if parse_result.extra and "tags" in parse_result.extra:
                tags = str(parse_result.extra["tags"])
            if used_direct_fallback:
                direct_fallback_completed = True

            if enable_cache:
                await plugin._write_json_cache(
                    url_hash,
                    {
                        "transcript": [
                            {"start": seg.start, "end": seg.end, "text": seg.text}
                            for seg in transcript["segments"]
                        ],
                        "title": title,
                        "tags": tags,
                        "source": source,
                    },
                    url=url,
                )

        segment_list: list[TranscriptSegment] = []
        for seg in transcript["segments"]:
            if isinstance(seg, dict):
                segment_list.append(TranscriptSegment(**seg))
            else:
                segment_list.append(seg)

        subtitle_text = segments_to_text(segment_list)
        if not subtitle_text.strip():
            raise ValueError("转写结果为空")

        return title, tags, subtitle_text

    except ValueError:
        raise
    except Exception as e:
        logger.error(f"LLM 工具字幕提取异常: {e}", exc_info=True)
        if used_direct_fallback and not direct_fallback_completed:
            raise ValueError("未找到支持处理此链接的解析器")
        raise ValueError(f"字幕提取失败: {str(e)}") from e
    finally:
        plugin._cleanup_temp_files(cleanup_targets)
        await plugin._close_processing_runtime(runtime)


async def run_media_subtitle_tool(plugin: "VideoSummaryPlugin", url: str, umo: str = "") -> str:
    """提取字幕文本，返回含时间戳的原始字幕。"""
    logger.info(f"LLM 工具 summary_extract_media_subtitle 被调用，URL: {url}")
    if not plugin._check_access(umo):
        return "❌ 当前会话无权使用字幕提取功能（不在白名单中或在黑名单中）"
    try:
        title, tags, subtitle_text = await _extract_transcript(plugin, url)
        result_preview = subtitle_text[:70].replace("\n", " ")
        logger.info(
            f"LLM 工具 summary_extract_media_subtitle 成功: {title}，"
            f"共计 {len(subtitle_text)} 字符，预览: {result_preview}..."
        )
        return subtitle_text
    except ValueError as e:
        logger.warning(f"LLM 工具 summary_extract_media_subtitle 失败: {e}")
        return f"❌ {e}"


async def run_media_summary_tool(
    plugin: "VideoSummaryPlugin", url: str, umo: str = "", event: Any = None
) -> str:
    """提取字幕并交由 AI 总结后返回精炼摘要。"""
    logger.info(f"LLM 工具 summary_extract_media_summary 被调用，URL: {url}")
    if not plugin._check_access(umo):
        return "❌ 当前会话无权使用媒体总结功能（不在白名单中或在黑名单中）"
    try:
        deadline = plugin._new_processing_deadline()
        title, tags, subtitle_text = await _extract_transcript(
            plugin, url, deadline=deadline
        )
        summary, _token_usage = await plugin._call_llm_for_summary(
            title,
            tags,
            subtitle_text,
            event=event,
            timeout=plugin._remaining_processing_timeout(deadline),
        )
        if getattr(plugin.cfg, "enable_cache", True):
            url_hash = uuid.uuid5(uuid.NAMESPACE_URL, url).hex
            await plugin._write_json_cache(url_hash, "summary", summary, url=url)
        result_preview = summary[:70].replace("\n", " ")
        logger.info(
            f"LLM 工具 summary_extract_media_summary 成功: {title}，"
            f"共计 {len(summary)} 字符，预览: {result_preview}..."
        )
        return summary
    except (ValueError, RuntimeError, TimeoutError) as e:
        logger.warning(f"LLM 工具 summary_extract_media_summary 失败: {e}")
        return f"❌ {e}"


@dataclass
class MediaSummaryTool(FunctionTool[AstrAgentContext]):
    """提取视频/音频链接中的语音内容，通过 AI 分析后返回精炼的文字摘要。"""

    __pydantic_config__ = {"arbitrary_types_allowed": True}

    plugin: Any = None
    name: str = "summary_extract_media_summary"
    description: str = (
        "提取视频/音频链接中的语音内容，通过 AI 分析后返回精炼的文字摘要，供对话上下文参考。"
        "本工具仅依赖音频转写，无法感知视频画面、运镜、场景等视觉信息。"
    )
    parameters: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "视频或音频链接地址",
                },
            },
            "required": ["url"],
        }
    )

    async def call(
        self, context: ContextWrapper[AstrAgentContext], url: str
    ) -> ToolExecResult:
        event = getattr(context.context, "event", None)
        umo = getattr(event, "unified_msg_origin", "")
        return await run_media_summary_tool(self.plugin, url, umo, event=event)


@dataclass
class MediaSubtitleTool(FunctionTool[AstrAgentContext]):
    """仅提取视频/音频链接中的字幕文本（含时间戳），不进行 AI 总结。"""

    __pydantic_config__ = {"arbitrary_types_allowed": True}

    plugin: Any = None
    name: str = "summary_extract_media_subtitle"
    description: str = (
        "提取视频/音频链接中的字幕文本（含时间戳），不进行 AI 总结。"
        "适合需要逐字分析原始语音内容的场景。"
        "本工具仅依赖音频转写，无法感知视频画面、运镜、场景等视觉信息。"
    )
    parameters: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "视频或音频链接地址",
                },
            },
            "required": ["url"],
        }
    )

    async def call(
        self, context: ContextWrapper[AstrAgentContext], url: str
    ) -> ToolExecResult:
        event = getattr(context.context, "event", None)
        umo = getattr(event, "unified_msg_origin", "")
        return await run_media_subtitle_tool(self.plugin, url, umo)
