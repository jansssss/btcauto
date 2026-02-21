"""
exit_monitor.py - 손절/트레일링 스톱 모니터 (백그라운드 스레드)

5초마다 전체 보유 포지션의 현재가를 조회하여
손절 또는 트레일링 스톱 조건 충족 시 즉시 매도 실행.
"""
from __future__ import annotations

import logging
import time
import threading
from typing import Callable, Optional

import pyupbit

from config import CONFIG
from position_manager import PositionManager

logger = logging.getLogger("trader.exit_monitor")


def _fetch_prices(tickers: list[str]) -> dict[str, float]:
    """현재가 일괄 조회"""
    try:
        prices = pyupbit.get_current_price(tickers)
        if isinstance(prices, (int, float)):
            return {tickers[0]: float(prices)}
        if isinstance(prices, dict):
            return {k: float(v) for k, v in prices.items() if v is not None}
    except Exception as e:
        logger.error("현재가 조회 실패: %s", e)
    return {}


def run_exit_monitor(
    position_mgr: PositionManager,
    sell_fn: Callable[[str, float, float], Optional[float]],
    stop_event: threading.Event,
) -> None:
    """
    백그라운드 모니터링 루프.

    Args:
        position_mgr: PositionManager 인스턴스
        sell_fn: sell_fn(ticker, quantity, current_price) -> exit_price | None
        stop_event: 외부에서 종료 신호를 보내는 Event
    """
    logger.info("Exit 모니터 시작 (체크 주기 %ds)", CONFIG.EXIT_CHECK_INTERVAL_SECONDS)

    while not stop_event.is_set():
        try:
            positions = position_mgr.get_all_positions()
            if not positions:
                stop_event.wait(CONFIG.EXIT_CHECK_INTERVAL_SECONDS)
                continue

            tickers = [p.ticker for p in positions]
            prices = _fetch_prices(tickers)

            for pos in positions:
                current_price = prices.get(pos.ticker)
                if current_price is None:
                    logger.warning("%s 현재가 없음 - 체크 건너뜀", pos.ticker)
                    continue

                reason = position_mgr.check_exit(pos, current_price)
                if reason:
                    logger.warning("[청산 시작] %s | %s | 현재가=%.4f",
                                   pos.ticker, reason, current_price)
                    exit_price = sell_fn(pos.ticker, pos.quantity, current_price)
                    actual_price = exit_price if exit_price else current_price
                    summary = position_mgr.close_position(
                        pos.ticker, actual_price, reason
                    )
                    if summary:
                        logger.info(
                            "[청산 완료] %s | PnL=%.2f%% (%.0f원) 보유=%.2fh",
                            summary["ticker"], summary["pnl_rate"],
                            summary["pnl_krw"], summary["hold_hours"],
                        )
                else:
                    pnl = pos.unrealized_pnl_rate(current_price)
                    logger.debug(
                        "%s 보유 중 | 현재=%.4f 수익=%.2f%% 고점=%.4f 손절=%.4f",
                        pos.ticker, current_price, pnl * 100,
                        pos.peak_price, pos.stop_loss_price,
                    )

        except Exception as e:
            logger.error("Exit 모니터 루프 오류: %s", e, exc_info=True)

        stop_event.wait(CONFIG.EXIT_CHECK_INTERVAL_SECONDS)

    logger.info("Exit 모니터 종료")
