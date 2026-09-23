"""Reply to QQ videos with a short AI opinion, including Bilibili cards."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import aiohttp
import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

BILI_URL_RE = re.compile(r"(?:https?://)?(?:www\.)?bilibili\.com/video/(?:(BV[0-9A-Za-z]+)|av(\d+))", re.IGNORECASE)
B23_RE = re.compile(r"https?://b23\.tv/[0-9A-Za-z]+", re.IGNORECASE)
DEFAULT_SYSTEM_PROMPT = "你是一个视频评价助手。根据用户发送视频的关键画面，给出简短、自然的中文评价。可以直接说好看或不好看，并用一句话说明原因，不要声称看到了无法确认的内容。"


def _walk_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _walk_strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _walk_strings(item)


def _component_data(component: Any) -> Any:
    data = getattr(component, "data", None)
    return data if data is not None else getattr(component, "__dict__", {})


@register("astrbot_plugin_bilipinshi", "OpenAI", "让你的bot可以吃你搬的答辩视频回复简短评价，支持 B 站卡片转换。", "1.0.0")
class QQVideoReplyPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.work_dir = Path(tempfile.mkdtemp(prefix="astrbot_qq_video_"))
        self.http: aiohttp.ClientSession | None = None

    async def initialize(self):
        self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=float(self.config.get("http_timeout", 30))))

    async def terminate(self):
        if self.http and not self.http.closed:
            await self.http.close()
        shutil.rmtree(self.work_dir, ignore_errors=True)

    def _strings_from_event(self, event: AstrMessageEvent) -> list[str]:
        values = [event.message_str or ""]
        for component in event.get_messages():
            values.extend(_walk_strings(_component_data(component)))
        values.extend(_walk_strings(getattr(event.message_obj, "raw_message", None)))
        return values

    async def _extract_bili_url(self, event: AstrMessageEvent) -> str | None:
        for text in self._strings_from_event(event):
            text = text.replace("\\\\/", "/")
            match = BILI_URL_RE.search(text)
            if match:
                value = match.group(0)
                return value if value.lower().startswith("http") else "https://" + value
            short = B23_RE.search(text)
            if short:
                return await self._resolve_short_url(short.group(0))
        return None

    async def _fetch_bili_metadata(self, url: str) -> dict[str, Any]:
        if not self.http:
            return {}
        match = BILI_URL_RE.search(url)
        if not match:
            return {}
        params = {"bvid": match.group(1)} if match.group(1) else {"aid": match.group(2)}
        try:
            async with self.http.get(
                "https://api.bilibili.com/x/web-interface/view",
                params=params,
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com/"},
            ) as response:
                payload = await response.json(content_type=None)
                if payload.get("code") == 0:
                    return payload.get("data") or {}
        except Exception as exc:
            logger.warning("获取 B 站元数据失败: %s", exc)
        return {}

    async def _resolve_short_url(self, url: str) -> str:
        if not self.http:
            return url
        try:
            async with self.http.get(url, allow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}) as response:
                return str(response.url)
        except Exception as exc:
            logger.warning("解析 B 站短链接失败: %s", exc)
            return url

    async def _download_bilibili(self, url: str) -> tuple[Path, dict[str, Any]]:
        import yt_dlp

        output = self.work_dir / "bilibili_%(id)s.%(ext)s"
        options = {"format": "bv*+ba/b", "outtmpl": str(output), "merge_output_format": "mp4", "noplaylist": True, "quiet": True, "no_warnings": True, "restrictfilenames": True, "http_headers": {"User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com/"}}

        def run():
            with yt_dlp.YoutubeDL(options) as downloader:
                info = downloader.extract_info(url, download=True)
                prepared = downloader.prepare_filename(info)
                candidates = [Path(prepared), Path(os.path.splitext(prepared)[0] + ".mp4")]
                for candidate in candidates:
                    if candidate.exists() and candidate.stat().st_size:
                        return candidate, info
                matches = sorted(self.work_dir.glob("bilibili_*"), key=lambda p: p.stat().st_mtime, reverse=True)
                if matches:
                    return matches[0], info
                raise FileNotFoundError("yt-dlp 未生成视频文件")

        return await asyncio.to_thread(run)

    async def _materialize_video(self, video: Comp.Video) -> Path:
        source = getattr(video, "path", None) or getattr(video, "file", "")
        if source.startswith("file:///"):
            path = Path(source[8:])
            if path.exists():
                return path
        if source.startswith(("http://", "https://")):
            if not self.http:
                raise RuntimeError("HTTP 会话未初始化")
            destination = self.work_dir / "incoming_video"
            async with self.http.get(source) as response:
                response.raise_for_status()
                with destination.open("wb") as stream:
                    async for chunk in response.content.iter_chunked(1024 * 256):
                        stream.write(chunk)
            return destination
        path = Path(source)
        if path.exists():
            return path
        raise FileNotFoundError(f"无法访问视频文件: {source}")

    async def _extract_frames(self, video_path: Path) -> list[Path]:
        frame_dir = self.work_dir / f"frames_{video_path.stem}"
        frame_dir.mkdir(exist_ok=True)
        command = ["ffmpeg", "-y", "-i", str(video_path), "-vf", "fps=1/10,scale=768:-2", "-frames:v", str(int(self.config.get("max_frames", 6))), str(frame_dir / "frame_%02d.jpg")]
        process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        _, stderr = await process.communicate()
        if process.returncode != 0:
            raise RuntimeError(f"FFmpeg 抽帧失败: {stderr.decode(errors='replace')[-500:]}")
        return sorted(frame_dir.glob("frame_*.jpg"))

    async def _analyze(self, event: AstrMessageEvent, video_path: Path) -> str:
        frames = await self._extract_frames(video_path)
        if not frames:
            raise RuntimeError("视频未提取出可分析的画面")
        provider = self.context.get_using_provider(event.unified_msg_origin)
        if not provider:
            raise RuntimeError("未配置可用的 LLM 提供商")
        response = await self.context.llm_generate(chat_provider_id=provider.meta().id, prompt="请评价这个视频，好看还是不好看？控制在两句话以内。", image_urls=[str(path) for path in frames], system_prompt=self.config.get("system_prompt", DEFAULT_SYSTEM_PROMPT))
        text = getattr(response, "completion_text", "") or ""
        return text.strip() or "这个视频我暂时无法判断好不好看。"

    async def _analyze_metadata(self, event: AstrMessageEvent, metadata: dict[str, Any]) -> str:
        """Analyze only Bilibili title and cover, avoiding a full video download."""
        title = metadata.get("title") or "未知标题"
        cover = metadata.get("pic") or metadata.get("thumbnail") or ""
        thumbnails = metadata.get("thumbnails") or []
        if not cover and thumbnails:
            cover = thumbnails[0].get("url", "")
        if cover.startswith("//"):
            cover = "https:" + cover
        provider = self.context.get_using_provider(event.unified_msg_origin)
        if not provider:
            raise RuntimeError("未配置可用的 LLM 提供商")
        response = await self.context.llm_generate(
            chat_provider_id=provider.meta().id,
            prompt=f"只根据这个 B 站视频的标题评价它是否好看，不要假装看过完整视频。标题：{title}",
            image_urls=[cover] if cover else [],
            system_prompt=self.config.get("system_prompt", DEFAULT_SYSTEM_PROMPT),
        )
        text = getattr(response, "completion_text", "") or ""
        return text.strip() or "这个视频我暂时无法判断好不好看。"

    def _fallback_chain(self, metadata: dict[str, Any], error: Exception):
        logger.error("B 站视频处理失败: %s", error, exc_info=True)
        title = metadata.get("title") or "B 站视频"
        thumbnails = metadata.get("thumbnails") or []
        cover = metadata.get("thumbnail") or (thumbnails[0].get("url", "") if thumbnails else "")
        chain = [Comp.Plain(f"{title}\n暂时无法完成视频分析。")]
        if cover:
            chain.insert(0, Comp.Image.fromURL(urljoin("https:", cover) if cover.startswith("//") else cover))
        return chain

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.ALL, priority=20)
    async def on_message(self, event: AstrMessageEvent):
        components = event.get_messages()
        video = next((item for item in components if isinstance(item, Comp.Video)), None)
        bili_url = await self._extract_bili_url(event) if not video else None
        if not video and not bili_url:
            return
        event.stop_event()
        metadata: dict[str, Any] = {}
        try:
            if video:
                video_path = await self._materialize_video(video)
            else:
                metadata = await self._fetch_bili_metadata(bili_url)
                if self.config.get("analyze_metadata_only", False):
                    yield event.plain_result(await self._analyze_metadata(event, metadata))
                    return
                video_path, metadata = await self._download_bilibili(bili_url)
            yield event.plain_result(await self._analyze(event, video_path))
        except Exception as error:
            if not video and self.config.get("fallback_metadata_on_error", True):
                yield event.chain_result(self._fallback_chain(metadata, error))
            else:
                logger.error("视频分析失败: %s", error, exc_info=True)
                yield event.plain_result("视频分析失败，请检查 FFmpeg、yt-dlp 和模型配置。")
