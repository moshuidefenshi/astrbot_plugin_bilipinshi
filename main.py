"""Evaluate Bilibili card messages sent in QQ chats."""

from __future__ import annotations

import json
import re
import asyncio
import shutil
import tempfile
from pathlib import Path
from typing import Any

import aiohttp
import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

BILI_URL_RE = re.compile(r"(?:https?://)?(?:www\.)?bilibili\.com/video/(?:(BV[0-9A-Za-z]+)|av(\d+))", re.IGNORECASE)
B23_RE = re.compile(r"https?://b23\.tv/[0-9A-Za-z]+", re.IGNORECASE)


def _walk_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _walk_strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _walk_strings(item)


def _card_data(component: Any) -> Any:
    data = getattr(component, "data", None)
    if isinstance(data, str):
        try:
            return json.loads(data)
        except json.JSONDecodeError:
            return data
    return data


@register("astrbot_plugin_bilipinshi", "OpenAI", "让你的bot可以吃你搬的答辩视频回复简短评价，支持 B 站卡片转换。", "1.1.0")
class BiliPinshiPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.http: aiohttp.ClientSession | None = None
        self.work_dir = Path(tempfile.mkdtemp(prefix="astrbot_bilipinshi_"))

    async def initialize(self):
        timeout = float(self.config.get("http_timeout", 30))
        self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout))

    async def terminate(self):
        if self.http and not self.http.closed:
            await self.http.close()
        shutil.rmtree(self.work_dir, ignore_errors=True)

    def _card_strings(self, event: AstrMessageEvent) -> list[str]:
        values: list[str] = []
        for component in event.get_messages():
            if isinstance(component, Comp.Json):
                values.extend(_walk_strings(_card_data(component)))
        return values

    async def _resolve_short_url(self, url: str) -> str:
        if not self.http:
            return url
        try:
            async with self.http.get(url, allow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}) as response:
                return str(response.url)
        except Exception as exc:
            logger.warning("解析 B 站短链接失败: %s", exc)
            return url

    async def _extract_url(self, event: AstrMessageEvent) -> str | None:
        for value in self._card_strings(event):
            value = value.replace("\\/", "/")
            match = BILI_URL_RE.search(value)
            if match:
                result = match.group(0)
                return result if result.startswith("http") else "https://" + result
            short = B23_RE.search(value)
            if short:
                return await self._resolve_short_url(short.group(0))
        return None

    async def _fetch_metadata(self, url: str) -> dict[str, Any]:
        if not self.http:
            raise RuntimeError("网络会话未初始化")
        match = BILI_URL_RE.search(url)
        if not match:
            raise ValueError("卡片中没有有效的 B 站视频地址")
        params = {"bvid": match.group(1)} if match.group(1) else {"aid": match.group(2)}
        async with self.http.get("https://api.bilibili.com/x/web-interface/view", params=params, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com/"}) as response:
            payload = await response.json(content_type=None)
        if payload.get("code") != 0:
            raise RuntimeError(payload.get("message") or "B 站接口返回错误")
        return payload.get("data") or {}

    def _preference_prompt(self) -> str:
        tendency = max(0, min(100, int(self.config.get("review_tendency", 50))))
        if tendency < 35:
            return "评论倾向明显偏向差评，优先挑出视频的问题。"
        if tendency > 65:
            return "评论倾向明显偏向好评，优先指出视频的优点。"
        return "评论保持中立，按标题和封面呈现的信息判断。"

    async def _analyze(self, event: AstrMessageEvent, metadata: dict[str, Any]) -> str:
        provider = self.context.get_using_provider(event.unified_msg_origin)
        if not provider:
            raise RuntimeError("未配置可用的 LLM 提供商")
        title = metadata.get("title") or "未知标题"
        cover = metadata.get("pic") or ""
        if cover.startswith("//"):
            cover = "https:" + cover
        prompt = "只根据下面这个 B 站视频的标题和封面进行评价，不要假装看过完整视频。\n标题：" + title + "\n" + self._preference_prompt() + "\n严格只输出1到10个中文字，可使用🐛表示特别难看，不要解释。"
        system_prompt = self.config.get("system_prompt", "你是一个视频评价助手。用一句话评价视频（1-10个字，可以用🐛表示不好看）")
        response = await self.context.llm_generate(chat_provider_id=provider.meta().id, prompt=prompt, image_urls=[cover] if cover else [], system_prompt=system_prompt)
        text = getattr(response, "completion_text", "") or ""
        return text.strip() or "这个视频我暂时无法判断好不好看。"

    async def _video_path(self, video: Comp.Video) -> Path:
        source = getattr(video, "path", None) or getattr(video, "file", "")
        if source.startswith("file:///"):
            path = Path(source[8:])
            if path.exists():
                return path
        if source.startswith(("http://", "https://")):
            if not self.http:
                raise RuntimeError("网络会话未初始化")
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

    async def _extract_video_frames(self, video_path: Path) -> list[Path]:
        import imageio_ffmpeg

        frame_dir = self.work_dir / "video_frames"
        frame_dir.mkdir(exist_ok=True)
        output_pattern = str(frame_dir / "frame_%02d.jpg")
        command = [
            imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-i", str(video_path),
            "-vf", "fps=1/10,scale=768:-2", "-frames:v",
            str(int(self.config.get("video_max_frames", 6))), output_pattern,
        ]
        process = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await process.communicate()
        if process.returncode != 0:
            raise RuntimeError(f"视频抽帧失败: {stderr.decode(errors='replace')[-500:]}")
        return sorted(frame_dir.glob("frame_*.jpg"))

    async def _analyze_video(self, event: AstrMessageEvent, video_path: Path) -> str:
        frames = await self._extract_video_frames(video_path)
        if not frames:
            raise RuntimeError("视频中没有可分析的画面")
        provider = self.context.get_using_provider(event.unified_msg_origin)
        if not provider:
            raise RuntimeError("未配置可用的 LLM 提供商")
        prompt = "请根据这些视频关键画面评价视频。" + self._preference_prompt() + "严格用一句1到10个字的中文短评，可用🐛表示不好看。"
        system_prompt = self.config.get("system_prompt", "你是一个视频评价助手。用一句话评价视频（1-10个字，可以用🐛表示不好看）")
        response = await self.context.llm_generate(
            chat_provider_id=provider.meta().id,
            prompt=prompt,
            image_urls=[str(path) for path in frames],
            system_prompt=system_prompt,
        )
        return (getattr(response, "completion_text", "") or "这个视频我暂时无法判断好不好看。").strip()

    async def _download_bili_video(self, url: str) -> tuple[Path, dict[str, Any]]:
        import imageio_ffmpeg
        import yt_dlp

        output = self.work_dir / "bilibili_%(id)s.%(ext)s"
        options = {
            "format": "bv*+ba/b",
            "outtmpl": str(output),
            "merge_output_format": "mp4",
            "ffmpeg_location": imageio_ffmpeg.get_ffmpeg_exe(),
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "restrictfilenames": True,
            "http_headers": {
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://www.bilibili.com/",
            },
        }

        def download() -> tuple[Path, dict[str, Any]]:
            with yt_dlp.YoutubeDL(options) as downloader:
                metadata = downloader.extract_info(url, download=True)
                prepared = Path(downloader.prepare_filename(metadata))
                candidates = [prepared, prepared.with_suffix(".mp4")]
                for candidate in candidates:
                    if candidate.exists() and candidate.stat().st_size:
                        return candidate, metadata
                matches = sorted(
                    self.work_dir.glob("bilibili_*"),
                    key=lambda item: item.stat().st_mtime,
                    reverse=True,
                )
                if matches:
                    return matches[0], metadata
                raise FileNotFoundError("yt-dlp 未生成视频文件")

        return await asyncio.to_thread(download)

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.ALL, priority=20)
    async def on_card(self, event: AstrMessageEvent):
        """Only consume Json card messages containing a Bilibili video link."""
        video = next((item for item in event.get_messages() if isinstance(item, Comp.Video)), None)
        if video:
            event.stop_event()
            try:
                video_path = await self._video_path(video)
                review = await self._analyze_video(event, video_path)
                yield event.plain_result(review)
                yield event.chain_result([Comp.Video.fromFileSystem(path=str(video_path))])
            except Exception as exc:
                logger.error("视频评价失败: %s", exc, exc_info=True)
                yield event.plain_result("这个视频暂时无法评价，请稍后再试。")
            return
        url = await self._extract_url(event)
        if not url:
            return
        event.stop_event()
        try:
            metadata = await self._fetch_metadata(url)
            if not self.config.get("analyze_metadata_only", False):
                video_path, _ = await self._download_bili_video(url)
                review = await self._analyze_video(event, video_path)
                yield event.plain_result(review)
                yield event.chain_result([Comp.Video.fromFileSystem(path=str(video_path))])
            else:
                yield event.plain_result(await self._analyze(event, metadata))
        except Exception as exc:
            logger.error("B站卡片评价失败: %s", exc, exc_info=True)
            if "metadata" in locals() and metadata:
                try:
                    yield event.plain_result(await self._analyze(event, metadata))
                    return
                except Exception as fallback_exc:
                    logger.error("B站卡片元数据兜底评价也失败: %s", fallback_exc, exc_info=True)
            yield event.plain_result("这个 B 站卡片暂时无法评价，请稍后再试。")
