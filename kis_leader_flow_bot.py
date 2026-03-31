#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
오늘경제TV - 오전장 주도주 수급 포착 봇
---------------------------------------------------------
[핵심 특징]
1) 한국투자증권(KIS) OpenAPI를 사용한 실시간(준실시간) 감시
2) 09:00~15:20 장 운영시간 + 09:05~10:30 타점 탐색시간 분리
3) 오전 15분 거래대금 폭발 + 시가 지지/고점 재돌파 + 프로그램 순매수 전환
4) 텔레그램 즉시 알림
5) 토큰 자동 발급/갱신, 레이트리밋 방어용 딜레이, 예외 로깅

주의:
- KIS OpenAPI의 TR_ID/응답필드는 계좌/권한/상품구분에 따라 다를 수 있습니다.
- 아래 ENDPOINTS/TR_ID 설정은 실전에서 반드시 본인 API 문서 기준으로 점검하세요.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, time as dtime
from typing import Any, Dict, List, Optional

import requests
from zoneinfo import ZoneInfo

try:
    # 종목 풀 구축용(장중 실시간 데이터는 KIS 사용)
    from pykrx import stock
except ImportError:
    stock = None


# ============================================================
# 1) 사용자 설정 영역 (가장 먼저 수정할 부분)
# ============================================================
KIS_APP_KEY = "여기에_KIS_APP_KEY"
KIS_APP_SECRET = "여기에_KIS_APP_SECRET"
KIS_BASE_URL = "https://openapi.koreainvestment.com:9443"  # 실전
# KIS_BASE_URL = "https://openapivts.koreainvestment.com:29443"  # 모의

TELEGRAM_BOT_TOKEN = "여기에_텔레그램_BOT_TOKEN"
TELEGRAM_CHAT_ID = "여기에_CHAT_ID"

# API 요청 사이 최소 간격(초) - REST 폴링 시 레이트리밋 방어
REQUEST_DELAY_SEC = 0.12
# 한 루프에서 감시할 최대 종목 수(요청량 방어)
MAX_WATCHLIST_SIZE = 80

# 로직 파라미터
MIN_MKT_CAP = 100_000_000_000       # 1,000억
MAX_MKT_CAP = 2_000_000_000_000     # 2조
TURNOVER_BURST_RATIO = 0.30         # 15분 누적 거래대금 / 전일 거래대금 >= 30%
PROGRAM_STRONG_DELTA = 20_000       # 프로그램 순매수 급증 판단(수량)
BREAKOUT_BUFFER = 0.0015            # 1차 고점 재돌파 여유(0.15%)

# 시간 설정 (한국시간)
KST = ZoneInfo("Asia/Seoul")
MARKET_OPEN = dtime(9, 0, 0)
MARKET_CLOSE = dtime(15, 20, 0)
SCAN_START = dtime(9, 5, 0)
SCAN_END = dtime(10, 30, 0)
FIFTEEN_MARK = dtime(9, 15, 0)

# 엔드포인트 / TR_ID 매핑 (계좌환경별로 다를 수 있으므로 필요시 수정)
ENDPOINTS = {
    "token": "/oauth2/tokenP",
    "price": "/uapi/domestic-stock/v1/quotations/inquire-price",
    "daily_price": "/uapi/domestic-stock/v1/quotations/inquire-daily-price",
    "program_trade": "/uapi/domestic-stock/v1/quotations/program-trade-by-stock",
}
TR_IDS = {
    "price": "FHKST01010100",
    "daily_price": "FHKST01010400",
    "program_trade": "FHPPG00010000",  # 실제 문서 기준으로 교체 필요할 수 있음
}


# ============================================================
# 2) 로깅 설정
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("leader_flow_bot.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("leader-flow-bot")


@dataclass
class TokenState:
    access_token: str = ""
    expired_at: datetime = field(default_factory=lambda: datetime.now(tz=KST))


@dataclass
class IntradayState:
    symbol: str
    name: str = ""
    open_price: int = 0
    first_peak: int = 0
    first_peak_time: Optional[datetime] = None
    turnover_15m: int = 0
    yday_turnover: int = 0
    program_net_prev: int = 0
    alerted: bool = False
    below_open_once: bool = False


class KISClient:
    """KIS REST API 연동 클라이언트 (토큰 자동관리 포함)."""

    def __init__(self, app_key: str, app_secret: str, base_url: str):
        self.app_key = app_key
        self.app_secret = app_secret
        self.base_url = base_url.rstrip("/")
        self.token = TokenState()
        self.session = requests.Session()

    def _token_needed(self) -> bool:
        # 만료 2분 전이면 선제 갱신
        return (not self.token.access_token) or (datetime.now(tz=KST) >= self.token.expired_at - timedelta(minutes=2))

    def ensure_token(self) -> None:
        if not self._token_needed():
            return

        url = f"{self.base_url}{ENDPOINTS['token']}"
        payload = {
            "grant_type": "client_credentials",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
        }
        headers = {"content-type": "application/json"}

        try:
            res = self.session.post(url, headers=headers, json=payload, timeout=8)
            res.raise_for_status()
            data = res.json()

            self.token.access_token = data["access_token"]
            expires_in = int(data.get("expires_in", 3600))
            self.token.expired_at = datetime.now(tz=KST) + timedelta(seconds=expires_in)
            logger.info("KIS 토큰 갱신 완료 (만료시각: %s)", self.token.expired_at.strftime("%H:%M:%S"))

            # 발급 제한 보호 (과도한 재발급 방지)
            time.sleep(1.0)
        except Exception as e:
            logger.exception("토큰 발급 실패: %s", e)
            raise

    def _headers(self, tr_id: str) -> Dict[str, str]:
        self.ensure_token()
        return {
            "authorization": f"Bearer {self.token.access_token}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "tr_id": tr_id,
            "content-type": "application/json; charset=utf-8",
        }

    def get(self, endpoint: str, tr_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{self.base_url}{endpoint}"
        try:
            res = self.session.get(url, headers=self._headers(tr_id), params=params, timeout=8)
            if res.status_code == 401:
                # 토큰 만료 시 1회 재시도
                logger.warning("401 수신: 토큰 재발급 후 재시도")
                self.token.access_token = ""
                self.ensure_token()
                res = self.session.get(url, headers=self._headers(tr_id), params=params, timeout=8)

            res.raise_for_status()
            data = res.json()

            # KIS 응답코드 방어
            if data.get("rt_cd") not in ("0", 0, None):
                logger.warning("KIS 경고 rt_cd=%s msg=%s endpoint=%s", data.get("rt_cd"), data.get("msg1"), endpoint)

            time.sleep(REQUEST_DELAY_SEC)
            return data

        except Exception as e:
            logger.exception("GET 실패 endpoint=%s params=%s error=%s", endpoint, params, e)
            return {}


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

    def send_signal(self, st: IntradayState, now_price: int, now_dt: datetime) -> None:
        change_pct = ((now_price / st.open_price) - 1.0) * 100 if st.open_price else 0.0
        text = (
            "📊 [오늘경제TV 주도주 수급 포착]\n"
            f"- 종목명: {st.name} ({st.symbol})\n"
            f"- 포착 시간: {now_dt.strftime('%H:%M:%S')}\n"
            f"- 현재가: {now_price:,}원 (당일 시가 대비 {change_pct:+.1f}%)\n"
            "- 포착 사유: 오전장 거래대금 폭발 및 프로그램 순매수 유입\n"
            "- 🎯 1차 익절 목표가: 현재가 대비 +3~5%\n"
            "- 🛑 기계적 손절가: 당일 시가 이탈 시 즉시 손절"
        )
        payload = {
            "chat_id": self.chat_id,
            "text": text,
        }
        try:
            r = requests.post(self.url, json=payload, timeout=5)
            r.raise_for_status()
            logger.info("텔레그램 전송 완료: %s (%s)", st.name, st.symbol)
        except Exception as e:
            logger.exception("텔레그램 전송 실패: %s", e)


class LeaderFlowBot:
    """오전장 주도주 수급 돌파 전략 실행 엔진."""

    def __init__(self):
        self.kis = KISClient(KIS_APP_KEY, KIS_APP_SECRET, KIS_BASE_URL)
        self.tg = TelegramNotifier(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
        self.states: Dict[str, IntradayState] = {}
        self.watchlist: List[str] = []
        self.last_watchlist_date: Optional[str] = None

    # --------------------------------------------------------
    # 유니버스 구축
    # --------------------------------------------------------
    def _today_str(self) -> str:
        return datetime.now(tz=KST).strftime("%Y%m%d")

    def _load_krx_symbols(self) -> List[str]:
        if stock is None:
            logger.error("pykrx가 설치되지 않아 종목 풀 생성 불가. pip install pykrx 필요")
            return []

        today = self._today_str()
        try:
            kospi = stock.get_market_ticker_list(today, market="KOSPI")
            kosdaq = stock.get_market_ticker_list(today, market="KOSDAQ")
            return list(kospi) + list(kosdaq)
        except Exception as e:
            logger.exception("KRX 티커 로딩 실패: %s", e)
            return []

    @staticmethod
    def _is_preferred_or_spac(name: str) -> bool:
        n = (name or "").strip()
        if "스팩" in n.upper() or "SPAC" in n.upper():
            return True
        # 국내 우선주 네이밍 필터(완벽하지 않으므로 실무에서 추가 보강 권장)
        suffixes = ("우", "우B", "우C", "1우", "2우", "3우")
        return n.endswith(suffixes)

    def _fetch_basic_snapshot(self, symbol: str) -> Dict[str, Any]:
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",  # J: 주식
            "FID_INPUT_ISCD": symbol,
        }
        data = self.kis.get(ENDPOINTS["price"], TR_IDS["price"], params)
        return data.get("output", {}) if data else {}

    def build_watchlist(self) -> None:
        """관리/환기/우선주/SPAC 제외 + 시총 필터로 감시대상 생성."""
        symbols = self._load_krx_symbols()
        selected: List[str] = []

        for sym in symbols:
            snap = self._fetch_basic_snapshot(sym)
            if not snap:
                continue

            name = snap.get("hts_kor_isnm", "")
            if self._is_preferred_or_spac(name):
                continue

            # 시장경고/관리/환기 필터 (API 필드명은 계좌환경별 상이 가능)
            warning = (snap.get("mrkt_warn_cls_code") or "").strip()
            stat_cls = (snap.get("iscd_stat_cls_code") or "").strip()
            if warning and warning != "00":
                continue
            if stat_cls in {"51", "52", "53"}:  # 예시: 관리/투자유의 등
                continue

            try:
                mkt_cap = int(str(snap.get("stck_avls", "0")).replace(",", ""))
            except ValueError:
                mkt_cap = 0

            if MIN_MKT_CAP <= mkt_cap <= MAX_MKT_CAP:
                selected.append(sym)

            if len(selected) >= MAX_WATCHLIST_SIZE:
                break

        self.watchlist = selected
        self.last_watchlist_date = self._today_str()
        logger.info("감시대상 %d개 확정", len(self.watchlist))

    # --------------------------------------------------------
    # 시그널 계산
    # --------------------------------------------------------
    def _get_or_create_state(self, symbol: str, name: str, open_price: int) -> IntradayState:
        if symbol not in self.states:
            self.states[symbol] = IntradayState(symbol=symbol, name=name, open_price=open_price)
        st = self.states[symbol]
        if not st.name:
            st.name = name
        if st.open_price == 0 and open_price > 0:
            st.open_price = open_price
        return st

    def _fetch_today_yday_turnover(self, symbol: str) -> tuple[int, int]:
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": symbol,
            "FID_ORG_ADJ_PRC": "0",
            "FID_PERIOD_DIV_CODE": "D",
        }
        data = self.kis.get(ENDPOINTS["daily_price"], TR_IDS["daily_price"], params)
        out = data.get("output", []) if data else []
        if not out:
            return 0, 0

        # output[0]=당일, output[1]=전일(일반적 케이스)
        try:
            today_turnover = int(str(out[0].get("acml_tr_pbmn", "0")).replace(",", ""))
        except Exception:
            today_turnover = 0
        try:
            yday_turnover = int(str(out[1].get("acml_tr_pbmn", "0")).replace(",", "")) if len(out) > 1 else 0
        except Exception:
            yday_turnover = 0
        return today_turnover, yday_turnover

    def _fetch_program_net_buy(self, symbol: str) -> int:
        """프로그램 순매수 수량 조회 (TR/필드는 반드시 본인 문서 확인 필요)."""
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": symbol,
        }
        data = self.kis.get(ENDPOINTS["program_trade"], TR_IDS["program_trade"], params)
        out = data.get("output", {}) if data else {}

        # 계정환경별 필드명이 다를 수 있어 후보키 순차 탐색
        keys = ["prsm_nby_qty", "prgm_ntby_qty", "program_net_buy_qty", "netprps_qty"]
        for k in keys:
            if k in out:
                try:
                    return int(str(out[k]).replace(",", ""))
                except Exception:
                    pass
        return 0

    def _detect_signal(self, symbol: str, now_dt: datetime) -> Optional[Dict[str, Any]]:
        snap = self._fetch_basic_snapshot(symbol)
        if not snap:
            return None

        name = snap.get("hts_kor_isnm", symbol)
        try:
            now_price = int(str(snap.get("stck_prpr", "0")).replace(",", ""))
            open_price = int(str(snap.get("stck_oprc", "0")).replace(",", ""))
            high_price = int(str(snap.get("stck_hgpr", "0")).replace(",", ""))
        except ValueError:
            return None

        if now_price <= 0 or open_price <= 0:
            return None

        st = self._get_or_create_state(symbol, name, open_price)
        if st.alerted:
            return None

        # 조건2-보조: 시가 이탈 여부 추적
        if now_price < st.open_price:
            st.below_open_once = True
            return None

        # 1차 고점 기록 (09:05~10:00 구간에서 선형 업데이트)
        if now_dt.time() <= dtime(10, 0, 0):
            if high_price > st.first_peak:
                st.first_peak = high_price
                st.first_peak_time = now_dt
            return None

        # ----------------------------------------------------
        # 조건 1: 09:15 이후, 오늘 누적 거래대금 >= 전일 거래대금의 30%
        # ----------------------------------------------------
        today_turnover, yday_turnover = self._fetch_today_yday_turnover(symbol)
        st.turnover_15m = today_turnover
        st.yday_turnover = yday_turnover

        cond1 = (
            now_dt.time() >= FIFTEEN_MARK
            and yday_turnover > 0
            and today_turnover >= int(yday_turnover * TURNOVER_BURST_RATIO)
        )

        # ----------------------------------------------------
        # 조건 2: 시가 미이탈 + 1차 고점 재돌파
        # ----------------------------------------------------
        cond2 = (
            (not st.below_open_once)
            and st.first_peak > 0
            and now_price >= int(st.first_peak * (1 + BREAKOUT_BUFFER))
        )

        # ----------------------------------------------------
        # 조건 3: 프로그램 순매수 음수->양수 전환 또는 강한 증가
        # ----------------------------------------------------
        program_now = self._fetch_program_net_buy(symbol)
        delta = program_now - st.program_net_prev
        cond3 = (st.program_net_prev < 0 <= program_now) or (program_now > 0 and delta >= PROGRAM_STRONG_DELTA)
        st.program_net_prev = program_now

        if cond1 and cond2 and cond3:
            return {
                "symbol": symbol,
                "name": name,
                "price": now_price,
            }

        return None

    # --------------------------------------------------------
    # 실행 루프
    # --------------------------------------------------------
    @staticmethod
    def _in_market_hours(now_dt: datetime) -> bool:
        t = now_dt.time()
        return MARKET_OPEN <= t <= MARKET_CLOSE

    @staticmethod
    def _in_scan_window(now_dt: datetime) -> bool:
        t = now_dt.time()
        return SCAN_START <= t <= SCAN_END

    def run(self) -> None:
        logger.info("오늘경제TV 주도주 수급 봇 시작")
        logger.info("운영시간: 09:00~15:20, 탐색시간: 09:05~10:30")

        while True:
            try:
                now_dt = datetime.now(tz=KST)

                # 장외 시간: 1분 대기
                if not self._in_market_hours(now_dt):
                    time.sleep(60)
                    continue

                # 일자 바뀌면 감시대상/상태 초기화
                today = self._today_str()
                if self.last_watchlist_date != today:
                    self.states.clear()
                    self.build_watchlist()

                # 탐색시간이 아니면 가벼운 heartbeat만 유지
                if not self._in_scan_window(now_dt):
                    time.sleep(5)
                    continue

                if not self.watchlist:
                    logger.warning("감시대상이 비어있어 30초 후 재시도")
                    time.sleep(30)
                    continue

                # 종목 순회 탐색
                for sym in self.watchlist:
                    sig = self._detect_signal(sym, now_dt)
                    if sig:
                        st = self.states[sym]
                        self.tg.send_signal(st, sig["price"], now_dt)
                        st.alerted = True

                # 루프 간 인터벌
                time.sleep(1.0)

            except Exception as e:
                # 어떤 에러가 나더라도 봇은 종료하지 않고 지속
                logger.exception("메인 루프 예외(지속 실행): %s", e)
                time.sleep(3)


def validate_config() -> None:
    missing = []
    if "여기에_" in KIS_APP_KEY:
        missing.append("KIS_APP_KEY")
    if "여기에_" in KIS_APP_SECRET:
        missing.append("KIS_APP_SECRET")
    if "여기에_" in TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if "여기에_" in TELEGRAM_CHAT_ID:
        missing.append("TELEGRAM_CHAT_ID")

    if missing:
        raise ValueError(f"설정값 누락: {', '.join(missing)}")


if __name__ == "__main__":
    try:
        validate_config()
        bot = LeaderFlowBot()
        bot.run()
    except Exception as exc:
        logger.exception("프로그램 시작 실패: %s", exc)
        sys.exit(1)
