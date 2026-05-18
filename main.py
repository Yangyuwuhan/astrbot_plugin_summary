import re
import time
import asyncio
import uuid
import json
import os
from pathlib import Path
from typing import Optional, Tuple, List, Any

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.core import AstrBotConfig

# 引用 复制过来的 bcut 和模型
from .core.transcriber.bcut import BcutTranscriber
from .core.transcriber.transcriber_model import TranscriptSegment

# 引用 parser 项目组件
from .core.parser.download import Downloader
from .core.parser.config import PluginConfig
from .core.parser.parsers.base import BaseParser


@register(
    "astrbot_plugin_summary",
    "YOUR_NAME",
    "支持视频内容解析与总结，针对任意平台提供专业总结分析。",
    "1.0",
)
class VideoSummaryPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.cfg = PluginConfig(config, context=context)
        self.downloader = Downloader(self.cfg)
        self.transcriber = BcutTranscriber()
        self._debug = bool(getattr(self.cfg, "debug_mode", False))

        # 确保 temp_dir 与 cache_dir 为 Path 且存在
        self._temp_dir = Path(getattr(self.cfg, "temp_dir", Path.cwd() / "tmp"))
        self._cache_dir = Path(getattr(self.cfg, "cache_dir", self._temp_dir / "cache"))
        try:
            self._temp_dir.mkdir(parents=True, exist_ok=True)
            self._cache_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.warning(f"无法创建临时/缓存目录: {e}")

        self._parser_patterns = self._build_parser_index()

    def _build_parser_index(self):
        """构建提取器索引"""
        patterns = []
        for parser_cls in BaseParser.get_all_subclass():
            parser_inst = parser_cls(self.cfg, self.downloader)
            # 聚合所有的配置里的白名单正则表达式
            # 这里简单直接聚合所有 parser 的 _key_patterns
            for keyword, pattern in getattr(parser_inst, "_key_patterns", []):
                patterns.append((keyword, pattern, parser_inst))
        return patterns

    def _get_json_cache_path(self, url_hash: str) -> Path:
        return self._cache_dir / f"{url_hash}.json"

    def _read_json_cache(self, url_hash: str) -> dict:
        cache_file = self._get_json_cache_path(url_hash)
        if cache_file.exists():
            try:
                with open(cache_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}

    def _write_json_cache(
        self, url_hash: str, key: str, value: Any, url: Optional[str] = None
    ):
        data = self._read_json_cache(url_hash)
        if url:
            data["url"] = url
        data[key] = value
        with open(self._get_json_cache_path(url_hash), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)

    async def _resolve_url(
        self, url: str
    ) -> Tuple[Optional[BaseParser], Optional[str], Optional[Any]]:
        for keyword, pattern, parser_inst in self._parser_patterns:
            if keyword in url:
                searched = pattern.search(url)
                if searched:
                    return parser_inst, keyword, searched
        return None, None, None

    async def _materialize_audio(self, parse_result) -> Tuple[Path, List[Path]]:
        """提取或转换第一份音频或视频素材得到 mp3 供 bcut 处理"""
        targets = []
        source_path = None

        # 将所有已下载的解析结果媒体及封面加入待清理列表
        for content in parse_result.contents:
            try:
                if hasattr(content, "get_path"):
                    targets.append(await content.get_path())
                if hasattr(content, "get_cover_path"):
                    c_path = await content.get_cover_path()
                    if c_path:
                        targets.append(c_path)
            except Exception:
                pass

        if parse_result.audio_contents:
            source_path = await parse_result.audio_contents[0].get_path()
        elif parse_result.video_contents:
            source_path = await parse_result.video_contents[0].get_path()

        if not source_path or not source_path.exists():
            raise FileNotFoundError("未成功拉取到媒体文件实体")
        targets.append(source_path)

        out_mp3 = self._temp_dir / f"{uuid.uuid4().hex}.mp3"
        targets.append(out_mp3)

        # 使用 ffmpeg 提取归一化音频
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            str(source_path),
            "-vn",
            "-acodec",
            "libmp3lame",
            "-ar",
            "16000",
            "-ac",
            "1",
            str(out_mp3),
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
        )
        await proc.communicate()

        if not out_mp3.exists():
            raise RuntimeError("ffmpeg 转换音频失败。")

        return out_mp3, targets

    @filter.command("总结")
    async def summarize_video(self, event: AstrMessageEvent, url: str):
        """总结任意视频链接: /总结 <URL>"""
        async for result in self._summarize_video_impl(event, url, force_refresh=False):
            yield result

    @filter.command("强制总结")
    async def force_summarize_video(self, event: AstrMessageEvent, url: str):
        """强制重新总结任意视频链接: /强制总结 <URL>"""
        async for result in self._summarize_video_impl(event, url, force_refresh=True):
            yield result

    async def _summarize_video_impl(
        self, event: AstrMessageEvent, url: str, force_refresh: bool = False
    ):
        """统一总结主流程。force_refresh=True 时优先复用本地字幕缓存并强制重跑 LLM。"""
        if not url.startswith("http"):
            yield event.plain_result("❌ 请输入有效的URL链接")
            return

        parser_inst, keyword, searched = await self._resolve_url(url)
        if not parser_inst:
            yield event.plain_result("❌ 未找到支持处理此链接的解析器")
            return

        enable_cache = getattr(self.cfg, "enable_cache", True)

        url_hash = uuid.uuid5(uuid.NAMESPACE_URL, url).hex
        cache_dict = self._read_json_cache(url_hash)
        cache_url_match = cache_dict.get("url") == url or ("url" not in cache_dict)

        # 1. 普通总结命令在命中总结缓存时直接返回
        if enable_cache and (not force_refresh) and cache_url_match:
            cached_sum = cache_dict.get("summary")
            if cached_sum:
                if getattr(self.cfg, "show_token_usage", False):
                    cached_sum += (
                        "\n━━━━━━━━━━━━━━\n输入: 0 tokens\n输出: 0 tokens\n耗时: 0.00 s"
                    )
                yield event.plain_result(f"📌 视频总结（命中缓存）\n\n{cached_sum}")
                return

        yield event.plain_result(
            "⏳ 正在拉取素材与转写字幕（这可能需要一段较长的时间）..."
        )

        cleanup_targets = []
        transcript = None
        title = "未知视频"
        tags = "通用视频"
        try:
            # 2. 命中字幕缓存时可跳过下载与转写
            if enable_cache and cache_url_match:
                cached_trs = cache_dict.get("transcript")
                if cached_trs:
                    transcript = {"segments": cached_trs}
                    title = str(cache_dict.get("title") or "缓存视频")
                    tags = str(cache_dict.get("tags") or "通用视频")
                    if force_refresh:
                        yield event.plain_result(
                            "⏳ 强制总结：命中本地字幕缓存，正在交由 AI 重新思考..."
                        )
                    else:
                        yield event.plain_result("⏳ 素材命中缓存，正在交由 AI 思考...")

            if not transcript:
                # 3. 借助 parser 项目解析与下载
                parse_result = await parser_inst.parse_with_redirect(url=url)

                if not parse_result.video_contents and not parse_result.audio_contents:
                    yield event.plain_result("❌ 未解析到可供总结的音频/视频对象")
                    return

                audio_path, cleanup_targets = await self._materialize_audio(
                    parse_result
                )

                # 4. 交给 bcut 转写
                transcript_res = await asyncio.to_thread(
                    self.transcriber.transcript, str(audio_path)
                )

                if not transcript_res or not transcript_res.segments:
                    yield event.plain_result("❌ 无法获取视频转写内容")
                    return
                transcript = {"segments": transcript_res.segments}
                title = parse_result.title or "未知视频"
                tags = "通用视频"
                if parse_result.extra and "tags" in parse_result.extra:
                    tags = str(parse_result.extra["tags"])

                # 开启缓存后，同时写入 url、字幕、标题、标签
                if enable_cache:
                    self._write_json_cache(
                        url_hash,
                        "transcript",
                        [
                            {"start": seg.start, "end": seg.end, "text": seg.text}
                            for seg in transcript["segments"]
                        ],
                        url=url,
                    )
                    self._write_json_cache(url_hash, "title", title)
                    self._write_json_cache(url_hash, "tags", tags)

                yield event.plain_result("⏳ 素材转写完成，正在交由 AI 思考...")

            segments_to_prompt = []
            for seg in transcript["segments"]:
                if isinstance(seg, dict):
                    segments_to_prompt.append(TranscriptSegment(**seg))
                else:
                    segments_to_prompt.append(seg)

            def format_time(seconds: float) -> str:
                total = int(seconds)
                hours, remainder = divmod(total, 3600)
                minutes, seconds = divmod(remainder, 60)
                if hours > 0:
                    return f"{hours}:{minutes:02d}:{seconds:02d}"
                return f"{minutes:02d}:{seconds:02d}"

            segment_text = "\n".join(
                f"{format_time(segment.start)} - {segment.text.strip()}"
                for segment in segments_to_prompt
            )

            # 模板目录和文件名（支持通过配置选择模板）
            prompts_dir = Path(__file__).parent / "core" / "prompts"
            prompts_dir.mkdir(parents=True, exist_ok=True)
            template_name = (
                getattr(self.cfg, "summary_template", "default.txt") or "default.txt"
            )
            template_path = prompts_dir / template_name

            if not template_path.exists():
                # 回退到默认模板文件
                fallback = prompts_dir / "default.txt"
                if fallback.exists():
                    template_path = fallback
                else:
                    logger.error(f"模板文件不存在: {template_path}")
                    yield event.plain_result(
                        "❌ 模板文件不存在，无法生成总结，请检查插件 prompts 目录下是否包含模板 txt 文件"
                    )
                    return

            try:
                with open(template_path, "r", encoding="utf-8") as f:
                    template_content = f.read()
            except Exception as e:
                logger.error(f"读取模板失败: {e}")
                yield event.plain_result("❌ 无法读取模板文件，请检查模板权限与路径")
                return

            # 为避免 template 中或待填充文本中包含未转义的大括号导致 str.format 抛错，先对填充值中的大括号进行转义
            def _escape_format(s: str) -> str:
                return s.replace("{", "{{").replace("}", "}}")

            safe_kwargs = {
                "video_title": _escape_format(str(title)),
                "tags": _escape_format(str(tags)),
                "segment_text": _escape_format(str(segment_text)),
            }

            try:
                prompt = template_content.format(**safe_kwargs)
            except Exception as e:
                logger.error(f"模板填充失败: {e}")
                yield event.plain_result(
                    "❌ 模板填充失败：模板或文本中可能包含无法解析的占位符"
                )
                return

            # 获取对应的 LLM 提供商
            provider_id = getattr(self.cfg, "llm_provider", "")
            if provider_id:
                provider = self.context.get_provider_by_id(provider_id)
            else:
                # 获取当前会话下默认分配的全局 LLM provider
                curr_provider_id = await self.context.get_current_chat_provider_id(
                    umo=event.unified_msg_origin
                )
                provider = self.context.get_provider_by_id(curr_provider_id)

            if not provider:
                yield event.plain_result(
                    "❌ 未配置 LLM Provider，或者指定了不存在的 LLM。请在 AstrBot 设置中配置"
                )
                return

            # 根据处理时长限定
            timeout = getattr(self.cfg, "processing_timeout", 120)
            chat_coro = provider.text_chat(
                prompt=prompt, session_id=f"VideoSummary_{uuid.uuid4().hex}"
            )

            start_t = time.time()
            response = await asyncio.wait_for(chat_coro, timeout=timeout)
            ai_cost_time = time.time() - start_t

            if hasattr(response, "completion_text"):
                result = response.completion_text
            elif isinstance(response, str):
                result = response
            else:
                result = str(response)

            # 调用参考自 markdown_killer 的清理逻辑去除 Markdown
            result = self._remove_markdown(result)

            # 保存总结缓存
            if enable_cache:
                self._write_json_cache(url_hash, "summary", result, url=url)
            if getattr(self.cfg, "show_token_usage", False):
                input_tokens = 0
                output_tokens = 0
                if (
                    not isinstance(response, str)
                    and hasattr(response, "usage")
                    and response.usage
                ):
                    usage = response.usage
                    input_tokens = getattr(usage, "input_other", 0) + getattr(
                        usage, "input_cached", 0
                    )
                    output_tokens = getattr(usage, "output", 0)
                result += f"\n━━━━━━━━━━━━━━\n输入: {input_tokens} tokens\n输出: {output_tokens} tokens\n耗时: {ai_cost_time:.2f} s"

            yield event.plain_result(f"📌 视频总结\n\n{result}")

        except asyncio.TimeoutError:
            logger.error("视频总结超时")
            yield event.plain_result("❌ 总结生成超时，视频可能过长。")
        except Exception as e:
            logger.error(f"视频总结失败: {e}", exc_info=True)
            yield event.plain_result(f"❌ 总结生成失败: {str(e)}")

        finally:
            import shutil

            # 彻底清理解析流程中产生的临时下载文件和 mp3
            for target in cleanup_targets:
                if target and target.exists():
                    try:
                        if target.is_file():
                            os.remove(target)
                    except Exception as e:
                        logger.warning(f"未能删除临时文件 {target} : {e}")

            # 清理 parser 阶段残留的临时文件，保留 cookies 目录以便复用持久化凭据
            try:
                if self._temp_dir and self._temp_dir.exists():
                    for item in self._temp_dir.iterdir():
                        if item.is_file():
                            item.unlink(missing_ok=True)
                        elif item.is_dir() and item.name != "cookies":
                            shutil.rmtree(item, ignore_errors=True)
            except Exception:
                pass

    def _remove_markdown(self, text: str) -> str:
        """
        参考 markdown_killer 项目的 Markdown 移除逻辑，确保输出内容结构稳定且具有普适可读性。
        """
        # 移除代码块 (保留内容)
        text = re.sub(r"```(?:[a-zA-Z0-9+\-]*\s+)?([\s\S]*?)```", r"\1", text)
        # 移除行内代码 `code` -> code
        text = re.sub(r"`([^`]+)`", r"\1", text)
        # 移除图片 ![alt](url) -> alt (提前于普通链接处理避免残留 "!")
        text = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", text)
        # 移除普通链接 [text](url) -> text
        text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
        # 移除粗体 - 使用非贪婪匹配以支持内部包含特殊符号的情况
        text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
        text = re.sub(r"__(.*?)__", r"\1", text)
        # 移除斜体 - 严格模式，避免误伤数学公式 (3 * 4 = 12) 或变量名 (this_is_var)
        text = re.sub(r"(?<!\*)\*(?!\s)(.*?)(?<!\s)\*(?!\*)", r"\1", text)
        text = re.sub(r"(?<!\w)_(?!\s)(.*?)(?<!\s)_(?!\w)", r"\1", text)
        # 移除删除线
        text = re.sub(r"~~(.*?)~~", r"\1", text)
        # 移除标题 (包含多级标题)
        text = re.sub(r"^(#{1,6})\s+(.*)", r"\2", text, flags=re.MULTILINE)
        # 移除引用 (处理嵌套情况: >>> text -> text)
        text = re.sub(r"^(?:>\s*)+(.*)", r"\1", text, flags=re.MULTILINE)
        # 移除列表标记 (移除行首的 -, *, +)
        text = re.sub(r"^\s*[-*+]\s+(.*)", r"\1", text, flags=re.MULTILINE)

        return text

    async def terminate(self):
        """插件卸载时触发"""
        await self.downloader.close()
