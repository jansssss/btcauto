"""
order_executor.py - 주문 실행

DRY_RUN=true: 실주문 없이 로그만
DRY_RUN=false: 업비트 시장가 주문 실행

재시도: 최대 3회, 지수 백오프
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

import pyupbit

from config import CONFIG

logger = logging.getLogger("trader.executor")


@dataclass
class OrderResult:
    success: bool
    ticker: str
    side: str           # "buy" | "sell"
    price: float        # 예상 체결가
    quantity: float
    amount_krw: float
    order_id: Optional[str] = None
    error: Optional[str] = None
    dry_run: bool = False

    def __str__(self) -> str:
        mode = "[DRY-RUN]" if self.dry_run else "[LIVE]"
        status = "OK" if self.success else f"FAIL({self.error})"
        return (
            f"Order{mode} {status} | {self.side.upper()} {self.ticker} "
            f"qty={self.quantity:.8f} price={self.price:,.0f} "
            f"total={self.amount_krw:,.0f}KRW"
            + (f" id={self.order_id}" if self.order_id else "")
        )


class OrderExecutor:
    _MAX_RETRIES = 3
    _BASE_DELAY = 1.0

    def __init__(self) -> None:
        self._client: Optional[pyupbit.Upbit] = None
        self._init_client()

    def _init_client(self) -> None:
        if CONFIG.DRY_RUN:
            logger.info("DRY-RUN 모드: 실제 주문 비활성화")
            return
        if not CONFIG.UPBIT_ACCESS_KEY or not CONFIG.UPBIT_SECRET_KEY:
            raise RuntimeError("실거래 모드: .env에 API 키가 없습니다")

        self._client = pyupbit.Upbit(
            access=CONFIG.UPBIT_ACCESS_KEY,
            secret=CONFIG.UPBIT_SECRET_KEY,
        )
        try:
            bal = self._client.get_balance("KRW")
            logger.info("업비트 API 연결 성공 | KRW 잔고: %s원", f"{bal:,.0f}")
        except Exception as e:
            raise RuntimeError(f"업비트 API 인증 실패: {e}") from e

    # ── 잔고 조회 ─────────────────────────────────────────────────────────────

    def get_krw_balance(self) -> float:
        if CONFIG.DRY_RUN:
            return 10_000_000.0  # 모의 잔고
        try:
            bal = self._client.get_balance("KRW")
            return float(bal) if bal else 0.0
        except Exception as e:
            logger.error("KRW 잔고 조회 실패: %s", e)
            return 0.0

    def get_total_portfolio_krw(self) -> float:
        if CONFIG.DRY_RUN:
            return 10_000_000.0
        try:
            total = 0.0
            for b in self._client.get_balances():
                currency = b["currency"]
                balance = float(b["balance"]) + float(b.get("locked", 0))
                if currency == "KRW":
                    total += balance
                else:
                    price = pyupbit.get_current_price(f"KRW-{currency}")
                    if price:
                        total += balance * float(price)
            return total
        except Exception as e:
            logger.error("포트폴리오 조회 실패: %s", e)
            return 0.0

    def get_coin_quantity(self, ticker: str) -> float:
        if CONFIG.DRY_RUN:
            return 0.0
        coin = ticker.split("-")[-1]
        try:
            bal = self._client.get_balance(coin)
            return float(bal) if bal else 0.0
        except Exception as e:
            logger.error("%s 잔고 조회 실패: %s", ticker, e)
            return 0.0

    # ── 주문 실행 ─────────────────────────────────────────────────────────────

    def buy(self, ticker: str, amount_krw: float,
            current_price: float) -> OrderResult:
        """시장가 매수"""
        if amount_krw < CONFIG.MIN_ORDER_KRW:
            msg = f"최소 주문금액 미달: {amount_krw:.0f} < {CONFIG.MIN_ORDER_KRW:.0f}"
            return OrderResult(False, ticker, "buy", current_price, 0.0, amount_krw, error=msg)

        est_qty = amount_krw / current_price if current_price > 0 else 0.0

        if CONFIG.DRY_RUN:
            logger.info("[DRY-RUN] 매수 | %s %.8f개 @ %.4f원 (%.0f원)",
                        ticker, est_qty, current_price, amount_krw)
            return OrderResult(True, ticker, "buy", current_price, est_qty,
                               amount_krw, "dry-run-buy", dry_run=True)

        return self._retry("buy", ticker, current_price, est_qty,
                           amount_krw=amount_krw)

    def sell(self, ticker: str, quantity: float,
             current_price: float) -> OrderResult:
        """시장가 매도"""
        amount_krw = quantity * current_price

        if CONFIG.DRY_RUN:
            logger.info("[DRY-RUN] 매도 | %s %.8f개 @ %.4f원 (%.0f원)",
                        ticker, quantity, current_price, amount_krw)
            return OrderResult(True, ticker, "sell", current_price, quantity,
                               amount_krw, "dry-run-sell", dry_run=True)

        return self._retry("sell", ticker, current_price, quantity,
                           sell_qty=quantity)

    def _retry(self, action: str, ticker: str, price: float, qty: float,
               amount_krw: float = 0.0, sell_qty: float = 0.0) -> OrderResult:
        last_err: Optional[Exception] = None
        for attempt in range(1, self._MAX_RETRIES + 1):
            try:
                if action == "buy":
                    resp = self._client.buy_market_order(ticker, amount_krw)
                else:
                    resp = self._client.sell_market_order(ticker, sell_qty)

                if resp and "uuid" in resp:
                    order_id = resp["uuid"]
                    logger.info("[%s] %s 성공 | qty=%.8f price~%.0f id=%s",
                                action.upper(), ticker, qty, price, order_id)
                    return OrderResult(True, ticker, action, price, qty,
                                       amount_krw or price * sell_qty,
                                       order_id=order_id)
                raise ValueError(f"비정상 응답: {resp}")

            except Exception as e:
                last_err = e
                delay = self._BASE_DELAY * (2 ** (attempt - 1))
                logger.warning("주문 실패 (%d/%d) %s %s: %s | %.0fs 후 재시도",
                               attempt, self._MAX_RETRIES, action, ticker, e, delay)
                if attempt < self._MAX_RETRIES:
                    time.sleep(delay)

        msg = f"주문 최종 실패 ({self._MAX_RETRIES}회): {last_err}"
        logger.error(msg)
        return OrderResult(False, ticker, action, price, qty,
                           amount_krw or price * sell_qty, error=msg)
