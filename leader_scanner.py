"""
leader_scanner.py - 시장 리더 코인 식별

복합 점수로 주도 코인 선별:
  Leader Score = Volume(35%) + Momentum(30%) + RS_vs_BTC(20%) + Liquidity(15%)

최소 필터: 거래량 급등 1.5x 미만 제외, 24h 거래대금 기준 미달 제외
"""
from __future__ import annotations

import time
import logging
from typing import Optional

import pyupbit
import pandas as pd
import numpy as np

from config import CONFIG

logger = logging.getLogger("trader.scanner")

EXCLUDED_TICKERS = {"KRW-USDT", "KRW-USDC", "KRW-DAI"}
RS_BENCHMARK = "KRW-BTC"
REQUEST_DELAY = 0.12  # 업비트 API 초당 10회 제한


def fetch_ohlcv(ticker: str) -> Optional[pd.DataFrame]:
    """OHLCV 조회. 데이터 부족 시 None 반환."""
    try:
        df = pyupbit.get_ohlcv(
            ticker,
            interval=CONFIG.CANDLE_INTERVAL,
            count=CONFIG.CANDLE_COUNT,
        )
        if df is not None and len(df) >= CONFIG.CANDLE_COUNT * 0.8:
            return df
    except Exception as e:
        logger.debug("OHLCV 조회 실패 %s: %s", ticker, e)
    return None


def _calc_volume_score(df: pd.DataFrame) -> tuple[float, float]:
    """
    거래량 급등 점수 (0~100) + 급등 배수 반환.
    현재 거래량 / 최근 20기간 평균 = surge_ratio
    surge_ratio 3.0x = 100점 (선형 스케일)
    """
    window = CONFIG.VOLUME_SURGE_WINDOW
    if len(df) < window + 1:
        return 0.0, 0.0

    current_vol = df["volume"].iloc[-1]
    avg_vol = df["volume"].iloc[-(window + 1):-1].mean()
    if avg_vol <= 0:
        return 0.0, 0.0

    ratio = current_vol / avg_vol
    score = min(100.0, (ratio / 3.0) * 100.0)
    return max(0.0, score), ratio


def _calc_momentum_score(df: pd.DataFrame) -> float:
    """
    가격 모멘텀 점수 (0~100).
    단기(5봉) ROC × 0.6 + 장기(20봉) ROC × 0.4
    ±30% 범위를 0~100으로 정규화
    """
    close = df["close"]
    if len(close) < 22:
        return 0.0

    short_roc = (close.iloc[-1] / close.iloc[-6] - 1) * 100
    long_roc = (close.iloc[-1] / close.iloc[-21] - 1) * 100
    combined = short_roc * 0.6 + long_roc * 0.4
    score = ((combined + 30) / 60) * 100
    return float(np.clip(score, 0, 100))


def _calc_rs_score(df: pd.DataFrame, btc_df: pd.DataFrame,
                   period: int = 20) -> float:
    """
    BTC 대비 상대강도 점수 (0~100).
    코인 수익률 - BTC 수익률 (20봉 기준)
    ±20%p 범위를 0~100으로 정규화
    """
    if len(df) < period + 1 or len(btc_df) < period + 1:
        return 50.0

    coin_ret = df["close"].iloc[-1] / df["close"].iloc[-(period + 1)] - 1
    btc_ret = btc_df["close"].iloc[-1] / btc_df["close"].iloc[-(period + 1)] - 1
    rs_diff = (coin_ret - btc_ret) * 100
    score = ((rs_diff + 20) / 40) * 100
    return float(np.clip(score, 0, 100))


def _calc_liquidity_score(ticker: str) -> float:
    """
    호가창 유동성 점수 (0~100).
    스프레드 타이트 + 호가 깊이 = 유동성 좋음
    """
    try:
        orderbook = pyupbit.get_orderbook(ticker)
        if not orderbook:
            return 0.0
        ob = orderbook[0] if isinstance(orderbook, list) else orderbook
        units = ob.get("orderbook_units", [])
        if not units:
            return 0.0

        best_ask = units[0]["ask_price"]
        best_bid = units[0]["bid_price"]
        if best_bid <= 0:
            return 0.0

        spread_pct = (best_ask - best_bid) / best_bid * 100
        spread_score = max(0.0, min(100.0, (1.0 - spread_pct) / 0.9 * 100.0))

        total_depth = sum(
            u["bid_price"] * u["bid_size"] + u["ask_price"] * u["ask_size"]
            for u in units[:5]
        )
        depth_score = min(100.0, max(0.0, (np.log10(max(total_depth, 1)) - 7) / 3 * 100.0))

        return spread_score * 0.5 + depth_score * 0.5
    except Exception as e:
        logger.debug("유동성 조회 실패 %s: %s", ticker, e)
        return 0.0


def scan_market_leaders() -> list[dict]:
    """
    전체 KRW 마켓 스캔 → 상위 LEADER_TOP_N 리더 코인 반환.

    반환 형식:
    [
      {
        "ticker": "KRW-XXX",
        "composite_score": float,
        "volume_score": float,
        "momentum_score": float,
        "rs_score": float,
        "liquidity_score": float,
        "volume_ratio": float,
        "latest_close": float,
      },
      ...
    ]
    """
    logger.info("시장 리더 스캔 시작...")

    all_tickers = pyupbit.get_tickers(fiat="KRW")
    if not all_tickers:
        logger.error("KRW 티커 조회 실패")
        return []

    tickers = [t for t in all_tickers if t not in EXCLUDED_TICKERS]

    # BTC 기준 데이터 (상대강도 계산용)
    btc_df = fetch_ohlcv(RS_BENCHMARK)
    if btc_df is None:
        logger.error("BTC 데이터 조회 실패 - RS 점수 비활성화")
        btc_df = pd.DataFrame()

    # 24h 거래대금 필터링 (REST API 일괄 조회)
    try:
        import requests
        chunk_size = 100
        ticker_rows = []
        for i in range(0, len(tickers), chunk_size):
            chunk = tickers[i:i + chunk_size]
            resp = requests.get(
                "https://api.upbit.com/v1/ticker",
                params={"markets": ",".join(chunk)},
                timeout=10,
            )
            resp.raise_for_status()
            ticker_rows.extend(resp.json())
            time.sleep(REQUEST_DELAY)

        df_all = pd.DataFrame(ticker_rows)
        df_filtered = df_all[
            df_all["acc_trade_price_24h"] >= CONFIG.MIN_VOLUME_KRW_24H
        ]["market"].tolist()
        logger.info("24h 거래대금 필터 통과: %d / %d 종목", len(df_filtered), len(tickers))
        tickers = df_filtered
    except Exception as e:
        logger.warning("거래대금 필터 실패, 전체 스캔 진행: %s", e)

    results = []
    for ticker in tickers:
        if ticker in EXCLUDED_TICKERS:
            continue

        df = fetch_ohlcv(ticker)
        if df is None:
            continue

        vol_score, vol_ratio = _calc_volume_score(df)

        # 거래량 최소 급등 기준 미달 시 건너뜀
        if vol_ratio < CONFIG.VOLUME_SURGE_MIN_RATIO:
            continue

        mom_score = _calc_momentum_score(df)
        rs_score = _calc_rs_score(df, btc_df) if not btc_df.empty else 50.0
        liq_score = _calc_liquidity_score(ticker)

        composite = (
            vol_score * 0.35
            + mom_score * 0.30
            + rs_score * 0.20
            + liq_score * 0.15
        )

        results.append({
            "ticker": ticker,
            "composite_score": round(composite, 2),
            "volume_score": round(vol_score, 2),
            "momentum_score": round(mom_score, 2),
            "rs_score": round(rs_score, 2),
            "liquidity_score": round(liq_score, 2),
            "volume_ratio": round(vol_ratio, 2),
            "latest_close": float(df["close"].iloc[-1]),
        })

        time.sleep(REQUEST_DELAY)

    results.sort(key=lambda x: x["composite_score"], reverse=True)
    leaders = results[:CONFIG.LEADER_TOP_N]

    for rank, ldr in enumerate(leaders, 1):
        logger.info(
            "Leader #%d: %s | 종합=%.1f Vol=%.0f Mom=%.0f RS=%.0f Liq=%.0f (거래량%.1fx)",
            rank, ldr["ticker"], ldr["composite_score"],
            ldr["volume_score"], ldr["momentum_score"],
            ldr["rs_score"], ldr["liquidity_score"],
            ldr["volume_ratio"],
        )

    return leaders
