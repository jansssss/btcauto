"""
entry_engine.py - 매수 진입 신호 엔진

7가지 기술적 조건으로 0~100점 채점.
70점 이상이면 매수 진입 권장.

점수 배분:
  EMA 정배열(9>21>50): 25점
  MACD 히스토그램 양전환: 20점
  RSI 적정 구간(35~65): 15점
  ADX 추세 강도(>20): 15점
  볼린저 중간선 위: 10점
  거래량 확인(>=1.5x 평균): 10점
  양봉 확인: 5점
"""
from __future__ import annotations

import logging
from typing import Optional

import pandas as pd
import numpy as np

from config import CONFIG

logger = logging.getLogger("trader.entry")


# ── 기술적 지표 계산 ─────────────────────────────────────────────────────────

def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI"""
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _macd(close: pd.Series, fast: int = 12, slow: int = 26,
          signal: int = 9) -> tuple[pd.Series, pd.Series, pd.Series]:
    """MACD 라인, 시그널 라인, 히스토그램"""
    macd_line = _ema(close, fast) - _ema(close, slow)
    signal_line = _ema(macd_line, signal)
    return macd_line, signal_line, macd_line - signal_line


def _bollinger(close: pd.Series, period: int = 20,
               std_mult: float = 2.0) -> tuple[pd.Series, pd.Series, pd.Series]:
    mid = close.rolling(period).mean()
    std = close.rolling(period).std()
    return mid + std_mult * std, mid, mid - std_mult * std


def _adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average Directional Index"""
    high, low, close = df["high"], df["low"], df["close"]
    plus_dm = high.diff().clip(lower=0)
    minus_dm = (-low.diff()).clip(lower=0)
    plus_dm = plus_dm.where(plus_dm > minus_dm, 0.0)
    minus_dm = minus_dm.where(minus_dm > plus_dm, 0.0)

    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs(),
    ], axis=1).max(axis=1)

    alpha = 1 / period
    atr = tr.ewm(alpha=alpha, min_periods=period, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(alpha=alpha, min_periods=period, adjust=False).mean() / atr
    minus_di = 100 * minus_dm.ewm(alpha=alpha, min_periods=period, adjust=False).mean() / atr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=alpha, min_periods=period, adjust=False).mean()


def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


# ── 진입 점수 계산 ───────────────────────────────────────────────────────────

def compute_entry_score(df: pd.DataFrame) -> dict:
    """
    OHLCV DataFrame을 받아 진입 점수와 세부 시그널을 반환.

    Returns:
        {
          "score": int,               # 0~100
          "entry_recommended": bool,  # score >= ENTRY_SCORE_THRESHOLD
          "signals": dict,            # 각 조건 충족 여부
          "indicators": dict,         # 계산된 지표값
        }
    """
    close = df["close"]
    score = 0
    signals = {}
    indicators = {}

    # 1. EMA 정배열 (25점) ─────────────────────────────────────────────────
    ema9 = _ema(close, CONFIG.EMA_FAST).iloc[-1]
    ema21 = _ema(close, CONFIG.EMA_MID).iloc[-1]
    ema50 = _ema(close, CONFIG.EMA_SLOW).iloc[-1]
    indicators.update(ema9=round(ema9, 4), ema21=round(ema21, 4), ema50=round(ema50, 4))

    if ema9 > ema21 > ema50:
        score += 25
        signals["ema_aligned"] = True
    elif ema9 > ema21:
        score += 10
        signals["ema_aligned"] = False
    else:
        signals["ema_aligned"] = False

    # 2. MACD 히스토그램 (20점) ────────────────────────────────────────────
    _, _, hist = _macd(close)
    hist_now = hist.iloc[-1]
    hist_prev = hist.iloc[-2]
    hist_prev2 = hist.iloc[-3]
    indicators["macd_hist"] = round(float(hist_now), 6)

    macd_bullish = False
    if hist_now > 0 and hist_prev <= 0:       # 신규 양전환 (최강)
        score += 20
        macd_bullish = True
    elif hist_now > 0 and hist_now > hist_prev:  # 히스토그램 확장
        score += 15
        macd_bullish = True
    elif hist_now > hist_prev > hist_prev2:    # 연속 증가 (음수 구간도)
        score += 8
    signals["macd_bullish"] = macd_bullish

    # 3. RSI 적정 구간 (15점) ──────────────────────────────────────────────
    rsi_val = float(_rsi(close, CONFIG.RSI_PERIOD).iloc[-1])
    indicators["rsi"] = round(rsi_val, 2)

    if CONFIG.RSI_ENTRY_MIN <= rsi_val <= CONFIG.RSI_ENTRY_MAX:
        score += 15
        signals["rsi_ok"] = True
    elif rsi_val < CONFIG.RSI_ENTRY_MIN:  # 과매도 탈출 시도
        score += 8
        signals["rsi_ok"] = False
    else:
        signals["rsi_ok"] = False

    # 4. ADX 추세 강도 (15점) ──────────────────────────────────────────────
    adx_val = float(_adx(df, CONFIG.ADX_PERIOD).iloc[-1])
    indicators["adx"] = round(adx_val, 2)

    if adx_val > CONFIG.ADX_THRESHOLD:
        score += 15
        signals["adx_trending"] = True
    elif adx_val > 15:
        score += 7
        signals["adx_trending"] = False
    else:
        signals["adx_trending"] = False

    # 5. 볼린저 중간선 위 (10점) ───────────────────────────────────────────
    _, bb_mid, _ = _bollinger(close)
    bb_mid_val = float(bb_mid.iloc[-1])
    current_price = float(close.iloc[-1])
    indicators["bb_mid"] = round(bb_mid_val, 4)

    above_bb = current_price > bb_mid_val
    signals["above_bb_mid"] = above_bb
    if above_bb:
        score += 10

    # 6. 거래량 확인 (10점) ────────────────────────────────────────────────
    vol_avg = df["volume"].rolling(20).mean().iloc[-1]
    vol_now = df["volume"].iloc[-1]
    vol_ratio = float(vol_now / vol_avg) if vol_avg > 0 else 0.0
    indicators["volume_ratio"] = round(vol_ratio, 2)

    vol_confirmed = vol_ratio >= 1.5
    signals["volume_confirmed"] = vol_confirmed
    if vol_confirmed:
        score += 10

    # 7. 양봉 확인 (5점) ───────────────────────────────────────────────────
    bullish_candle = current_price > float(df["open"].iloc[-1])
    signals["bullish_candle"] = bullish_candle
    if bullish_candle:
        score += 5

    # ATR (포지션 사이징용)
    atr_val = float(_atr(df, CONFIG.ATR_PERIOD).iloc[-1])
    indicators["atr"] = round(atr_val, 4)
    indicators["current_price"] = current_price

    return {
        "score": score,
        "entry_recommended": score >= CONFIG.ENTRY_SCORE_THRESHOLD,
        "signals": signals,
        "indicators": indicators,
    }


def should_enter(ticker: str, df: pd.DataFrame,
                 leader_score: float) -> dict:
    """
    리더 점수 + 기술적 진입 점수를 합산한 최종 진입 판단.

    combined_confidence = leader_score × 0.4 + entry_score × 0.6

    Returns:
        {
          "enter": bool,
          "ticker": str,
          "entry_score": int,
          "leader_score": float,
          "combined_confidence": float,
          "entry_price": float,
          "stop_loss_price": float,
          "indicators": dict,
          "signals": dict,
        }
    """
    result = compute_entry_score(df)
    combined = leader_score * 0.4 + result["score"] * 0.6
    entry_price = result["indicators"]["current_price"]

    entry = {
        "enter": result["entry_recommended"],
        "ticker": ticker,
        "entry_score": result["score"],
        "leader_score": round(leader_score, 2),
        "combined_confidence": round(combined, 2),
        "entry_price": entry_price,
        "stop_loss_price": round(entry_price * (1 + CONFIG.STOP_LOSS_RATE), 4),
        "indicators": result["indicators"],
        "signals": result["signals"],
    }

    logger.info(
        "%s | 진입점수=%d 리더=%.1f 종합=%.1f | %s | 시그널: EMA=%s MACD=%s RSI=%.1f ADX=%.1f",
        ticker,
        result["score"],
        leader_score,
        combined,
        "진입 권장" if entry["enter"] else "대기",
        signals_str(result["signals"]),
        "O" if result["signals"].get("macd_bullish") else "X",
        result["indicators"].get("rsi", 0),
        result["indicators"].get("adx", 0),
    )

    return entry


def signals_str(signals: dict) -> str:
    mapping = {
        "ema_aligned": "EMA",
        "macd_bullish": "MACD",
        "rsi_ok": "RSI",
        "adx_trending": "ADX",
        "above_bb_mid": "BB",
        "volume_confirmed": "VOL",
        "bullish_candle": "BULL",
    }
    return " ".join(v for k, v in mapping.items() if signals.get(k, False))
