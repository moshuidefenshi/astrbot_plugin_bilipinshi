"""Evaluate Bilibili card messages sent in QQ chats."""

from __future__ import annotations

import json
import re
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

    async def initialize(self):
        timeout = float(self.config.get("http_timeout", 30))
        self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout))

    async def terminate(self):
        if self.http and not self.http.closed:
            await self.http.close()

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
        preference = str(self.config.get("review_preference", "neutral")).lower()
        if preference in {"positive", "好评", "偏好好评"}:
            return "评价倾向偏向好评，优先指出视频有趣、精彩、值得看的地方，但不要违背明显事实。"
        if preference in {"negative", "差评", "偏好差评"}:
            return "评价倾向偏向差评，优先指出视频无聊、粗糙、标题夸张或不值得看的地方，但不要恶意编造。"
        return "保持中立，根据标题和封面呈现出的信息客观评价。"

    async def _analyze(self, event: AstrMessageEvent, metadata: dict[str, Any]) -> str:
        provider = self.context.get_using_provider(event.unified_msg_origin)
        if not provider:
            raise RuntimeError("未配置可用的 LLM 提供商")
        title = metadata.get("title") or "未知标题"
        cover = metadata.get("pic") or ""
        if cover.startswith("//"):
            cover = "https:" + cover
        prompt = "只根据下面这个 B 站视频的标题和封面进行评价，不要假装看过完整视频。\n标题：" + title + "\n" + self._preference_prompt() + "\n请用中文回复两句话以内，并明确说好看、不好看或一般。"
        response = await self.context.llm_generate(chat_provider_id=provider.meta().id, prompt=prompt, image_urls=[cover] if cover else [], system_prompt="你是一个简短的视频卡片评价助手，只能依据标题和封面作出有限判断。")
        text = getattr(response, "completion_text", "") or ""
        return text.strip() or "这个视频我暂时无法判断好不好看。"

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.ALL, priority=20)
    async def on_card(self, event: AstrMessageEvent):
        """Only consume Json card messages containing a Bilibili video link."""
        url = await self._extract_url(event)
        if not url:
            return
        event.stop_event()
        try:
            metadata = await self._fetch_metadata(url)
            yield event.plain_result(await self._analyze(event, metadata))
        except Exception as exc:
            logger.error("B站卡片评价失败: %s", exc, exc_info=True)
            yield event.plain_result("这个 B 站卡片暂时无法评价，请稍后再试。")
