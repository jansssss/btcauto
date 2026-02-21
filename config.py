"""
config.py - 전역 설정 및 로깅
API 키는 .env 파일에서 로드 (절대 소스코드 하드코딩 금지)
"""
import os
import logging
import logging.handlers
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class TradingConfig:
    # ── API 인증 ──────────────────────────────────────────────
    UPBIT_ACCESS_KEY: str = field(default_factory=lambda: os.getenv("UPBIT_ACCESS_KEY", ""))
    UPBIT_SECRET_KEY: str = field(default_factory=lambda: os.getenv("UPBIT_SECRET_KEY", ""))

    # ── 거래 모드 ──────────────────────────────────────────────
    DRY_RUN: bool = field(
        default_factory=lambda: os.getenv("DRY_RUN", "true").lower() == "true"
    )

    # ── 리스크 관리 ────────────────────────────────────────────
    STOP_LOSS_RATE: float = -0.10           # 매수가 대비 -10% 고정 손절
    TRAILING_STOP_RATE: float = -0.10       # 고점 대비 -10% 트레일링 스톱
    TRAILING_ACTIVATION_RATE: float = 0.05  # +5% 수익 후 트레일링 활성화

    # ── 포지션 사이징 ──────────────────────────────────────────
    MAX_CONCURRENT_POSITIONS: int = 5       # 최대 동시 보유 종목 수
    MAX_SINGLE_POSITION_RATIO: float = 0.20 # 단일 종목 최대 비중 (포트폴리오의 20%)
    MAX_INVESTED_RATIO: float = 0.80        # 최대 투자 비중 (현금 20% 유지)
    MAX_RISK_PER_TRADE: float = 0.02        # 거래당 최대 리스크 (포트폴리오의 2%)
    MIN_ORDER_KRW: float = 5_000            # 업비트 최소 주문금액

    # ── 시장 스캐너 파라미터 ───────────────────────────────────
    LEADER_TOP_N: int = 5                   # 상위 N개 리더 코인 선별
    VOLUME_SURGE_WINDOW: int = 20           # 거래량 기준 기간 (캔들 수)
    VOLUME_SURGE_MIN_RATIO: float = 1.5     # 거래량 최소 급등 배수
    MIN_VOLUME_KRW_24H: float = 5_000_000_000  # 최소 24시간 거래대금 (50억)

    # ── 기술적 지표 파라미터 ───────────────────────────────────
    CANDLE_INTERVAL: str = "minute60"       # 1시간봉 기준
    CANDLE_COUNT: int = 200                 # 조회 캔들 수
    EMA_FAST: int = 9
    EMA_MID: int = 21
    EMA_SLOW: int = 50
    RSI_PERIOD: int = 14
    RSI_ENTRY_MIN: float = 35.0
    RSI_ENTRY_MAX: float = 65.0
    ADX_PERIOD: int = 14
    ADX_THRESHOLD: float = 20.0
    ATR_PERIOD: int = 14
    ENTRY_SCORE_THRESHOLD: int = 70         # 70점 이상 시 진입

    # ── 스케줄링 ──────────────────────────────────────────────
    SCAN_INTERVAL_SECONDS: int = 300        # 시장 스캔 주기 (5분)
    EXIT_CHECK_INTERVAL_SECONDS: int = 5    # 손절/트레일링 체크 주기 (5초)

    # ── 디렉토리 ──────────────────────────────────────────────
    STATE_DIR: str = "D:/auto/state"
    LOG_DIR: str = "D:/auto/logs"

    def validate(self) -> None:
        if not self.DRY_RUN:
            if not self.UPBIT_ACCESS_KEY or not self.UPBIT_SECRET_KEY:
                raise ValueError("실거래 모드: API 키가 필요합니다 (.env 파일 확인)")
        if not (-1.0 < self.STOP_LOSS_RATE < 0):
            raise ValueError("STOP_LOSS_RATE는 -1.0 ~ 0 사이여야 합니다")
        if self.MAX_CONCURRENT_POSITIONS < 1:
            raise ValueError("MAX_CONCURRENT_POSITIONS는 1 이상이어야 합니다")


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    import os
    from datetime import datetime
    logger = logging.getLogger("trader")
    if logger.handlers:
        return logger
    logger.setLevel(level)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)-8s] %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    os.makedirs(CONFIG.LOG_DIR, exist_ok=True)
    fh = logging.handlers.TimedRotatingFileHandler(
        filename=os.path.join(CONFIG.LOG_DIR, f"trader_{datetime.now():%Y%m%d}.log"),
        when="midnight",
        backupCount=30,
        encoding="utf-8",
    )
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    return logger


CONFIG = TradingConfig()
