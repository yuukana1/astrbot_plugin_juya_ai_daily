import asyncio
import base64
import hashlib
import json
import os
import random
import re
import tempfile
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone as dt_timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import List, Dict, Set, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiohttp

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.event import MessageChain
from astrbot.api import AstrBotConfig
from astrbot.api.star import Context, Star, register
from astrbot.api.star import StarTools
from astrbot.api import logger

from .news_renderer import (
    NEWS_IMAGE_TEMPLATE,
    build_render_data,
    extract_news_items,
)
from .delivery import (
    CircuitBreaker,
    PushResult,
    TargetResult,
    pending_targets,
    retry_delays,
)

# RSS 订阅源配置
RSS_URL = "https://daily.juya.uk/rss.xml"
EMBEDDED_FONT_FILENAME = "LXGWWenKaiLite-Regular.ttf"

@register(
    "astrbot_plugin_juya_ai_daily",
    "yuukana1",
    "订阅橘鸦AI日报，生成全量图片并提供可靠降级投递",
    "1.0.2",
    "https://github.com/yuukana1/astrbot_plugin_juya_ai_daily",
)
class DailyAINewsPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._task: Optional[asyncio.Task] = None
        self._render_lock = asyncio.Lock()
        self._image_download_lock = asyncio.Lock()
        self._image_cache: Dict[str, str] = {}
        self._local_image_cache: Dict[str, str] = {}
        self._image_send_locks: Dict[str, asyncio.Lock] = {}
        self._recent_image_deliveries: Dict[str, float] = {}
        self._recent_manual_image_attempts: Dict[str, float] = {}
        self._http_session: Optional[aiohttp.ClientSession] = None

        # 使用框架规范的数据目录
        self._data_dir = StarTools.get_data_dir("astrbot_plugin_juya_ai_daily")
        self._subscriptions_file = self._data_dir / "subscriptions.json"
        self._sent_file = self._data_dir / "sent_news.json"
        self._delivery_file = self._data_dir / "delivery_state.json"
        self._image_dir = self._data_dir / "rendered_images"

        # 通过指令订阅的 unified_msg_origin 集合
        self._cmd_subscriptions: Set[str] = set()
        # 已推送的日期和链接（分离存储）
        self._sent_dates: Set[str] = set()
        self._sent_links: Set[str] = set()
        self._sent_link_order: List[str] = []
        self._delivery_records: Dict[str, Dict] = {}

        self._rss_etag = ""
        self._rss_last_modified = ""
        self._rss_cached_article: Optional[Dict] = None
        # 某些极简容器未安装 IANA tzdata；固定 +08:00 仍可保证默认时区可用。
        self._timezone = dt_timezone(timedelta(hours=8), "Asia/Shanghai")
        self._send_semaphore = asyncio.Semaphore(3)
        self._render_breaker = CircuitBreaker()
        self._runtime_status = {
            "last_rss_fetch_at": "从未",
            "last_article_date": "无",
            "last_render_error": "无",
            "last_push": "无",
        }
        # 文件读写互斥锁
        self._file_lock = asyncio.Lock()

    async def initialize(self):
        """插件初始化：加载持久化数据，启动定时推送任务。"""
        os.makedirs(self._data_dir, exist_ok=True)
        os.makedirs(self._image_dir, exist_ok=True)
        self._apply_runtime_config()
        self._http_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30),
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (compatible; AstrBot/4.0; "
                    "+https://github.com/AstrBot)"
                )
            },
        )
        await self._load_subscriptions()
        await self._load_sent_news()
        await self._load_delivery_state()
        await self._cleanup_rendered_images()

        self._task = asyncio.create_task(self._schedule_loop())
        logger.info("每日AI资讯推送插件已初始化（全量图片 + 可靠投递模式）")

    def _config_int(self, key: str, default: int, minimum: int, maximum: int) -> int:
        try:
            value = int(self.config.get(key, default))
        except (TypeError, ValueError):
            value = default
        return max(minimum, min(value, maximum))

    def _config_float(
        self, key: str, default: float, minimum: float, maximum: float
    ) -> float:
        try:
            value = float(self.config.get(key, default))
        except (TypeError, ValueError):
            value = default
        return max(minimum, min(value, maximum))

    def _apply_runtime_config(self) -> None:
        timezone_name = str(self.config.get("timezone", "Asia/Shanghai")).strip()
        try:
            self._timezone = ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError):
            logger.warning(f"无效时区 {timezone_name!r}，已回退 Asia/Shanghai")
            self._timezone = dt_timezone(timedelta(hours=8), "Asia/Shanghai")

        concurrency = self._config_int("send_concurrency", 3, 1, 10)
        self._send_semaphore = asyncio.Semaphore(concurrency)
        self._render_breaker = CircuitBreaker(
            threshold=self._config_int(
                "render_circuit_failure_threshold", 3, 1, 10
            ),
            cooldown_seconds=self._config_int(
                "render_circuit_minutes", 30, 1, 1440
            )
            * 60,
        )

    def _now(self) -> datetime:
        return datetime.now(self._timezone)

    def _now_text(self) -> str:
        return self._now().strftime("%Y-%m-%d %H:%M:%S")

    # ==================== 指令处理 ====================

    @filter.command("AI日报")
    async def cmd_ainews(self, event: AstrMessageEvent):
        """手动获取最新 AI 早报"""
        article = await self._fetch_rss_latest()
        if not article:
            yield event.plain_result(
                self._image_failure_notice(
                    self._now().strftime("%Y-%m-%d"), "render"
                )
            )
            return

        # 使用文章实际日期展示；图片与摘要缓存还会包含文章链接/内容指纹。
        article_date = self._parse_article_date(article)

        delivery_key = self._image_delivery_key(
            "manual",
            article.get("link", article_date),
            event.unified_msg_origin,
            self._event_message_id(event),
        )
        send_lock = self._image_send_locks.setdefault(
            delivery_key, asyncio.Lock()
        )
        async with send_lock:
            if self._recent_image_sent(
                delivery_key
            ) or self._recent_manual_image_attempted(delivery_key):
                return

            image = self._get_cached_image(article_date)
            if not image and self.config.get("enable_image_render", True):
                image = await self._render_news_image(article, article_date)
            if image:
                # 手动请求只执行一次图片发送，不在异常后尝试第二张图片；
                # 防止平台已收到消息但客户端抛错时发生重复投递。
                self._remember_manual_image_attempt(delivery_key)
                sent = await self._send_event_image_once(
                    event, image, article_date
                )
                if sent:
                    self._remember_image_sent(delivery_key)
                    return

                # 手动图片发送失败时只提示失败，不重试第二张图片。
                yield event.plain_result(
                    self._image_failure_notice(article_date, "send")
                )
                return

            # 未启用渲染或渲染重试耗尽时不发送 RSS 纯文本。
            yield event.plain_result(
                self._image_failure_notice(article_date, "render")
            )

    @filter.command("AI日报订阅")
    async def cmd_subscribe(self, event: AstrMessageEvent):
        """为当前群聊或私聊订阅每日 AI 资讯推送。"""
        umo = event.unified_msg_origin
        logger.info(f"订阅状态: {umo}")
        if umo in self._cmd_subscriptions:
            yield event.plain_result("📢 当前会话已订阅每日AI资讯推送。")
            return
        self._cmd_subscriptions.add(umo)
        await self._save_subscriptions()
        yield event.plain_result(
            "✅ 订阅成功！每日将自动向当前会话推送完整图片 AI 日报。\n"
            "取消订阅请发送 /AI日报退订"
        )

    @filter.command("AI日报退订")
    async def cmd_unsubscribe(self, event: AstrMessageEvent):
        """取消每日 AI 资讯推送订阅"""
        umo = event.unified_msg_origin
        if umo not in self._cmd_subscriptions:
            yield event.plain_result("ℹ️ 当前会话未通过指令订阅过 AI 资讯推送。")
            return
        self._cmd_subscriptions.discard(umo)
        await self._save_subscriptions()
        yield event.plain_result("✅ 已取消每日AI资讯推送订阅。")

    @filter.command("AI日报状态")
    async def cmd_status(self, event: AstrMessageEvent):
        """查看推送状态"""
        hour = self._config_int("push_hour", 8, 0, 23)
        minute = self._config_int("push_minute", 0, 0, 59)
        poll_interval = self._config_int("rss_poll_interval", 600, 60, 86400)
        image_enabled = (
            "已启用" if self.config.get("enable_image_render", True) else "已关闭"
        )
        current_targets = self._get_all_targets()
        subscription_info = self._format_subscription_status(current_targets)
        pending_count = sum(
            len(pending_targets(record, current_targets))
            for record in self._delivery_records.values()
        )
        if self._render_breaker.open_until:
            until = datetime.fromtimestamp(
                self._render_breaker.open_until, self._timezone
            ).strftime("%Y-%m-%d %H:%M:%S")
            breaker_info = f"熔断至 {until}"
        else:
            breaker_info = f"正常（连续失败 {self._render_breaker.failures} 次）"

        status_text = (
            "📊 **每日AI资讯推送状态**\n"
            f"📡 数据源：RSS 订阅（橘鸦 AI 日报）\n"
            f"⏰ 首次检查时间：每天 {hour:02d}:{minute:02d}\n"
            f"🔄 轮询间隔：{poll_interval} 秒\n"
            f"🌏 调度时区：{self.config.get('timezone', 'Asia/Shanghai')}\n"
            f"🖼️ 图片日报：{image_enabled}\n"
            f"🛡️ 渲染服务：{breaker_info}\n"
            f"{subscription_info}\n"
            f"📚 已推送日期缓存：{len(self._sent_dates)} 天\n"
            f"📚 已推送文章缓存：{len(self._sent_links)} 篇\n"
            f"⏳ 待重试目标：{pending_count} 个\n"
            f"📥 最近 RSS 检查：{self._runtime_status['last_rss_fetch_at']}\n"
            f"🗓️ RSS 最新一期：{self._runtime_status['last_article_date']}\n"
            f"📤 最近投递：{self._runtime_status['last_push']}\n"
            f"⚠️ 最近渲染错误：{self._runtime_status['last_render_error']}"
        )
        yield event.plain_result(status_text)

    # ==================== 定时 + 轮询推送 ====================

    async def _schedule_loop(self):
        """后台定时循环：每天在设定时间首次检查 RSS，若未更新则轮询直到获取到当日文章。"""
        # 启动时先执行一次补偿检查
        await self._startup_compensation_check()

        while True:
            try:
                target_hour = self._config_int("push_hour", 8, 0, 23)
                target_minute = self._config_int("push_minute", 0, 0, 59)
                poll_interval = self._config_int(
                    "rss_poll_interval", 600, 60, 86400
                )

                now = self._now()
                target = now.replace(
                    hour=target_hour, minute=target_minute, second=0, microsecond=0
                )
                if target <= now:
                    target += timedelta(days=1)

                wait_seconds = (target - now).total_seconds()
                logger.info(
                    f"下次 RSS 检查时间：{target.strftime('%Y-%m-%d %H:%M')}，"
                    f"等待 {wait_seconds:.0f} 秒"
                )

                await asyncio.sleep(wait_seconds)

                # 到达设定时间，开始检查 RSS 并尝试推送
                today = self._now().strftime("%Y-%m-%d")

                # 检查今天是否已经推送过
                if today in self._sent_dates:
                    logger.info(f"今日 ({today}) 已推送过，等待明天")
                    continue

                # 首次尝试获取 RSS
                pushed = await self._try_fetch_and_push(today)
                if pushed:
                    continue

                # RSS 尚未更新，进入轮询模式
                logger.info(
                    f"RSS 尚未更新当日 ({today}) 内容，"
                    f"进入轮询模式（间隔 {poll_interval} 秒）"
                )
                while True:
                    await asyncio.sleep(poll_interval)

                    # 如果已经过了当天，停止轮询
                    current_date = self._now().strftime("%Y-%m-%d")
                    if current_date != today:
                        logger.info("已过当天，停止轮询，等待明天定时触发")
                        break

                    pushed = await self._try_fetch_and_push(today)
                    if pushed:
                        break

            except asyncio.CancelledError:
                logger.info("定时推送任务已取消")
                break
            except Exception as e:
                logger.error(f"定时推送任务出错: {e}")
                await asyncio.sleep(60)

    async def _startup_compensation_check(self):
        """启动时补偿检查：若当前已过推送时间且当天未推送过，立即尝试推送。"""
        try:
            target_hour = self._config_int("push_hour", 8, 0, 23)
            target_minute = self._config_int("push_minute", 0, 0, 59)

            now = self._now()
            today = now.strftime("%Y-%m-%d")

            # 只在过了今天的推送时间后才补偿
            target_time = now.replace(
                hour=target_hour, minute=target_minute, second=0, microsecond=0
            )
            if now < target_time:
                logger.info("当前未到推送时间，跳过补偿检查")
                return

            if today in self._sent_dates:
                logger.info(f"今日 ({today}) 已推送过，跳过补偿检查")
                return

            logger.info(f"启动补偿检查：今日 ({today}) 尚未推送，尝试拉取并推送")
            await self._try_fetch_and_push(today)

        except Exception as e:
            logger.error(f"启动补偿检查失败: {e}")

    async def _try_fetch_and_push(self, today: str) -> bool:
        """尝试从 RSS 获取当日文章并推送。返回 True 表示成功推送。"""
        try:
            article = await self._fetch_rss_latest()
            if not article:
                logger.info("RSS 获取失败或无文章")
                return False

            # 解析文章日期
            article_date = self._parse_article_date(article)

            # 检查是否是当日文章
            if article_date != today:
                logger.info(
                    f"RSS 最新文章日期 ({article_date}) 不是今日 ({today})，继续等待"
                )
                return False

            push_result = await self._do_push(article, article_date)
            return push_result.complete

        except Exception as e:
            logger.error(f"尝试获取并推送失败: {e}")
            return False

    async def _do_push(self, article: Dict, article_date: str) -> PushResult:
        """只投递尚未成功的目标，并返回可供调度器判断的真实结果。"""
        logger.info(f"开始执行每日AI资讯推送: {article['title']}")
        targets = self._get_all_targets()
        if not targets:
            logger.info("没有任何推送目标，结束本次检查")
            return PushResult(no_targets=True)

        record = self._delivery_records.setdefault(
            article["link"],
            {
                "date": article_date,
                "title": article.get("title", ""),
                "targets": {},
                "updated_at": self._now_text(),
            },
        )
        todo = pending_targets(record, targets)
        if not todo:
            logger.info("当前所有目标均已成功投递，跳过重复发送")
            return PushResult(no_targets=True)

        image = None
        image_failure_reason = "render"
        if self.config.get("enable_image_render", True):
            image = await self._render_news_image(article, article_date)
            if image:
                image_failure_reason = ""

        async def deliver(umo: str) -> TargetResult:
            async with self._send_semaphore:
                return await self._deliver_target(
                    umo, article, article_date, image, image_failure_reason
                )

        delivered = await asyncio.gather(*(deliver(umo) for umo in todo))
        push_result = PushResult(results={item.target: item for item in delivered})

        for item in delivered:
            previous = record["targets"].get(item.target, {})
            record["targets"][item.target] = {
                "success": item.success,
                "mode": item.mode,
                "attempts": int(previous.get("attempts", 0)) + item.attempts,
                "last_error": item.error,
                "updated_at": self._now_text(),
            }
        record["updated_at"] = self._now_text()
        await self._save_delivery_state()

        remaining = pending_targets(record, targets)
        if not remaining:
            await self._mark_article_complete(article["link"], article_date)
            self._runtime_status["last_push"] = (
                f"{article_date} 全部 {len(targets)} 个目标投递成功"
            )
        else:
            self._runtime_status["last_push"] = (
                f"{article_date} 成功 {push_result.succeeded}，"
                f"仍待重试 {len(remaining)}"
            )
            logger.warning(
                f"本轮仍有 {len(remaining)} 个目标失败，将在下轮只重试失败目标"
            )
        return push_result

    async def _deliver_target(
        self,
        umo: str,
        article: Dict,
        article_date: str,
        image: Optional[str],
        image_failure_reason: str = "render",
    ) -> TargetResult:
        """投递图片；失败后只发送简短的失败提示，不发送日报纯文本。"""
        total_attempts = 0
        last_error = ""
        if image:
            delivery_key = self._image_delivery_key(
                "scheduled", article.get("link", ""), umo
            )
            send_lock = self._image_send_locks.setdefault(
                delivery_key, asyncio.Lock()
            )
            async with send_lock:
                if self._recent_image_sent(delivery_key):
                    logger.info(f"跳过刚刚已发送的重复图片: {umo}")
                    return TargetResult(umo, True, "image_deduped", 0)
                success, attempts, last_error, mode = (
                    await self._send_image_with_retries(
                        umo, image, article_date
                    )
                )
                if success:
                    self._remember_image_sent(delivery_key)
            total_attempts += attempts
            if success:
                logger.info(f"图片日报已推送至: {umo} ({mode})")
                return TargetResult(umo, True, mode, total_attempts)

        notice_kind = "render" if not image else "send"
        if not image_failure_reason:
            image_failure_reason = notice_kind
        notice = self._image_failure_notice(article_date, image_failure_reason)
        success, attempts, text_error = await self._send_chain_with_retry(
            umo,
            lambda: MessageChain().message(notice),
            f"image_{image_failure_reason}_notice",
        )
        total_attempts += attempts
        if success:
            mode = (
                "render_failed_notice"
                if image_failure_reason == "render"
                else "send_failed_notice"
            )
            logger.info(f"图片失败提示已发送至: {umo} ({mode})")
            return TargetResult(umo, True, mode, total_attempts)
        error = text_error or last_error or "失败提示发送错误"
        logger.error(f"图片及失败提示均无法发送至 {umo}: {error}")
        return TargetResult(umo, False, "failed", total_attempts, error)

    async def _send_image_with_retries(
        self, umo: str, image: str, article_date: str
    ) -> tuple[bool, int, str, str]:
        """按配置发送 URL/本地图片；auto 模式会在 URL 失败后改用本地文件。"""
        delivery_mode = str(
            self.config.get("image_delivery_mode", "auto")
        ).lower()
        if delivery_mode not in {"auto", "url", "local"}:
            delivery_mode = "auto"

        total_attempts = 0
        last_error = ""
        is_url = image.startswith(("http://", "https://"))

        if delivery_mode in {"auto", "url"} and is_url:
            success, attempts, last_error = await self._send_chain_with_retry(
                umo, lambda: MessageChain().url_image(image), "image_url"
            )
            total_attempts += attempts
            if success or delivery_mode == "url":
                return success, total_attempts, last_error, "image_url"

        local_image = image if not is_url else await self._ensure_local_image(
            image, article_date
        )
        if local_image:
            success, attempts, local_error = await self._send_chain_with_retry(
                umo, lambda: MessageChain().file_image(local_image), "image_file"
            )
            total_attempts += attempts
            return success, total_attempts, local_error, "image_file"

        return False, total_attempts, last_error or "图片下载到本地失败", "image_file"

    async def _send_chain_with_retry(
        self, umo: str, chain_factory, mode: str
    ) -> tuple[bool, int, str]:
        max_attempts = self._config_int("send_max_retries", 3, 1, 8)
        base_delay = self._config_float("retry_base_delay", 2.0, 0.0, 30.0)
        delays = retry_delays(max_attempts, base_delay)
        last_error = ""
        for index in range(max_attempts):
            try:
                sent = await self.context.send_message(umo, chain_factory())
                if sent:
                    return True, index + 1, ""
                last_error = "AstrBot 未找到匹配平台或平台拒绝主动消息"
            except Exception as e:
                last_error = f"{type(e).__name__}: {e}"
            if index < len(delays):
                jitter = random.uniform(0, min(0.5, base_delay / 4))
                logger.warning(
                    f"{mode} 推送至 {umo} 第 {index + 1} 次失败，"
                    f"{delays[index] + jitter:.1f} 秒后重试: {last_error}"
                )
                await asyncio.sleep(delays[index] + jitter)
        return False, max_attempts, last_error

    @staticmethod
    def _image_failure_notice(article_date: str, kind: str) -> str:
        if kind == "render":
            return (
                f"⚠️ {article_date} AI 日报图片渲染失败，未生成可发送的日报图片。"
                "\n请稍后再试。"
            )
        return (
            f"⚠️ {article_date} AI 日报图片发送失败，未成功投递日报图片。"
            "\n请稍后再试。"
        )

    async def _send_event_image_once(
        self, event: AstrMessageEvent, image: str, article_date: str
    ) -> bool:
        """手动请求至多调用一次图片发送，杜绝重试导致的重复图片。"""
        mode = str(self.config.get("image_delivery_mode", "auto")).lower()
        if mode not in {"auto", "url", "local"}:
            mode = "auto"
        is_url = image.startswith(("http://", "https://"))
        selected = image
        if is_url and mode in {"auto", "local"}:
            local_image = await self._ensure_local_image(image, article_date)
            if local_image:
                selected = local_image
                is_url = False
            elif mode == "local":
                return False

        try:
            chain = (
                MessageChain().url_image(selected)
                if is_url
                else MessageChain().file_image(selected)
            )
            await event.send(chain)
            return True
        except Exception as e:
            logger.warning(
                f"手动图片单次发送失败，将发送失败提示: {type(e).__name__}: {e}"
            )
            return False

    @staticmethod
    def _image_delivery_key(
        scope: str,
        article_identity: str,
        umo: str,
        request_identity: str = "",
    ) -> str:
        return hashlib.sha256(
            f"{scope}\n{article_identity}\n{umo}\n{request_identity}".encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _event_message_id(event: AstrMessageEvent) -> str:
        """Use the platform message ID so only the same command event is deduped."""
        message_obj = getattr(event, "message_obj", None)
        message_id = str(getattr(message_obj, "message_id", "") or "").strip()
        return message_id or f"event-object:{id(event)}"

    def _recent_image_sent(self, delivery_key: str) -> bool:
        ttl = self._config_int("manual_dedupe_seconds", 60, 10, 300)
        sent_at = self._recent_image_deliveries.get(delivery_key, 0)
        return bool(sent_at and time.time() - sent_at < ttl)

    def _recent_manual_image_attempted(self, delivery_key: str) -> bool:
        ttl = self._config_int("manual_dedupe_seconds", 60, 10, 300)
        attempted_at = self._recent_manual_image_attempts.get(delivery_key, 0)
        return bool(attempted_at and time.time() - attempted_at < ttl)

    def _remember_manual_image_attempt(self, delivery_key: str) -> None:
        self._recent_manual_image_attempts[delivery_key] = time.time()
        if len(self._recent_manual_image_attempts) > 200:
            oldest = min(
                self._recent_manual_image_attempts,
                key=self._recent_manual_image_attempts.get,
            )
            self._recent_manual_image_attempts.pop(oldest, None)
        self._prune_image_send_locks()

    def _remember_image_sent(self, delivery_key: str) -> None:
        self._recent_image_deliveries[delivery_key] = time.time()
        if len(self._recent_image_deliveries) > 200:
            oldest = min(
                self._recent_image_deliveries,
                key=self._recent_image_deliveries.get,
            )
            self._recent_image_deliveries.pop(oldest, None)

        self._prune_image_send_locks()

    def _prune_image_send_locks(self) -> None:
        """Bound per-target lock state without removing a lock that is in use."""
        if len(self._image_send_locks) <= 300:
            return
        protected = set(self._recent_image_deliveries)
        protected.update(self._recent_manual_image_attempts)
        for key, lock in list(self._image_send_locks.items()):
            if len(self._image_send_locks) <= 200:
                break
            if key not in protected and not lock.locked():
                self._image_send_locks.pop(key, None)

    def _get_cached_image(self, article_date: str) -> Optional[str]:
        """按日期查找已渲染的本地缓存图片（内存缓存优先，磁盘兜底）。"""
        for key, path in self._image_cache.items():
            if key.startswith(article_date + ":"):
                if path.startswith(("http://", "https://")) or os.path.exists(path):
                    return path
        matches = list(self._image_dir.glob(f"{article_date}_*.jpg"))
        if matches:
            return str(max(matches, key=lambda item: item.stat().st_mtime))
        return None

    async def _ensure_local_image(
        self, image_url: str, article_date: str
    ) -> Optional[str]:
        if image_url in self._local_image_cache:
            path = self._local_image_cache[image_url]
            if os.path.exists(path):
                return path

        session = self._http_session
        if session is None or session.closed:
            return None
        digest = hashlib.sha256(image_url.encode("utf-8")).hexdigest()[:16]
        target = Path(self._image_dir) / f"{article_date}_{digest}.jpg"
        if target.exists():
            self._local_image_cache[image_url] = str(target)
            return str(target)

        async with self._image_download_lock:
            if target.exists():
                self._local_image_cache[image_url] = str(target)
                return str(target)
            try:
                async with session.get(image_url) as response:
                    if response.status != 200:
                        raise RuntimeError(f"HTTP {response.status}")
                    content_type = response.headers.get("Content-Type", "")
                    if content_type and not content_type.startswith("image/"):
                        raise RuntimeError(f"非图片响应: {content_type}")
                    data = await response.read()
                    if not data or len(data) > 15 * 1024 * 1024:
                        raise RuntimeError("图片为空或超过 15MB 限制")
                await asyncio.to_thread(self._atomic_write_bytes, target, data)
                self._local_image_cache[image_url] = str(target)
                return str(target)
            except Exception as e:
                logger.error(f"下载渲染图片失败: {type(e).__name__}: {e}")
                return None

    async def _mark_article_complete(self, link: str, article_date: str) -> None:
        self._sent_dates.add(article_date)
        if link not in self._sent_links:
            self._sent_links.add(link)
            self._sent_link_order.append(link)
        self._sent_dates = set(sorted(self._sent_dates)[-30:])
        self._sent_link_order = self._sent_link_order[-100:]
        self._sent_links = set(self._sent_link_order)
        await self._save_sent_news()
        logger.info(f"{article_date} 已对全部当前目标完成投递")

    @staticmethod
    def _embedded_font_path() -> Path:
        return (
            Path(__file__).resolve().parent
            / "assets"
            / EMBEDDED_FONT_FILENAME
        )

    async def _load_render_font_data(self) -> str:
        """按需读取内置字体；字体缺失时返回空值并回退系统字体。"""
        font_path = self._embedded_font_path()
        if not font_path.is_file():
            return ""

        try:
            font_bytes = await asyncio.to_thread(font_path.read_bytes)
            if not font_bytes.startswith((b"\x00\x01\x00\x00", b"OTTO")):
                raise ValueError("文件不是有效的 TTF/OTF 字体")
            return base64.b64encode(font_bytes).decode("ascii")
        except Exception as e:
            logger.warning(
                f"内置霞鹜文楷加载失败，将使用系统字体: "
                f"{type(e).__name__}: {e}"
            )
            return ""

    async def _render_news_image(
        self, article: Dict, article_date: str
    ) -> Optional[str]:
        """用 AstrBot 官方 HTML 渲染服务生成覆盖全部 RSS 更新的一张长图。"""
        if not self.config.get("enable_image_render", True):
            return None

        cache_key = f"{article_date}:{article.get('link', '')}"
        if cache_key in self._image_cache:
            return self._image_cache[cache_key]

        now_ts = time.time()
        if not self._render_breaker.allow(now_ts):
            open_until = datetime.fromtimestamp(
                self._render_breaker.open_until, self._timezone
            ).strftime("%H:%M:%S")
            logger.warning(f"图片渲染熔断中（至 {open_until}），本次跳过图片并发送失败提示")
            return None

        async with self._render_lock:
            # 双重检查，避免并发手动请求重复渲染。
            if cache_key in self._image_cache:
                return self._image_cache[cache_key]
            if not self._render_breaker.allow(time.time()):
                return None

            # 不设条目上限，RSS 当期的全部更新合并为一张长图。
            items = extract_news_items(article.get("raw_content", ""))
            if not items:
                logger.warning("RSS 中未提取到可渲染的新闻条目")
                return None

            try:
                quality = int(self.config.get("image_quality", 90))
            except (TypeError, ValueError):
                quality = 90
            quality = max(70, min(quality, 95))

            options = {
                "type": "jpeg",
                "quality": quality,
                "full_page": True,
                "animations": "disabled",
                "caret": "hide",
                "scale": "css",
            }

            render_data = build_render_data(
                article_date=article_date,
                items=items,
                source_url=article.get("link", ""),
                now=self._now(),
                total_count=len(items),
            )
            render_data["font_data"] = await self._load_render_font_data()
            max_attempts = self._config_int("render_max_retries", 3, 1, 8)
            base_delay = self._config_float("retry_base_delay", 2.0, 0.0, 30.0)
            timeout = self._config_int("render_timeout", 45, 10, 120)
            delays = retry_delays(max_attempts, base_delay)
            image = None
            last_error = ""
            for index in range(max_attempts):
                try:
                    image = await asyncio.wait_for(
                        self.html_render(
                            NEWS_IMAGE_TEMPLATE,
                            render_data,
                            return_url=True,
                            options=options,
                        ),
                        timeout=timeout,
                    )
                    if image:
                        break
                    last_error = "渲染服务返回空结果"
                except Exception as e:
                    last_error = f"{type(e).__name__}: {e}"
                if index < len(delays):
                    jitter = random.uniform(0, min(0.5, base_delay / 4))
                    logger.warning(
                        f"全量图片第 {index + 1} 次渲染失败，"
                        f"{delays[index] + jitter:.1f} 秒后重试: {last_error}"
                    )
                    await asyncio.sleep(delays[index] + jitter)

            if not image:
                self._render_breaker.record_failure(time.time())
                self._runtime_status["last_render_error"] = last_error or "未知错误"
                logger.error(f"图片渲染重试耗尽，将发送渲染失败提示: {last_error}")
                return None

            self._render_breaker.record_success()
            self._runtime_status["last_render_error"] = "无"

            self._image_cache[cache_key] = image
            # 仅保留最近三期的进程内 URL，防止常驻进程无限增长。
            while len(self._image_cache) > 3:
                self._image_cache.pop(next(iter(self._image_cache)))
            return image

    # ==================== RSS 获取 ====================

    async def _fetch_rss_latest(self) -> Optional[Dict]:
        """从 RSS 订阅源获取最新一篇文章。"""
        try:
            headers = {
                "Accept": "application/rss+xml, application/xml, text/xml, */*",
            }
            if self._rss_etag:
                headers["If-None-Match"] = self._rss_etag
            if self._rss_last_modified:
                headers["If-Modified-Since"] = self._rss_last_modified

            session = self._http_session
            if session is None or session.closed:
                logger.error("RSS HTTP 会话尚未初始化")
                return None
            async with session.get(RSS_URL, headers=headers) as resp:
                self._runtime_status["last_rss_fetch_at"] = self._now_text()
                if resp.status == 304:
                    return self._rss_cached_article
                if resp.status != 200:
                    logger.warning(f"RSS 请求返回状态码 {resp.status}")
                    return None
                self._rss_etag = resp.headers.get("ETag", self._rss_etag)
                self._rss_last_modified = resp.headers.get(
                    "Last-Modified", self._rss_last_modified
                )
                xml_text = await resp.text()

            # 解析 RSS XML
            root = ET.fromstring(xml_text)
            channel = root.find("channel")
            if channel is None:
                logger.warning("RSS XML 中未找到 channel 元素")
                return None

            # 获取第一个 item（最新文章）
            item = channel.find("item")
            if item is None:
                logger.warning("RSS 中没有任何文章")
                return None

            title = item.findtext("title", "").strip()
            link = item.findtext("link", "").strip()
            description = item.findtext("description", "").strip()
            encoded_content = item.findtext(
                "{http://purl.org/rss/1.0/modules/content/}encoded", ""
            ).strip()
            pub_date = item.findtext("pubDate", "").strip()

            if not title:
                logger.warning("RSS 文章标题为空")
                return None

            # 优先使用 content:encoded 完整正文，缺失时回退到 description 摘要
            content = self._clean_html(encoded_content or description)

            logger.info(f"RSS 获取到最新文章：{title}")
            article = {
                "title": title,
                "link": link,
                "content": content,
                "raw_content": encoded_content or description,
                "pub_date": pub_date,
            }
            self._rss_cached_article = article
            self._runtime_status["last_article_date"] = self._parse_article_date(
                article
            )
            return article

        except ET.ParseError as e:
            logger.error(f"RSS XML 解析失败: {e}")
        except Exception as e:
            logger.error(f"RSS 获取失败: {type(e).__name__}: {e!r}")

        return None

    def _parse_article_date(self, article: Dict) -> str:
        """从文章中解析日期，优先使用标题日期，回退 pubDate，最后使用当天日期。"""
        # 优先：从标题提取 YYYY-MM-DD（橘鸦 AI 日报标题即日期）
        title = article.get("title", "")
        match = re.search(r"(\d{4}-\d{2}-\d{2})", title)
        if match:
            return match.group(1)

        # 回退：解析 pubDate（RFC 2822 格式）
        pub_date = article.get("pub_date", "")
        if pub_date:
            try:
                dt = parsedate_to_datetime(pub_date)
                return dt.strftime("%Y-%m-%d")
            except Exception:
                pass

        # 最后回退：当天日期
        return self._now().strftime("%Y-%m-%d")

    # ==================== 工具方法 ====================

    def _clean_html(self, text: str) -> str:
        """去除 HTML 标签，转为纯文本。"""
        if not text:
            return ""
        clean = re.sub(r"<[^>]+>", "", text)
        clean = clean.replace("&nbsp;", " ").replace("&amp;", "&")
        clean = clean.replace("&lt;", "<").replace("&gt;", ">")
        clean = clean.replace("&quot;", '"')
        clean = re.sub(r"\n{3,}", "\n\n", clean)
        return clean.strip()

    def _get_config_groups(self) -> List[str]:
        """从配置中获取手动填写的群聊列表（支持 {机器人名称}:{群聊ID} 格式或直接填纯数字）。"""
        groups_text = self.config.get("subscribed_groups", "")
        if not groups_text or not groups_text.strip():
            return []
        return [g.strip() for g in groups_text.strip().split("\n") if g.strip()]

    def _get_config_users(self) -> List[str]:
        """从配置中获取手动填写的私聊账号列表（支持 {机器人名称}:{私聊账号ID} 格式或直接填纯数字）。"""
        users_text = self.config.get("subscribed_users", "")
        if not users_text or not users_text.strip():
            return []
        return [u.strip() for u in users_text.strip().split("\n") if u.strip()]

    def _get_config_targets(self) -> Set[str]:
        """将配置中的群聊和私聊账号转换为统一会话标识。"""
        targets: Set[str] = set()
        cfg_groups = self._get_config_groups()
        for group_id in cfg_groups:
            parts = group_id.split(":")
            if len(parts) == 2:
                umo = f"{parts[0]}:GroupMessage:{parts[1]}"
                targets.add(umo)
            elif len(parts) == 3:
                targets.add(group_id)
            else:
                umo = f"default:GroupMessage:{group_id}"
                targets.add(umo)

        cfg_users = self._get_config_users()
        for user_id in cfg_users:
            parts = user_id.split(":")
            if len(parts) == 2:
                umo = f"{parts[0]}:FriendMessage:{parts[1]}"
                targets.add(umo)
            elif len(parts) == 3:
                targets.add(user_id)
            else:
                umo = f"default:FriendMessage:{user_id}"
                targets.add(umo)

        return targets

    def _get_all_targets(self) -> Set[str]:
        """获取配置订阅与指令订阅合并去重后的全部推送目标。"""
        return set(self._cmd_subscriptions) | self._get_config_targets()

    def _format_subscription_status(self, targets: Set[str]) -> str:
        """按会话类型列出订阅目标、机器人和订阅来源。"""
        configured_targets = self._get_config_targets()
        groups: List[str] = []
        users: List[str] = []
        others: List[str] = []

        for umo in sorted(targets):
            sources = []
            if umo in self._cmd_subscriptions:
                sources.append("指令")
            if umo in configured_targets:
                sources.append("配置")
            source_text = "、".join(sources) or "未知"

            parts = umo.split(":", 2)
            if len(parts) != 3:
                others.append(f"  • {umo}（来源：{source_text}）")
                continue

            bot_name, message_type, target_id = parts
            detail = f"机器人：{bot_name}；来源：{source_text}"
            if message_type == "GroupMessage":
                groups.append(f"  • 群号 {target_id}（{detail}）")
            elif message_type == "FriendMessage":
                users.append(f"  • 用户 {target_id}（{detail}）")
            else:
                others.append(f"  • {umo}（来源：{source_text}）")

        def section(icon: str, title: str, entries: List[str]) -> str:
            header = f"{icon} {title}（{len(entries)}）"
            return f"{header}：\n" + "\n".join(entries) if entries else f"{header}：无"

        sections = [
            section("👥", "群聊订阅", groups),
            section("👤", "私聊订阅", users),
        ]
        if others:
            sections.append(section("🧩", "其他会话订阅", others))
        return "\n".join(sections)

    # ==================== 持久化（带锁 + 原子写）====================

    def _atomic_write(self, filepath, data: dict):
        """原子写入 JSON 文件：先写临时文件，再 rename 替换。"""
        dir_path = os.path.dirname(str(filepath))
        try:
            fd, tmp_path = tempfile.mkstemp(dir=dir_path, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                os.replace(tmp_path, str(filepath))
            except Exception:
                # 清理临时文件
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except Exception as e:
            logger.error(f"原子写入 {filepath} 失败: {e}")
            raise

    @staticmethod
    def _atomic_write_bytes(filepath: Path, data: bytes) -> None:
        """在目标目录内原子落盘图片，避免发送到半写入文件。"""
        filepath.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=str(filepath.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as file:
                file.write(data)
            os.replace(tmp_path, str(filepath))
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    @staticmethod
    def _read_json(filepath: Path) -> Dict:
        with open(filepath, "r", encoding="utf-8") as file:
            data = json.load(file)
        return data if isinstance(data, dict) else {}

    async def _load_subscriptions(self):
        """从文件加载指令订阅列表。"""
        async with self._file_lock:
            try:
                filepath = str(self._subscriptions_file)
                if os.path.exists(filepath):
                    data = await asyncio.to_thread(
                        self._read_json, self._subscriptions_file
                    )
                    self._cmd_subscriptions = set(data.get("subscriptions", []))
                    logger.info(f"已加载 {len(self._cmd_subscriptions)} 个指令订阅")
            except Exception as e:
                logger.error(f"加载订阅列表失败: {e}")
                self._cmd_subscriptions = set()

    async def _save_subscriptions(self):
        """将指令订阅列表保存到文件。"""
        async with self._file_lock:
            try:
                await asyncio.to_thread(
                    self._atomic_write,
                    self._subscriptions_file,
                    {"subscriptions": sorted(self._cmd_subscriptions)},
                )
            except Exception as e:
                logger.error(f"保存订阅列表失败: {e}")

    async def _load_sent_news(self):
        """加载已推送记录。"""
        async with self._file_lock:
            try:
                filepath = str(self._sent_file)
                if os.path.exists(filepath):
                    data = await asyncio.to_thread(self._read_json, self._sent_file)
                    # 兼容旧格式：如果是旧的 sent_ids 格式，自动迁移
                    if "sent_ids" in data:
                        old_ids = set(data.get("sent_ids", []))
                        for item in old_ids:
                            if re.match(r"\d{4}-\d{2}-\d{2}$", item):
                                self._sent_dates.add(item)
                            else:
                                self._sent_links.add(item)
                                self._sent_link_order.append(item)
                        logger.info("已从旧格式迁移已推送记录")
                    else:
                        self._sent_dates = set(data.get("sent_dates", []))
                        self._sent_link_order = list(
                            dict.fromkeys(data.get("sent_links", []))
                        )
                        self._sent_links = set(self._sent_link_order)
                    logger.info(
                        f"已加载 {len(self._sent_dates)} 个已推送日期，"
                        f"{len(self._sent_links)} 个已推送链接"
                    )
            except Exception as e:
                logger.error(f"加载已推送记录失败: {e}")
                self._sent_dates = set()
                self._sent_links = set()
                self._sent_link_order = []

    async def _save_sent_news(self):
        """保存已推送记录。"""
        async with self._file_lock:
            try:
                await asyncio.to_thread(
                    self._atomic_write,
                    self._sent_file,
                    {
                        "sent_dates": sorted(self._sent_dates),
                        "sent_links": self._sent_link_order,
                    },
                )
            except Exception as e:
                logger.error(f"保存已推送记录失败: {e}")

    async def _load_delivery_state(self) -> None:
        """加载每篇文章、每个目标的独立投递状态。"""
        async with self._file_lock:
            try:
                if self._delivery_file.exists():
                    data = await asyncio.to_thread(
                        self._read_json, self._delivery_file
                    )
                    records = data.get("records", {})
                    if isinstance(records, dict):
                        self._delivery_records = records
                logger.info(f"已加载 {len(self._delivery_records)} 条投递状态")
            except Exception as e:
                logger.error(f"加载投递状态失败: {e}")
                self._delivery_records = {}

    async def _save_delivery_state(self) -> None:
        async with self._file_lock:
            try:
                # 低配服务器只保留最近 30 篇，足够故障恢复且不会无限增长。
                ordered = sorted(
                    self._delivery_records.items(),
                    key=lambda item: item[1].get("updated_at", ""),
                )[-30:]
                self._delivery_records = dict(ordered)
                await asyncio.to_thread(
                    self._atomic_write,
                    self._delivery_file,
                    {"version": 1, "records": self._delivery_records},
                )
            except Exception as e:
                logger.error(f"保存投递状态失败: {e}")

    async def _cleanup_rendered_images(self) -> None:
        """仅清理插件图片目录中超过保留期的 JPEG，不递归操作其他路径。"""
        keep_days = self._config_int("image_cache_days", 7, 1, 30)
        cutoff = time.time() - keep_days * 86400

        def cleanup() -> int:
            removed = 0
            for path in self._image_dir.glob("*.jpg"):
                try:
                    if path.is_file() and path.stat().st_mtime < cutoff:
                        path.unlink()
                        removed += 1
                except OSError as e:
                    logger.warning(f"清理图片缓存失败 {path.name}: {e}")
            return removed

        removed = await asyncio.to_thread(cleanup)
        if removed:
            logger.info(f"已清理 {removed} 张过期日报图片")

    async def terminate(self):
        """插件卸载时取消定时任务。"""
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._http_session and not self._http_session.closed:
            await self._http_session.close()
        logger.info("每日AI资讯推送插件已停用")
