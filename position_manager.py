"""
position_manager.py - 포지션 관리

- ATR 기반 포지션 사이징 (신뢰도 보정)
- 손절가 / 트레일링 스톱 추적
- 프로세스 재시작 대비 JSON 영속화
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field, asdict
from typing import Optional

from config import CONFIG

logger = logging.getLogger("trader.position")
STATE_FILE = os.path.join(CONFIG.STATE_DIR, "positions.json")


@dataclass
class Position:
    ticker: str
    entry_price: float
    quantity: float
    entry_time: float           # Unix timestamp
    invested_krw: float
    stop_loss_price: float      # 고정 손절가 (매수가 × 0.90)
    peak_price: float           # 진입 후 최고가 (트레일링 스톱용)
    trailing_active: bool = False
    entry_score: float = 0.0
    leader_score: float = 0.0

    def update_peak(self, current_price: float) -> None:
        """최고가 갱신 및 트레일링 활성화 체크"""
        if current_price > self.peak_price:
            self.peak_price = current_price

        if not self.trailing_active:
            gain = (current_price / self.entry_price) - 1.0
            if gain >= CONFIG.TRAILING_ACTIVATION_RATE:
                self.trailing_active = True
                logger.info(
                    "%s 트레일링 스톱 활성화: +%.1f%% (고점=%.4f)",
                    self.ticker, gain * 100, self.peak_price,
                )

    @property
    def trailing_stop_price(self) -> float:
        """고점 대비 트레일링 스톱가"""
        return self.peak_price * (1.0 + CONFIG.TRAILING_STOP_RATE)

    def unrealized_pnl_rate(self, current_price: float) -> float:
        return (current_price / self.entry_price) - 1.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Position":
        return cls(**data)

    def __repr__(self) -> str:
        return (
            f"Position({self.ticker} | "
            f"entry={self.entry_price:,.0f} peak={self.peak_price:,.0f} "
            f"SL={self.stop_loss_price:,.0f} "
            f"trailing={'ON' if self.trailing_active else 'OFF'})"
        )


class PositionManager:
    """
    포지션 관리자.

    포지션 사이징:
      base_size = (portfolio × MAX_RISK_PER_TRADE) / ATR%
      adjusted  = base_size × confidence_multiplier
      confidence_multiplier: >=85→1.0, >=75→0.75, >=70→0.50

    상한:
      - 단일 종목: 포트폴리오의 20%
      - 총 투자: 포트폴리오의 80%
      - 동시 보유: MAX_CONCURRENT_POSITIONS
    """

    def __init__(self) -> None:
        self._positions: dict[str, Position] = {}
        os.makedirs(CONFIG.STATE_DIR, exist_ok=True)
        self._load()

    # ── 영속화 ────────────────────────────────────────────────────────────────

    def _save(self) -> None:
        data = {t: p.to_dict() for t, p in self._positions.items()}
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    def _load(self) -> None:
        if not os.path.exists(STATE_FILE):
            return
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                data = json.load(f)
            self._positions = {t: Position.from_dict(d) for t, d in data.items()}
            logger.info("포지션 복구 완료: %s", list(self._positions.keys()))
        except Exception as e:
            logger.error("포지션 상태 로드 실패: %s", e)

    # ── 포지션 CRUD ───────────────────────────────────────────────────────────

    def open_position(self, ticker: str, entry_price: float, quantity: float,
                      invested_krw: float, entry_score: float,
                      leader_score: float) -> Position:
        pos = Position(
            ticker=ticker,
            entry_price=entry_price,
            quantity=quantity,
            entry_time=time.time(),
            invested_krw=invested_krw,
            stop_loss_price=entry_price * (1.0 + CONFIG.STOP_LOSS_RATE),
            peak_price=entry_price,
            trailing_active=False,
            entry_score=entry_score,
            leader_score=leader_score,
        )
        self._positions[ticker] = pos
        self._save()
        logger.info(
            "포지션 오픈: %s | 매수가=%.4f 수량=%.8f 투자금=%.0f 손절가=%.4f",
            ticker, entry_price, quantity, invested_krw, pos.stop_loss_price,
        )
        return pos

    def close_position(self, ticker: str, exit_price: float,
                       reason: str) -> Optional[dict]:
        pos = self._positions.pop(ticker, None)
        if pos is None:
            return None

        pnl_rate = pos.unrealized_pnl_rate(exit_price)
        pnl_krw = pos.invested_krw * pnl_rate
        duration_h = (time.time() - pos.entry_time) / 3600

        summary = {
            "ticker": ticker,
            "entry_price": pos.entry_price,
            "exit_price": exit_price,
            "pnl_rate": round(pnl_rate * 100, 2),
            "pnl_krw": round(pnl_krw, 0),
            "invested_krw": pos.invested_krw,
            "peak_price": pos.peak_price,
            "hold_hours": round(duration_h, 2),
            "reason": reason,
        }
        self._save()
        logger.info(
            "포지션 청산: %s | 청산가=%.4f PnL=%.2f%% (%.0f원) 사유=%s",
            ticker, exit_price, pnl_rate * 100, pnl_krw, reason,
        )
        return summary

    def has_position(self, ticker: str) -> bool:
        return ticker in self._positions

    def get_position(self, ticker: str) -> Optional[Position]:
        return self._positions.get(ticker)

    def get_all_positions(self) -> list[Position]:
        return list(self._positions.values())

    def is_full(self) -> bool:
        return len(self._positions) >= CONFIG.MAX_CONCURRENT_POSITIONS

    @property
    def count(self) -> int:
        return len(self._positions)

    # ── 포지션 사이징 ─────────────────────────────────────────────────────────

    def calc_position_size(self, ticker: str, current_price: float, atr: float,
                           total_portfolio_krw: float,
                           combined_confidence: float) -> float:
        """
        ATR 기반 포지션 크기 계산 (KRW).

        Args:
            atr: ATR 값 (가격 단위)
            combined_confidence: 0~100 (리더 40% + 진입점수 60%)

        Returns:
            매수 금액 (KRW). 0이면 진입 불가.
        """
        if self.is_full():
            logger.info("포지션 한도 도달 (%d/%d)", self.count, CONFIG.MAX_CONCURRENT_POSITIONS)
            return 0.0

        if self.has_position(ticker):
            logger.info("%s 이미 보유 중", ticker)
            return 0.0

        total_invested = sum(p.invested_krw for p in self._positions.values())
        available = total_portfolio_krw * CONFIG.MAX_INVESTED_RATIO - total_invested
        if available < CONFIG.MIN_ORDER_KRW:
            logger.info("투자 가능 금액 부족: %.0f원", available)
            return 0.0

        atr_pct = atr / current_price if current_price > 0 else 0.05
        if atr_pct <= 0:
            atr_pct = 0.05

        base_size = (total_portfolio_krw * CONFIG.MAX_RISK_PER_TRADE) / atr_pct

        # 신뢰도 배수
        if combined_confidence >= 85:
            conf_mult = 1.0
        elif combined_confidence >= 75:
            conf_mult = 0.75
        else:
            conf_mult = 0.50

        adjusted = base_size * conf_mult
        max_single = total_portfolio_krw * CONFIG.MAX_SINGLE_POSITION_RATIO
        size = min(adjusted, max_single, available)
        size = max(0.0, size)

        if size < CONFIG.MIN_ORDER_KRW:
            return 0.0

        logger.info(
            "%s 포지션 사이즈: %.0f원 (ATR%.2f%% conf=%.0f mult=%.2f)",
            ticker, size, atr_pct * 100, combined_confidence, conf_mult,
        )
        return round(size, 0)

    # ── 청산 조건 체크 ────────────────────────────────────────────────────────

    def check_exit(self, pos: Position, current_price: float) -> Optional[str]:
        """
        청산 조건 판단.

        우선순위:
          1. 고정 손절 (-10% from entry)
          2. 트레일링 스톱 (-10% from peak, +5% 수익 후 활성)

        Returns:
            청산 사유 문자열 or None
        """
        pos.update_peak(current_price)

        # 1. 고정 손절
        if current_price <= pos.stop_loss_price:
            return f"손절 (매수가 대비 {pos.unrealized_pnl_rate(current_price):.1%})"

        # 2. 트레일링 스톱
        if pos.trailing_active and current_price <= pos.trailing_stop_price:
            return (
                f"트레일링스톱 고점={pos.peak_price:,.0f} "
                f"기준가={pos.trailing_stop_price:,.0f} "
                f"수익률={pos.unrealized_pnl_rate(current_price):.1%}"
            )

        return None

    def print_summary(self) -> None:
        if not self._positions:
            logger.info("보유 포지션 없음")
            return
        logger.info("── 보유 포지션 (%d/%d) ──", self.count, CONFIG.MAX_CONCURRENT_POSITIONS)
        for pos in self._positions.values():
            logger.info("  %s", pos)
