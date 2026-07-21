"""MarketDigestService — periodic market news + outlook digest.

Independent from the trade proposal engine. Runs on its own schedule,
fetches RSS headlines via NewsCollector, optionally adds live prices
via MarketDataService, and optionally calls the LLM for a short market
outlook summary.

The digest is informational — no trade proposals, no approval buttons.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

# ── Digest prompt (free-form text, not JSON schema) ───────────────────

_DIGEST_SYSTEM_PROMPT = """\
You are a market analyst writing a brief daily market digest for a
retail Forex trader. Write in a clear, professional tone.

Given the headlines and price data below, write a SHORT market outlook:
- 3-5 sentences maximum
- Mention the key drivers from the headlines
- Note any notable price moves or levels
- Flag any upcoming high-impact events if mentioned
- Keep it actionable but concise

Do NOT make trade recommendations. Just summarize what's happening.\
"""


class MarketDigestService:
    """Background asyncio loop that sends periodic market digests.

    On each tick:
      1. Fetch headlines from RSS via NewsCollector
      2. Optionally fetch live prices for configured symbols
      3. Optionally call LLM for a market outlook paragraph
      4. Render and send the digest to Telegram

    Also exposed as a callable for on-demand /digest commands.
    """

    def __init__(
        self,
        *,
        llm_provider,  # LLMProvider instance (or None)
        market_data_service,  # MarketDataService instance (or None)
        news_collector,  # NewsCollector instance
        telegram_app,  # telegram.ext.Application
        user_telegram_id: int,
        interval_minutes: int,
        include_prices: bool,
        use_llm: bool,
        symbols: list[str],
    ) -> None:
        self._llm = llm_provider
        self._market_data = market_data_service
        self._news = news_collector
        self._telegram = telegram_app
        self._user_id = user_telegram_id
        self._interval = interval_minutes
        self._include_prices = include_prices
        self._use_llm = use_llm and llm_provider is not None
        self._symbols = symbols

        self._task: asyncio.Task | None = None
        self._running = False

        logger.info(
            "market_digest_init",
            interval_minutes=interval_minutes,
            include_prices=include_prices,
            use_llm=self._use_llm,
            symbols=symbols,
        )

    # ── Lifecycle ─────────────────────────────────────────────────────

    def start(self) -> None:
        """Begin the background digest loop."""
        if self._task is not None:
            logger.warning("market_digest_already_running")
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("market_digest_started", interval_minutes=self._interval)

    async def stop(self) -> None:
        """Gracefully stop the background loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("market_digest_stopped")

    # ── Core Loop ─────────────────────────────────────────────────────

    async def _run_loop(self) -> None:
        """Main loop — runs every ``_interval`` minutes."""
        while self._running:
            try:
                await self.tick()
            except Exception:
                logger.exception("market_digest_tick_error")
            await asyncio.sleep(self._interval * 60)

    async def tick(self) -> None:
        """One cycle of the digest pipeline. Also callable on-demand."""
        # 1. Fetch headlines
        headlines: list[str] = []
        if self._news:
            try:
                headlines = await self._news.fetch(symbols=self._symbols)
            except Exception:
                logger.debug("digest_news_fetch_failed")

        if not headlines:
            headlines = ["No recent headlines available."]

        # 2. Fetch prices (optional)
        prices: dict[str, dict] = {}
        if self._include_prices and self._market_data and self._market_data.is_configured:
            for sym in self._symbols:
                try:
                    snap = await self._market_data.fetch_snapshot(sym)
                    if "error" not in snap and ("bid" in snap or "price" in snap):
                        prices[sym] = snap
                except Exception:
                    logger.debug("digest_price_fetch_failed", symbol=sym)

        # 3. LLM summary (optional)
        llm_summary = None
        if self._use_llm and self._llm is not None:
            try:
                llm_summary = await self._generate_summary(headlines, prices)
            except Exception as exc:
                logger.warning("digest_llm_failed", error=str(exc))

        # 4. Render and send
        text = self._render(headlines, prices, llm_summary)
        await self._send(text)

    # ── LLM Summary ──────────────────────────────────────────────────

    async def _generate_summary(
        self,
        headlines: list[str],
        prices: dict[str, dict],
    ) -> str | None:
        """Call the LLM for a short market outlook paragraph."""
        parts = [f"Current time (UTC): {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}\n"]

        parts.append("## Recent Headlines")
        for h in headlines:
            parts.append(f"- {h}")
        parts.append("")

        if prices:
            parts.append("## Current Prices")
            for sym, snap in prices.items():
                bid = snap.get("bid") or snap.get("price", "?")
                ask = snap.get("ask", "")
                if ask:
                    parts.append(f"- {sym}: bid={bid} ask={ask}")
                else:
                    parts.append(f"- {sym}: {bid}")
            parts.append("")

        user_prompt = "\n".join(parts)

        response = await self._llm.chat_completion(
            system_prompt=_DIGEST_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            temperature=0.5,
            max_tokens=300,
            response_format_type=None,  # free-form text
        )

        return response.text.strip() if response.text else None

    # ── Render ────────────────────────────────────────────────────────

    @staticmethod
    def _render(
        headlines: list[str],
        prices: dict[str, dict],
        llm_summary: str | None,
    ) -> str:
        """Build the Telegram-formatted digest message."""
        now = datetime.now(timezone.utc)
        lines = [
            f"📰 *Market Digest* — {now.strftime('%H:%M UTC')}",
            "",
            "━━━━━━━━━━━━━━━━━━━━━━━━━",
        ]

        # LLM summary first (if available)
        if llm_summary:
            lines.extend([
                "",
                "🧠 *Market Outlook*",
                llm_summary,
                "",
            ])

        # Headlines
        lines.append("📋 *Headlines*")
        for i, h in enumerate(headlines, 1):
            lines.append(f"{i}. {h}")
        lines.append("")

        # Prices
        if prices:
            lines.append("💹 *Prices*")
            for sym, snap in prices.items():
                bid = snap.get("bid") or snap.get("price", "?")
                ask = snap.get("ask")
                if ask:
                    lines.append(f"  {sym}: {bid} / {ask}")
                else:
                    lines.append(f"  {sym}: {bid}")
            lines.append("")

        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━")
        return "\n".join(lines)

    # ── Send ──────────────────────────────────────────────────────────

    async def _send(self, text: str) -> None:
        """Send the digest message to Telegram."""
        if self._telegram is None:
            logger.warning("digest_telegram_unavailable")
            return
        try:
            await self._telegram.bot.send_message(
                chat_id=self._user_id,
                text=text,
            )
            logger.info("digest_sent")
        except Exception as exc:
            logger.error("digest_send_failed", error=str(exc))
