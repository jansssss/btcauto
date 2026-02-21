"""
main.py - 업비트 코인 자동매매 메인

거래 원칙:
  1. 시장 리더 코인 자동 선별 (거래량/모멘텀/상대강도/유동성)
  2. 손절: 매수가 대비 -10% 도달 시 즉시 매도
  3. 이익실현: 고점 대비 -10% 하락 시 매도 (트레일링 스톱, +5% 수익 후 활성)
  4. 진입 타이밍: EMA정배열 + MACD + RSI + ADX + 볼린저 + 거래량 복합 채점 (70점↑)

실행:
  python main.py             # DRY_RUN은 .env 설정 따름
  DRY_RUN=true python main.py
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime
from typing import Optional

import schedule

from config import CONFIG, setup_logging
from leader_scanner import scan_market_leaders, fetch_ohlcv
from entry_engine import should_enter
from position_manager import PositionManager
from order_executor import OrderExecutor
from exit_monitor import run_exit_monitor

logger = setup_logging()

# ── 전역 인스턴스 ─────────────────────────────────────────────────────────────
position_mgr = PositionManager()
executor = OrderExecutor()
_stop_event = threading.Event()


# ── 주문 헬퍼 ────────────────────────────────────────────────────────────────

def _sell_fn(ticker: str, quantity: float,
             current_price: float) -> Optional[float]:
    """exit_monitor에서 호출하는 매도 콜백"""
    result = executor.sell(ticker, quantity, current_price)
    logger.info("%s", result)
    return result.price if result.success else None


# ── 핵심 태스크 ───────────────────────────────────────────────────────────────

def scan_and_trade() -> None:
    """
    [5분 주기] 시장 스캔 → 진입 분석 → 매수 실행.

    흐름:
      1. 포지션 여유 및 KRW 잔고 확인
      2. 시장 리더 스캔
      3. 각 리더 코인 기술적 분석 (진입 점수 채점)
      4. 70점 이상 + 미보유 종목 → ATR 기반 사이징 → 매수
    """
    logger.info("━" * 60)
    logger.info("[%s] 시장 스캔 시작", datetime.now().strftime("%H:%M:%S"))
    position_mgr.print_summary()

    if position_mgr.is_full():
        logger.info("포지션 꽉 참 (%d/%d) - 스캔 건너뜀",
                    position_mgr.count, CONFIG.MAX_CONCURRENT_POSITIONS)
        return

    total_portfolio = executor.get_total_portfolio_krw()
    krw_balance = executor.get_krw_balance()

    if krw_balance < CONFIG.MIN_ORDER_KRW:
        logger.warning("KRW 잔고 부족: %.0f원", krw_balance)
        return

    # ── 리더 코인 스캔 ───────────────────────────────────────────────────
    leaders = scan_market_leaders()
    if not leaders:
        logger.info("리더 코인 없음 - 대기")
        return

    entered = 0
    for leader in leaders:
        if position_mgr.is_full():
            break

        ticker = leader["ticker"]
        if position_mgr.has_position(ticker):
            continue

        # ── 기술적 진입 분석 ──────────────────────────────────────────
        df = fetch_ohlcv(ticker)
        if df is None:
            continue

        entry = should_enter(ticker, df, leader["composite_score"])
        if not entry["enter"]:
            continue

        # ── 포지션 사이징 ──────────────────────────────────────────────
        size_krw = position_mgr.calc_position_size(
            ticker=ticker,
            current_price=entry["entry_price"],
            atr=entry["indicators"].get("atr", 0),
            total_portfolio_krw=total_portfolio,
            combined_confidence=entry["combined_confidence"],
        )

        if size_krw <= 0:
            continue

        # 잔고 초과 방지 (95% 이하 사용)
        size_krw = min(size_krw, krw_balance * 0.95)
        if size_krw < CONFIG.MIN_ORDER_KRW:
            logger.info("%s 잔고 부족으로 매수 스킵", ticker)
            continue

        # ── 매수 실행 ──────────────────────────────────────────────────
        logger.info(
            "[진입 결정] %s | 점수=%d 신뢰도=%.1f 매수금액=%.0f원",
            ticker, entry["entry_score"], entry["combined_confidence"], size_krw,
        )
        order = executor.buy(ticker, size_krw, entry["entry_price"])
        logger.info("%s", order)

        if order.success:
            position_mgr.open_position(
                ticker=ticker,
                entry_price=order.price,
                quantity=order.quantity,
                invested_krw=order.amount_krw,
                entry_score=entry["entry_score"],
                leader_score=entry["leader_score"],
            )
            krw_balance -= order.amount_krw
            entered += 1
            time.sleep(0.3)

    if entered == 0:
        logger.info("이번 스캔: 진입 없음")
    else:
        logger.info("이번 스캔: %d종목 진입 완료", entered)


# ── 종료 처리 ─────────────────────────────────────────────────────────────────

def _on_signal(signum: int, frame) -> None:
    logger.info("종료 신호 수신 (signal=%d)", signum)
    _stop_event.set()


# ── 메인 ──────────────────────────────────────────────────────────────────────

def main() -> None:
    os.makedirs(CONFIG.STATE_DIR, exist_ok=True)
    os.makedirs(CONFIG.LOG_DIR, exist_ok=True)

    logger.info("=" * 60)
    logger.info("업비트 자동매매 시스템 시작")
    logger.info("모드: %s", "DRY-RUN (모의매매)" if CONFIG.DRY_RUN else "LIVE (실거래)")
    logger.info("손절: %.0f%% | 트레일링: %.0f%% (활성화: +%.0f%%)",
                CONFIG.STOP_LOSS_RATE * 100,
                CONFIG.TRAILING_STOP_RATE * 100,
                CONFIG.TRAILING_ACTIVATION_RATE * 100)
    logger.info("최대 포지션: %d개 | 스캔 주기: %ds | Exit 체크: %ds",
                CONFIG.MAX_CONCURRENT_POSITIONS,
                CONFIG.SCAN_INTERVAL_SECONDS,
                CONFIG.EXIT_CHECK_INTERVAL_SECONDS)
    logger.info("=" * 60)

    try:
        CONFIG.validate()
    except ValueError as e:
        logger.critical("설정 오류: %s", e)
        sys.exit(1)

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    # Exit 모니터 백그라운드 스레드 시작
    monitor_thread = threading.Thread(
        target=run_exit_monitor,
        args=(position_mgr, _sell_fn, _stop_event),
        daemon=True,
        name="ExitMonitor",
    )
    monitor_thread.start()
    logger.info("Exit 모니터 스레드 시작 완료")

    # 스케줄 등록
    schedule.every(CONFIG.SCAN_INTERVAL_SECONDS).seconds.do(scan_and_trade)
    logger.info("스케줄 등록 완료 (시장 스캔: %ds 주기)", CONFIG.SCAN_INTERVAL_SECONDS)

    # 시작 즉시 1회 실행
    logger.info("초기 스캔 실행...")
    scan_and_trade()

    # 메인 루프
    logger.info("메인 루프 시작 (Ctrl+C로 종료)")
    while not _stop_event.is_set():
        schedule.run_pending()
        time.sleep(1)

    logger.info("시스템 정상 종료")


if __name__ == "__main__":
    main()
