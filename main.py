import re
import time
import asyncio
import uuid
import json
import os
import shutil
from pathlib import Path
from typing import Optional, Tuple, List, Any

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.core import AstrBotConfig
from astrbot.api.message_components import Reply, Plain

# 引用 bcut 和模型
from .core.transcriber.bcut import BcutTranscriber
from .core.transcriber.transcriber_model import TranscriptSegment

# 引用 parser 项目组件
from .core.parser.download import Downloader
from .core.parser.config import PluginConfig
from .core.parser.parsers.base import BaseParser
from .core.parser.parsers.direct import DirectMediaParser
from .core.tools.media_subtitle_tool import (
    MediaSummaryTool,
    MediaSubtitleTool,
    segments_to_text,
)


class VideoSummaryPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.cfg = PluginConfig(config, context=context)
        self.downloader = Downloader(self.cfg)
        self.transcriber = BcutTranscriber()
        self._direct_parser = DirectMediaParser(self.cfg, self.downloader)

        # 确保 temp_dir 与 cache_dir 为 Path 且存在
        self._temp_dir = Path(getattr(self.cfg, "temp_dir", Path.cwd() / "tmp"))
        self._cache_dir = Path(getattr(self.cfg, "cache_dir", self._temp_dir / "cache"))
        try:
            self._temp_dir.mkdir(parents=True, exist_ok=True)
            self._cache_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.warning(f"无法创建临时/缓存目录: {e}")

        self._parser_patterns = self._build_parser_index()

        tools = []
        if getattr(self.cfg, "enable_media_summary_tool", True):
            tools.append(MediaSummaryTool(plugin=self))
        if getattr(self.cfg, "enable_media_subtitle_tool", False):
            tools.append(MediaSubtitleTool(plugin=self))
        if tools:
            self.context.add_llm_tools(*tools)

    def _build_parser_index(self):
        """构建提取器索引"""
        patterns = []
        for parser_cls in BaseParser.get_all_subclass():
            if parser_cls is DirectMediaParser:
                continue
            parser_inst = parser_cls(self.cfg, self.downloader)
            # 聚合所有的配置里的白名单正则表达式
            # 这里简单直接聚合所有 parser 的 _key_patterns
            for keyword, pattern in getattr(parser_inst, "_key_patterns", []):
                patterns.append((keyword, pattern, parser_inst))
        return patterns

    def _extract_first_http(self, text: str) -> Optional[str]:
        """从文本中提取第一个以 http/https 开头的链接，去掉末尾常见标点。

        返回第一个匹配到的链接字符串或 None。
        """
        if not text:
            return None
        # 匹配以 http 或 https 开头直到遇到空白字符的部分
        m = re.search(r"https?://\S+", text)
        if not m:
            return None
        url = m.group(0)
        # 去除末尾可能跟着的中文/英文标点或括号
        url = url.rstrip("\u3002\uff0c\uff1f\uff01.,;:!?)]}\u3001")
        return url

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

    async def _resolve_url_with_direct_fallback(
        self, url: str
    ) -> Tuple[Optional[BaseParser], Optional[str], Optional[Any], bool]:
        parser_inst, keyword, searched = await self._resolve_url(url)
        if parser_inst:
            return parser_inst, keyword, searched, False

        direct_searched = self._direct_parser.match_direct_url(url)
        if direct_searched:
            return self._direct_parser, "direct", direct_searched, True

        return None, None, None, False

    async def _parse_result_with_parser(
        self,
        parser_inst: BaseParser,
        url: str,
        keyword: Optional[str],
        searched: Optional[Any],
    ):
        if parser_inst is self._direct_parser:
            return await self._direct_parser.parse_direct_url(url)
        return await parser_inst.parse_with_redirect(url=url)

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

        source_path = None
        last_error = None
        for content_list in (parse_result.audio_contents, parse_result.video_contents):
            if content_list:
                try:
                    source_path = await content_list[0].get_path()
                    if source_path and source_path.exists():
                        last_error = None
                        break
                except Exception as e:
                    last_error = e
                    continue

        if not source_path or not source_path.exists():
            if last_error is not None:
                raise last_error
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

    def _cleanup_temp_files(self, cleanup_targets: List[Path]):
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

    def _check_access(self, umo: str) -> bool:
        whitelist = getattr(self.cfg, "whitelist", None) or []
        blacklist = getattr(self.cfg, "blacklist", None) or []
        if whitelist and umo not in whitelist:
            return False
        if blacklist and umo in blacklist:
            return False
        return True

    @filter.command("总结")
    async def summarize_video(self, event: AstrMessageEvent, url: str = ""):
        """总结任意视频链接: /总结 <URL>"""
        if not self._check_access(event.unified_msg_origin):
            yield event.plain_result("❌ 你没有权限使用此功能")
            return
        async for result in self._summarize_video_impl(event, url, force_refresh=False):
            yield result

    @filter.command("强制总结")
    async def force_summarize_video(self, event: AstrMessageEvent, url: str = ""):
        """强制重新总结任意视频链接: /强制总结 <URL>"""
        if not self._check_access(event.unified_msg_origin):
            yield event.plain_result("❌ 你没有权限使用此功能")
            return
        async for result in self._summarize_video_impl(event, url, force_refresh=True):
            yield result

    async def _summarize_video_impl(
        self, event: AstrMessageEvent, url: str, force_refresh: bool = False
    ):
        """统一总结主流程。force_refresh=True 时优先复用本地字幕缓存并强制重跑 LLM。"""
        # 为了支持用户在 URL 前后带描述文本（例如："/总结 B站视频<url>"），
        # 先尝试从完整的消息文本中提取第一个 http 链接作为最终的 url。
        raw_msg = getattr(event, "message_str", None) or ""
        first = self._extract_first_http(raw_msg)
        if first:
            url = first
        else:
            # 如果从当条消息中没有提取到链接，尝试从引用的消息中提取
            message_chain = event.get_messages()
            reply_seg = next(
                (seg for seg in message_chain if isinstance(seg, Reply)), None
            )
            if reply_seg and reply_seg.chain:
                reply_text = ""
                for seg in reply_seg.chain:
                    if isinstance(seg, Plain):
                        reply_text += seg.text
                if reply_text:
                    first_reply_url = self._extract_first_http(reply_text)
                    if first_reply_url:
                        url = first_reply_url

        if not isinstance(url, str) or not url.startswith("http"):
            yield event.plain_result(
                "❌ 请输入有效的URL链接，或者引用一条包含链接的消息"
            )
            return

        parser_inst, keyword, searched, used_direct_fallback = (
            await self._resolve_url_with_direct_fallback(url)
        )
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
        direct_fallback_completed = False
        try:
            # 2. 命中字幕缓存时可跳过下载与转写
            if enable_cache and cache_url_match:
                cached_trs = cache_dict.get("transcript")
                if cached_trs:
                    transcript = {"segments": cached_trs}
                    title = str(cache_dict.get("title") or "缓存视频")
                    tags = str(cache_dict.get("tags") or "通用视频")
                    if used_direct_fallback:
                        direct_fallback_completed = True
                    if force_refresh:
                        yield event.plain_result(
                            "⏳ 强制总结：命中本地字幕缓存，正在交由 AI 重新思考..."
                        )
                    else:
                        yield event.plain_result("⏳ 素材命中缓存，正在交由 AI 思考...")

            if not transcript:
                # 3. 借助 parser 项目解析与下载
                try:
                    parse_result = await self._parse_result_with_parser(
                        parser_inst=parser_inst,
                        url=url,
                        keyword=keyword,
                        searched=searched,
                    )
                except Exception:
                    if used_direct_fallback:
                        yield event.plain_result("❌ 未找到支持处理此链接的解析器")
                        return
                    raise

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
                if used_direct_fallback:
                    direct_fallback_completed = True
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

            segment_text = segments_to_text(segments_to_prompt)

            start_t = time.time()
            try:
                result = await self._call_llm_for_summary(
                    title, tags, segment_text, event
                )
            except FileNotFoundError as e:
                logger.error(str(e))
                yield event.plain_result(
                    "❌ 模板文件不存在，无法生成总结，请检查插件 prompts 目录下是否包含模板 txt 文件"
                )
                return
            except RuntimeError as e:
                logger.error(str(e))
                yield event.plain_result(
                    "❌ 未配置 LLM Provider，或者指定了不存在的 LLM。请在 AstrBot 设置中配置"
                )
                return
            ai_cost_time = time.time() - start_t

            # 保存总结缓存
            if enable_cache:
                self._write_json_cache(url_hash, "summary", result, url=url)
            if getattr(self.cfg, "show_token_usage", False):
                result += f"\n━━━━━━━━━━━━━━\n耗时: {ai_cost_time:.2f} s"

            yield event.plain_result(f"📌 视频总结\n\n{result}")

        except asyncio.TimeoutError:
            logger.error("视频总结超时")
            yield event.plain_result("❌ 总结生成超时，视频可能过长。")
        except Exception as e:
            if used_direct_fallback and not direct_fallback_completed:
                logger.warning(f"直链回退提取失败: {e}")
                yield event.plain_result("❌ 未找到支持处理此链接的解析器")
                return
            logger.error(f"视频总结失败: {e}", exc_info=True)
            yield event.plain_result(f"❌ 总结生成失败: {str(e)}")

        finally:
            self._cleanup_temp_files(cleanup_targets)

    async def _call_llm_for_summary(
        self,
        title: str,
        tags: str,
        segment_text: str,
        event: AstrMessageEvent | None = None,
    ) -> str:
        """加载模板、调用 LLM 生成总结，返回纯文本结果。"""
        prompts_dir = Path(__file__).parent / "core" / "prompts"
        prompts_dir.mkdir(parents=True, exist_ok=True)
        template_name = (
            getattr(self.cfg, "summary_template", "default.txt") or "default.txt"
        )
        template_path = prompts_dir / template_name
        if not template_path.exists():
            fallback = prompts_dir / "default.txt"
            if fallback.exists():
                template_path = fallback
            else:
                raise FileNotFoundError(f"模板文件不存在: {template_path}")

        with open(template_path, "r", encoding="utf-8") as f:
            template_content = f.read()

        def _escape_format(s: str) -> str:
            return s.replace("{", "{{").replace("}", "}}")

        safe_kwargs = {
            "video_title": _escape_format(str(title)),
            "tags": _escape_format(str(tags)),
            "segment_text": _escape_format(str(segment_text)),
        }
        prompt = template_content.format(**safe_kwargs)

        provider_id = getattr(self.cfg, "llm_provider", "")
        if provider_id:
            provider = self.context.get_provider_by_id(provider_id)
        elif event is not None:
            curr_provider_id = await self.context.get_current_chat_provider_id(
                umo=event.unified_msg_origin
            )
            provider = self.context.get_provider_by_id(curr_provider_id)
        else:
            provider = None

        if not provider:
            raise RuntimeError(
                "未配置 LLM Provider。请在插件配置中设置 llm_provider 或在 AstrBot 全局设置中配置"
            )

        timeout = getattr(self.cfg, "processing_timeout", 120)
        chat_coro = provider.text_chat(
            prompt=prompt, session_id=f"VideoSummary_{uuid.uuid4().hex}"
        )
        response = await asyncio.wait_for(chat_coro, timeout=timeout)

        if hasattr(response, "completion_text"):
            result = response.completion_text
        elif isinstance(response, str):
            result = response
        else:
            result = str(response)

        return self._remove_markdown(result)

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
