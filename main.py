# author: Yangyuwuhan
# repo: https://github.com/Yangyuwuhan/astrbot_plugin_summary

import re
import time
import asyncio
import uuid
import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple, List, Any

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.core import AstrBotConfig
from astrbot.api.message_components import Reply, Plain
from quart import jsonify, request

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
    format_time,
    segments_to_text,
)


PLUGIN_NAME = "astrbot_plugin_summary"


class _ProcessingConfigProxy:
    def __init__(self, base_cfg: PluginConfig, temp_dir: Path):
        self._base_cfg = base_cfg
        self.temp_dir = temp_dir

    def __getattr__(self, name: str):
        return getattr(self._base_cfg, name)


class VideoSummaryPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.cfg = PluginConfig(config, context=context)
        self.downloader = Downloader(self.cfg)
        self.transcriber = BcutTranscriber()
        self._direct_parser = DirectMediaParser(self.cfg, self.downloader)
        self._cache_locks: dict[str, asyncio.Lock] = {}

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

        self.context.register_web_api(
            f"/{PLUGIN_NAME}/cache/list",
            self.page_cache_list,
            ["GET"],
            "Summary cache list",
        )
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/cache/detail",
            self.page_cache_detail,
            ["GET"],
            "Summary cache detail",
        )

    def _is_parser_enabled(self, platform_name: str) -> bool:
        parser_nodes = getattr(getattr(self.cfg, "parser", None), "_nodes", {})
        parser_cfg = parser_nodes.get(platform_name)
        if parser_cfg is None:
            return False
        return bool(getattr(parser_cfg, "enable", False))

    def _build_parser_index(
        self, cfg: Optional[PluginConfig] = None, downloader: Optional[Downloader] = None
    ):
        """构建已启用的提取器索引"""
        cfg = cfg or self.cfg
        downloader = downloader or self.downloader
        patterns = []
        for parser_cls in BaseParser.get_all_subclass():
            if parser_cls is DirectMediaParser:
                continue
            platform_name = getattr(parser_cls.platform, "name", "")
            if not self._is_parser_enabled(platform_name):
                continue
            parser_inst = parser_cls(cfg, downloader)
            for keyword, pattern in getattr(parser_inst, "_key_patterns", []):
                patterns.append((keyword, pattern, parser_inst))
        return patterns

    def _create_processing_runtime(self) -> dict[str, Any]:
        temp_dir = self._temp_dir / f"job-{uuid.uuid4().hex}"
        temp_dir.mkdir(parents=True, exist_ok=True)
        cfg = _ProcessingConfigProxy(self.cfg, temp_dir)
        downloader = Downloader(cfg)
        direct_parser = DirectMediaParser(cfg, downloader)
        return {
            "temp_dir": temp_dir,
            "downloader": downloader,
            "direct_parser": direct_parser,
            "parser_patterns": self._build_parser_index(cfg=cfg, downloader=downloader),
        }

    async def _close_processing_runtime(self, runtime: Optional[dict[str, Any]]):
        if not runtime:
            return

        parser_instances = {
            id(parser): parser for _, _, parser in runtime.get("parser_patterns", [])
        }
        direct_parser = runtime.get("direct_parser")
        if direct_parser is not None:
            parser_instances[id(direct_parser)] = direct_parser

        for parser in parser_instances.values():
            close_session = getattr(parser, "close_session", None)
            if close_session:
                try:
                    await close_session()
                except Exception as e:
                    logger.warning(f"关闭解析器会话失败: {e}")

        downloader = runtime.get("downloader")
        if downloader is not None:
            try:
                await downloader.close()
            except Exception as e:
                logger.warning(f"关闭下载器会话失败: {e}")

        temp_dir = runtime.get("temp_dir")
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)

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

    def _get_cache_lock(self, url_hash: str) -> asyncio.Lock:
        lock = self._cache_locks.get(url_hash)
        if lock is None:
            lock = asyncio.Lock()
            self._cache_locks[url_hash] = lock
        return lock

    async def _write_json_cache(
        self,
        url_hash: str,
        key: str | dict[str, Any],
        value: Any = None,
        url: Optional[str] = None,
    ):
        lock = self._get_cache_lock(url_hash)
        async with lock:
            data = self._read_json_cache(url_hash)
            if url:
                data["url"] = url
            if isinstance(key, dict):
                data.update(key)
            else:
                data[key] = value

            cache_file = self._get_json_cache_path(url_hash)
            tmp_file = cache_file.with_name(f"{cache_file.name}.{uuid.uuid4().hex}.tmp")
            try:
                with open(tmp_file, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=4)
                tmp_file.replace(cache_file)
            finally:
                tmp_file.unlink(missing_ok=True)

    def _format_cache_mtime(self, cache_file: Path) -> str:
        try:
            modified = datetime.fromtimestamp(
                cache_file.stat().st_mtime, tz=self.cfg.timezone
            )
            return modified.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return ""

    def _normalize_cache_segments(self, segments: Any) -> list[dict[str, Any]]:
        if not isinstance(segments, list):
            return []
        normalized = []
        for seg in segments:
            if isinstance(seg, dict):
                start = seg.get("start", 0)
                end = seg.get("end", 0)
                text = str(seg.get("text") or "").strip()
            else:
                start = getattr(seg, "start", 0)
                end = getattr(seg, "end", 0)
                text = str(getattr(seg, "text", "") or "").strip()
            try:
                start_value = float(start or 0)
            except (TypeError, ValueError):
                start_value = 0.0
            try:
                end_value = float(end or 0)
            except (TypeError, ValueError):
                end_value = 0.0
            if not text:
                continue
            normalized.append(
                {
                    "start": start_value,
                    "end": end_value,
                    "time": format_time(start_value),
                    "text": text,
                }
            )
        return normalized

    def _cache_entry_payload(
        self, cache_id: str, cache_file: Path, data: dict, include_detail: bool = False
    ) -> dict[str, Any]:
        segments = self._normalize_cache_segments(data.get("transcript"))
        summary = str(data.get("summary") or "")
        title = str(data.get("title") or "未命名缓存")
        tags = str(data.get("tags") or "通用视频")
        url = str(data.get("url") or "")
        transcript_text = "\n".join(
            f"{seg['time']} - {seg['text']}" for seg in segments
        )
        payload = {
            "id": cache_id,
            "url": url,
            "title": title,
            "tags": tags,
            "updated_at": self._format_cache_mtime(cache_file),
            "has_summary": bool(summary.strip()),
            "has_transcript": bool(segments),
            "segment_count": len(segments),
            "summary_preview": summary[:180],
            "transcript_preview": transcript_text[:220],
        }
        if include_detail:
            payload.update(
                {
                    "summary": summary,
                    "transcript": segments,
                    "transcript_text": transcript_text,
                }
            )
        return payload

    async def page_cache_list(self):
        query = str(request.args.get("q") or "").strip().lower()
        items = []
        for cache_file in sorted(
            self._cache_dir.glob("*.json"),
            key=lambda p: p.stat().st_mtime if p.exists() else 0,
            reverse=True,
        ):
            if cache_file.name.endswith(".tmp"):
                continue
            data = self._read_json_cache(cache_file.stem)
            if not data:
                continue
            item = self._cache_entry_payload(cache_file.stem, cache_file, data)
            haystack = " ".join(
                str(item.get(key) or "")
                for key in ("url", "title", "tags", "summary_preview", "transcript_preview")
            ).lower()
            if query and query not in haystack:
                continue
            items.append(item)
            if len(items) >= 50:
                break
        return jsonify({"items": items, "total": len(items), "limit": 50})

    async def page_cache_detail(self):
        cache_id = str(request.args.get("id") or "").strip()
        if not re.fullmatch(r"[0-9a-fA-F]{32}", cache_id):
            return jsonify({"error": "invalid cache id"}), 400
        cache_file = self._get_json_cache_path(cache_id)
        if not cache_file.exists():
            return jsonify({"error": "cache not found"}), 404
        data = self._read_json_cache(cache_id)
        if not data:
            return jsonify({"error": "cache is empty or unreadable"}), 404
        return jsonify(self._cache_entry_payload(cache_id, cache_file, data, True))

    async def _resolve_url(
        self, url: str, parser_patterns: Optional[list] = None
    ) -> Tuple[Optional[BaseParser], Optional[str], Optional[Any]]:
        patterns = self._parser_patterns if parser_patterns is None else parser_patterns
        for keyword, pattern, parser_inst in patterns:
            if keyword in url:
                searched = pattern.search(url)
                if searched:
                    return parser_inst, keyword, searched
        return None, None, None

    async def _resolve_url_with_direct_fallback(
        self,
        url: str,
        parser_patterns: Optional[list] = None,
        direct_parser: Optional[DirectMediaParser] = None,
    ) -> Tuple[Optional[BaseParser], Optional[str], Optional[Any], bool]:
        parser_inst, keyword, searched = await self._resolve_url(
            url, parser_patterns=parser_patterns
        )
        if parser_inst:
            return parser_inst, keyword, searched, False

        if self._is_parser_enabled("direct"):
            direct_parser = direct_parser or self._direct_parser
            direct_searched = direct_parser.match_direct_url(url)
            if direct_searched:
                return direct_parser, "direct", direct_searched, True

        return None, None, None, False

    async def _parse_result_with_parser(
        self,
        parser_inst: BaseParser,
        url: str,
        keyword: Optional[str],
        searched: Optional[Any],
    ):
        if isinstance(parser_inst, DirectMediaParser):
            return await parser_inst.parse_direct_url(url)
        if keyword is not None and searched is not None:
            try:
                return await parser_inst.parse(keyword, searched)
            except Exception as e:
                logger.warning(f"原始链接解析失败，尝试重定向解析: {e}")
        return await parser_inst.parse_with_redirect(url=url)

    def _get_processing_timeout(self) -> Optional[float]:
        timeout = getattr(self.cfg, "processing_timeout", 120)
        try:
            timeout = float(timeout)
        except (TypeError, ValueError):
            timeout = 120.0
        return timeout if timeout > 0 else None

    def _new_processing_deadline(self) -> Optional[float]:
        timeout = self._get_processing_timeout()
        if timeout is None:
            return None
        return time.monotonic() + timeout

    def _remaining_processing_timeout(self, deadline: Optional[float]) -> Optional[float]:
        if deadline is None:
            return None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise asyncio.TimeoutError
        return remaining

    async def _wait_for_processing(self, awaitable, deadline: Optional[float]):
        try:
            timeout = self._remaining_processing_timeout(deadline)
        except BaseException:
            close = getattr(awaitable, "close", None)
            if close:
                close()
            raise
        if timeout is None:
            return await awaitable
        return await asyncio.wait_for(awaitable, timeout=timeout)

    async def _transcribe_audio(self, audio_path: Path, deadline: Optional[float]):
        bcut_timeout = self._remaining_processing_timeout(deadline)
        transcriber = BcutTranscriber()
        try:
            return await asyncio.to_thread(
                transcriber.transcript, str(audio_path), bcut_timeout
            )
        finally:
            try:
                transcriber.close()
            except Exception as e:
                logger.warning(f"关闭必剪转写会话失败: {e}")

    async def _materialize_audio(
        self, parse_result, deadline: Optional[float] = None
    ) -> Tuple[Path, List[Path]]:
        """提取或转换第一份音频或视频素材得到 mp3 供 bcut 处理"""
        targets = []
        source_path = None

        source_path = None
        last_error = None
        for content_list in (parse_result.audio_contents, parse_result.video_contents):
            if content_list:
                try:
                    source_path = await self._wait_for_processing(
                        content_list[0].get_path(), deadline
                    )
                    if source_path and source_path.exists():
                        last_error = None
                        break
                except asyncio.TimeoutError:
                    raise
                except Exception as e:
                    last_error = e
                    continue

        await self._cancel_unused_content_tasks(parse_result)

        if not source_path or not source_path.exists():
            if last_error is not None:
                raise last_error
            raise FileNotFoundError("未成功拉取到媒体文件实体")
        targets.append(source_path)

        out_mp3 = source_path.parent / f"{uuid.uuid4().hex}.mp3"
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
        try:
            await self._wait_for_processing(proc.communicate(), deadline)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.communicate()
            raise

        if proc.returncode != 0 or not out_mp3.exists():
            raise RuntimeError("ffmpeg 转换音频失败。")

        return out_mp3, targets

    def _cleanup_temp_files(self, cleanup_targets: List[Path]):
        # 只清理本次流程明确产生/使用过的文件，避免并发任务互相删除素材。
        seen: set[Path] = set()
        for target in cleanup_targets:
            if not target:
                continue
            try:
                target = Path(target)
                if target in seen:
                    continue
                seen.add(target)
                if target.exists() and target.is_file():
                    target.unlink(missing_ok=True)
            except Exception as e:
                logger.warning(f"未能删除临时文件 {target} : {e}")

    @staticmethod
    async def _cancel_unused_content_tasks(parse_result):
        tasks = []
        for content in getattr(parse_result, "contents", []):
            for attr in ("path_task", "cover"):
                task = getattr(content, attr, None)
                if isinstance(task, asyncio.Task) and not task.done():
                    task.cancel()
                    tasks.append(task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

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

        runtime = self._create_processing_runtime()
        try:
            (
                parser_inst,
                keyword,
                searched,
                used_direct_fallback,
            ) = await self._resolve_url_with_direct_fallback(
                url,
                parser_patterns=runtime["parser_patterns"],
                direct_parser=runtime["direct_parser"],
            )
            if not parser_inst:
                yield event.plain_result("❌ 未找到支持处理此链接的解析器")
                return

            async for result in self._summarize_resolved_video_impl(
                event=event,
                url=url,
                parser_inst=parser_inst,
                keyword=keyword,
                searched=searched,
                used_direct_fallback=used_direct_fallback,
                force_refresh=force_refresh,
            ):
                yield result
        finally:
            await self._close_processing_runtime(runtime)

    async def _summarize_resolved_video_impl(
        self,
        event: AstrMessageEvent,
        url: str,
        parser_inst: BaseParser,
        keyword: Optional[str],
        searched: Optional[Any],
        used_direct_fallback: bool,
        force_refresh: bool = False,
    ):
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

        deadline = self._new_processing_deadline()
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
                    parse_result = await self._wait_for_processing(
                        self._parse_result_with_parser(
                            parser_inst=parser_inst,
                            url=url,
                            keyword=keyword,
                            searched=searched,
                        ),
                        deadline,
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
                    parse_result, deadline=deadline
                )

                # 4. 交给 bcut 转写
                transcript_res = await self._transcribe_audio(audio_path, deadline)

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
                    await self._write_json_cache(
                        url_hash,
                        {
                            "transcript": [
                                {
                                    "start": seg.start,
                                    "end": seg.end,
                                    "text": seg.text,
                                }
                                for seg in transcript["segments"]
                            ],
                            "title": title,
                            "tags": tags,
                        },
                        url=url,
                    )

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
                result, token_usage = await self._call_llm_for_summary(
                    title,
                    tags,
                    segment_text,
                    event,
                    timeout=self._remaining_processing_timeout(deadline),
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
                await self._write_json_cache(url_hash, "summary", result, url=url)
            if getattr(self.cfg, "show_token_usage", False):
                result += self._format_token_usage(token_usage, ai_cost_time)

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
        timeout: Optional[float] = None,
    ) -> tuple[str, Optional[dict[str, int]]]:
        """加载模板、调用 LLM 生成总结，返回纯文本结果和 token 用量。"""
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

        chat_timeout = timeout if timeout is not None else self._get_processing_timeout()
        chat_coro = provider.text_chat(
            prompt=prompt, session_id=f"VideoSummary_{uuid.uuid4().hex}"
        )
        if chat_timeout is None:
            response = await chat_coro
        else:
            response = await asyncio.wait_for(chat_coro, timeout=chat_timeout)

        if hasattr(response, "completion_text"):
            result = response.completion_text
        elif isinstance(response, str):
            result = response
        else:
            result = str(response)

        return self._remove_markdown(result), self._extract_token_usage(response)

    def _extract_token_usage(self, response: Any) -> Optional[dict[str, int]]:
        raw_completion = getattr(response, "raw_completion", None)
        usage = None
        if raw_completion is not None:
            usage = getattr(raw_completion, "usage", None)
            if usage is None and isinstance(raw_completion, dict):
                usage = raw_completion.get("usage")
        if usage is None:
            usage = getattr(response, "usage", None)
            if usage is None and isinstance(response, dict):
                usage = response.get("usage")
        if usage is None:
            return None

        def _read_int(*names: str) -> Optional[int]:
            for name in names:
                value = None
                if isinstance(usage, dict):
                    value = usage.get(name)
                else:
                    value = getattr(usage, name, None)
                if value is None:
                    continue
                try:
                    return int(value)
                except (TypeError, ValueError):
                    continue
            return None

        input_tokens = _read_int("prompt_tokens", "input_tokens")
        output_tokens = _read_int("completion_tokens", "output_tokens")
        total_tokens = _read_int("total_tokens")
        if total_tokens is None and (input_tokens is not None or output_tokens is not None):
            total_tokens = (input_tokens or 0) + (output_tokens or 0)
        if input_tokens is None and output_tokens is None and total_tokens is None:
            return None
        result = {}
        if input_tokens is not None:
            result["input"] = input_tokens
        if output_tokens is not None:
            result["output"] = output_tokens
        if total_tokens is not None:
            result["total"] = total_tokens
        return result

    def _format_token_usage(
        self, token_usage: Optional[dict[str, int]], cost_time: float
    ) -> str:
        if not token_usage:
            return (
                f"\n━━━━━━━━━━━━━━\n"
                f"输入: 未返回\n输出: 未返回\n总计: 未返回\n耗时: {cost_time:.2f} s"
            )
        def _format_count(value: int | None) -> str:
            return f"{value} tokens" if value is not None else "未返回"

        input_tokens = token_usage.get("input")
        output_tokens = token_usage.get("output")
        total_tokens = token_usage.get("total")
        return (
            f"\n━━━━━━━━━━━━━━\n"
            f"输入: {_format_count(input_tokens)}\n"
            f"输出: {_format_count(output_tokens)}\n"
            f"总计: {_format_count(total_tokens)}\n"
            f"耗时: {cost_time:.2f} s"
        )

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
        parser_instances = {id(parser): parser for _, _, parser in self._parser_patterns}
        parser_instances[id(self._direct_parser)] = self._direct_parser
        for parser in parser_instances.values():
            close_session = getattr(parser, "close_session", None)
            if close_session:
                try:
                    await close_session()
                except Exception as e:
                    logger.warning(f"关闭解析器会话失败: {e}")
        try:
            self.transcriber.close()
        except Exception as e:
            logger.warning(f"关闭必剪转写会话失败: {e}")
        await self.downloader.close()
