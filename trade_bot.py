# -*- coding: utf-8 -*-
"""Claude AI 모의투자 봇
   매 실행마다: 시세/지표 수집 → Claude에게 판단 요청 → Alpaca 모의계좌에 주문 → ntfy 알림
   ⚠️ 모의계좌(paper) 전용. 실거래 주소는 코드에 존재하지 않습니다."""
import csv
import io
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
import datetime

def log(*args):
    """stderr로 즉시 출력 — GitHub Actions 로그에서 항상 보이도록"""
    sys.stderr.write(" ".join(str(a) for a in args) + "\n")
    sys.stderr.flush()


# ── 환경 변수 (GitHub Secrets) ──
ALPACA_KEY = os.environ.get("ALPACA_API_KEY", "").strip()
ALPACA_SECRET = os.environ.get("ALPACA_SECRET_KEY", "").strip()
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
DEEPSEEK_KEY = os.environ.get("DEEPSEEK_API_KEY", "").strip()
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()

ALPACA_BASE = "https://paper-api.alpaca.markets"  # 모의계좌 전용 (변경 금지)
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}

# ── 안전장치 ──
MAX_TRADES_PER_RUN = 5          # 한 번 실행에 최대 주문 수
MAX_POSITION_PCT = 0.05         # 한 종목 신규 매수 한도: 총자산의 5% (기본)
MAX_POSITION_HIGH = 0.12        # 강한 확신 시 최대 한도: 12% (일반주, 하락장 제외)
MAX_POSITION_LEV = 0.08         # 레버리지: 확신 시 최대 8% (3배 변동이라 일반주보다 낮게)
LEVERAGE_TICKERS = {"TQQQ", "SOXL", "UPRO", "QLD", "TNA", "FNGU", "TECL", "LABU", "NVDL", "TSLL"}
INVERSE_TICKERS = {"SQQQ", "SOXS", "SH", "SDS"}
MIN_CASH_BUFFER_PCT = 0.10      # 현금 10%는 항상 남김

# ── 하이리스크 슬롯 (저평가·과매도 역추세 전용 격리 예산) ──
# 일반(core) 매수와 별도로 운용. 한 종목 물려도 전체가 휘청이지 않게 칸막이.
MAX_HIGHRISK_BUDGET_PCT = 0.20   # 하이리스크 포지션 '합계' 상한: 총자산의 20%
MAX_HIGHRISK_POSITION_PCT = 0.05 # 하이리스크 한 종목 상한: 총자산의 5% (레버리지 포함 동일)


def http_json(url, method="GET", headers=None, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={**UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


# ── 시세 (1순위: Alpaca 자체 데이터 → 2순위: Stooq → 3순위: Yahoo) ──
def _get(url, headers=None):
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def fetch_alpaca_data(sym):
    url = (f"https://data.alpaca.markets/v2/stocks/{urllib.parse.quote(sym)}/bars"
           f"?timeframe=1Day&limit=200&adjustment=split&feed=iex")
    j = json.loads(_get(url, headers={"APCA-API-KEY-ID": ALPACA_KEY,
                                      "APCA-API-SECRET-KEY": ALPACA_SECRET}))
    rows = [{"date": b["t"][:10], "close": float(b["c"]), "volume": float(b.get("v") or 0)}
            for b in (j.get("bars") or [])]
    return rows


def fetch_stooq(sym):
    text = _get(f"https://stooq.com/q/d/l/?s={sym.lower()}.us&i=d")
    rows = []
    for row in csv.DictReader(io.StringIO(text)):
        try:
            rows.append({"date": row["Date"], "close": float(row["Close"]),
                         "volume": float(row.get("Volume") or 0)})
        except (KeyError, ValueError):
            continue
    return rows


def fetch_yahoo(sym):
    j = json.loads(_get(f"https://query1.finance.yahoo.com/v8/finance/chart/"
                        f"{urllib.parse.quote(sym)}?range=1y&interval=1d"))
    res = j["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    rows = []
    for i, t in enumerate(res["timestamp"]):
        c = q["close"][i]
        if c is None:
            continue
        d = datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%Y-%m-%d")
        rows.append({"date": d, "close": float(c), "volume": float(q["volume"][i] or 0)})
    return rows


def _yahoo_crumb_session():
    """야후 quote API는 쿠키+crumb 인증을 요구한다(2024년 이후). 쿠키를 받고 crumb를
    발급받아 (opener, crumb)를 반환. 실패 시 (None, None)."""
    try:
        import http.cookiejar
        cj = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
        opener.addheaders = list(UA.items())
        # 1) 쿠키 받기 (finance 메인에서 consent 쿠키)
        try:
            opener.open("https://fc.yahoo.com", timeout=10)
        except Exception:
            opener.open("https://finance.yahoo.com", timeout=10)
        # 2) crumb 발급
        crumb = opener.open(
            "https://query1.finance.yahoo.com/v1/test/getcrumb", timeout=10
        ).read().decode("utf-8", "replace").strip()
        if crumb and "<" not in crumb:   # HTML 에러페이지가 아니면
            return opener, crumb
    except Exception as e:
        log(f"⚠️ 야후 crumb 발급 실패: {e}")
    return None, None


def fetch_valuations(symbols):
    """야후 batch quote로 PER·PBR 등 밸류에이션을 한 번에 수집(쿠키+crumb 인증).
    저평가 판정용. 실패해도 봇이 죽지 않게 {}를 반환(있으면 쓰고 없으면 기술적으로만).
    코인·일부 ETF는 값이 없을 수 있음 → 그대로 둠."""
    out = {}
    syms = [s for s in symbols if not is_crypto(s)]
    if not syms:
        return out
    opener, crumb = _yahoo_crumb_session()
    if not crumb:
        log("⚠️ 밸류에이션: 야후 인증 실패 → 기술적 지표로만 판정")
        return out
    for i in range(0, len(syms), 50):
        chunk = syms[i:i + 50]
        try:
            url = ("https://query1.finance.yahoo.com/v7/finance/quote?crumb="
                   + urllib.parse.quote(crumb) + "&symbols="
                   + urllib.parse.quote(",".join(chunk)))
            raw = opener.open(url, timeout=20).read().decode("utf-8", "replace")
            j = json.loads(raw)
            for q in j.get("quoteResponse", {}).get("result", []):
                sym = str(q.get("symbol", "")).upper()
                if not sym:
                    continue
                out[sym] = {
                    "pe": q.get("trailingPE"),
                    "fwd_pe": q.get("forwardPE"),
                    "pb": q.get("priceToBook"),
                    "off_52w_high_pct": (
                        round((q.get("regularMarketPrice", 0) / q.get("fiftyTwoWeekHigh", 0) - 1) * 100, 1)
                        if q.get("fiftyTwoWeekHigh") else None),
                }
        except Exception as e:
            log(f"⚠️ 밸류에이션 수집 실패(chunk {i}): {e} → 기술적 지표로만 판정")
        time.sleep(0.3)
    if out:
        log(f"✅ 밸류에이션 수집: {len(out)}종목 (PER·PBR)")
    return out


def fetch_latest_prices(symbols):
    """알파카 snapshot으로 종목들의 '최신 체결가'를 batch 수집.
    정규장 마감 후에도 애프터마켓 체결가를 반영하므로, 실적 발표 등으로
    장 외 급변한 가격을 봇이 볼 수 있게 한다. 실패해도 {}를 반환(있으면 쓰고 없으면 일봉 종가).
    코인 제외(별도 처리)."""
    out = {}
    syms = [s for s in symbols if not is_crypto(s)]
    if not syms:
        return out
    for i in range(0, len(syms), 100):
        chunk = syms[i:i + 100]
        try:
            url = ("https://data.alpaca.markets/v2/stocks/snapshots?feed=iex&symbols="
                   + urllib.parse.quote(",".join(chunk)))
            j = json.loads(_get(url, headers={"APCA-API-KEY-ID": ALPACA_KEY,
                                              "APCA-API-SECRET-KEY": ALPACA_SECRET}))
            # snapshots 응답: {symbol: {latestTrade:{p:가격,t:시각}, dailyBar:{c}, ...}}
            data = j if isinstance(j, dict) else {}
            # 일부 응답은 {"snapshots":{...}} 형태일 수 있음
            if "snapshots" in data:
                data = data["snapshots"]
            for sym, snap in data.items():
                if not isinstance(snap, dict):
                    continue
                lt = snap.get("latestTrade") or {}
                price = lt.get("p")
                if price:
                    out[str(sym).upper()] = float(price)
        except Exception as e:
            log(f"⚠️ 최신가 수집 실패(chunk {i}): {e} → 일봉 종가로 대체")
        time.sleep(0.3)
    if out:
        log(f"✅ 애프터마켓 최신가 수집: {len(out)}종목")
    return out


def fetch_daily(sym):
    errors = []
    for fn in (fetch_alpaca_data, fetch_stooq, fetch_yahoo):
        try:
            rows = fn(sym)
            if len(rows) >= 25:
                rows.sort(key=lambda r: r["date"])
                return rows[-160:]
            errors.append(f"{fn.__name__}: 데이터 부족({len(rows)}행)")
        except Exception as e:
            errors.append(f"{fn.__name__}: {e}")
    raise RuntimeError(" / ".join(errors))


# ── 지표 요약 (Claude에게 줄 재료) ──
def sma(vals, n, i):
    if i < n - 1:
        return None
    return sum(vals[i - n + 1:i + 1]) / n


def support_resistance(closes, cur, k=3, cluster_pct=1.5, max_each=4):
    """종가 기준 스윙 고/저점을 찾아 가격대로 군집화 → 지지·저항대 산출.
    k: 좌우 비교 봉 수(클수록 큰 스윙만). cluster_pct: 이 %내 점들은 한 대역으로 묶음.
    반환: {"support":[현재가 아래 가까운 순], "resistance":[현재가 위 가까운 순]}"""
    if len(closes) < 2 * k + 5 or not cur:
        return {"support": [], "resistance": []}
    pivots = []
    for i in range(k, len(closes) - k):
        seg = closes[i - k:i + k + 1]
        if closes[i] == max(seg):
            pivots.append(closes[i])   # 스윙 고점
        elif closes[i] == min(seg):
            pivots.append(closes[i])   # 스윙 저점
    if not pivots:
        return {"support": [], "resistance": []}
    # 가까운 가격끼리 군집화
    pivots.sort()
    clusters = [[pivots[0]]]
    for p in pivots[1:]:
        if abs(p - clusters[-1][-1]) / clusters[-1][-1] * 100 <= cluster_pct:
            clusters[-1].append(p)
        else:
            clusters.append([p])
    # 대역 중심값 + 닿은 횟수(강도)
    levels = [{"price": round(sum(c) / len(c), 2), "touches": len(c)} for c in clusters]
    sup = sorted([l for l in levels if l["price"] < cur], key=lambda x: cur - x["price"])[:max_each]
    res = sorted([l for l in levels if l["price"] > cur], key=lambda x: x["price"] - cur)[:max_each]
    return {"support": sup, "resistance": res}


def fib_retracement(closes, lookback=60):
    """최근 lookback일의 스윙 최저→최고로 피보나치 되돌림 라인 계산.
    반환: {"swing_low","swing_high","up": bool, "levels":{...}} (없으면 None)"""
    if len(closes) < 10:
        return None
    seg = closes[-lookback:]
    lo, hi = min(seg), max(seg)
    if hi <= lo:
        return None
    lo_i, hi_i = seg.index(lo), seg.index(hi)
    up = hi_i > lo_i   # 저점이 먼저면 상승 스윙(되돌림은 아래로)
    diff = hi - lo
    # 상승 스윙이면 고점에서 아래로 되돌림, 하락 스윙이면 저점에서 위로 되돌림
    if up:
        levels = {r: round(hi - diff * f, 2) for r, f in
                  (("382", 0.382), ("500", 0.5), ("618", 0.618))}
    else:
        levels = {r: round(lo + diff * f, 2) for r, f in
                  (("382", 0.382), ("500", 0.5), ("618", 0.618))}
    return {"swing_low": round(lo, 2), "swing_high": round(hi, 2), "up": up, "levels": levels}


def taco_zone(cur, sr, fib):
    """저가매수 후보 밴드 = 현재가 바로 아래 지지대와 피보나치 되돌림(0.5~0.618)이
    겹치거나 가까운 구간. 반환: {"low","high","basis"} 또는 None."""
    if not cur:
        return None
    cands = []
    # 가장 가까운 아래 지지대
    if sr and sr.get("support"):
        cands.append(("지지대", sr["support"][0]["price"]))
    # 피보나치 0.5 / 0.618 중 현재가 아래인 것
    if fib:
        for r in ("500", "618"):
            v = fib["levels"].get(r)
            if v and v < cur:
                cands.append((f"피보 {r[0]}.{r[1:]}", v))
    if not cands:
        return None
    prices = [p for _, p in cands]
    lo, hi = min(prices), max(prices)
    # 밴드가 현재가에서 너무 멀면(>15%) 의미 약함 → 그래도 표시는 하되 basis에 표기
    basis = "+".join(sorted({n for n, _ in cands}))
    # 밴드가 한 점이면 ±0.8% 폭 부여
    if hi - lo < cur * 0.003:
        mid = (hi + lo) / 2
        lo, hi = round(mid * 0.992, 2), round(mid * 1.008, 2)
    return {"low": round(lo, 2), "high": round(hi, 2), "basis": basis}


def summarize(sym, series):
    closes = [r["close"] for r in series]
    vols = [r["volume"] for r in series]
    i = len(closes) - 1
    ma5, ma20, ma60 = sma(closes, 5, i), sma(closes, 20, i), sma(closes, 60, i)
    g = l = 0.0
    rsi = None
    for k in range(1, len(closes)):
        ch = closes[k] - closes[k - 1]
        gain, loss = max(ch, 0), max(-ch, 0)
        if k <= 14:
            g += gain / 14
            l += loss / 14
        else:
            g = (g * 13 + gain) / 14
            l = (l * 13 + loss) / 14
        if k >= 14:
            rsi = 100 - 100 / (1 + (100 if l == 0 else g / l))
    var = sum((c - ma20) ** 2 for c in closes[-20:]) / 20 if ma20 else 0
    sd = var ** 0.5
    vol_ratio = vols[-1] / (sum(vols[-20:]) / 20) if sum(vols[-20:]) else 1
    chg5 = (closes[-1] / closes[-6] - 1) * 100 if len(closes) > 6 else 0
    chg20 = (closes[-1] / closes[-21] - 1) * 100 if len(closes) > 21 else 0
    cur = closes[-1]
    # 52주(약 252거래일) 고점 대비 낙폭 — 가격 데이터만으로 계산(야후 인증 불필요).
    # 밸류에이션 수집이 실패해도 '얼마나 빠졌나'는 항상 제공돼 저가매수 판단에 쓰임.
    hi_252 = max(closes[-252:]) if len(closes) >= 20 else max(closes)
    off_52w = round((cur / hi_252 - 1) * 100, 1) if hi_252 else None
    sr = support_resistance(closes, cur)
    fib = fib_retracement(closes)
    tz = taco_zone(cur, sr, fib)
    # 현재가가 TACO ZONE 안에 있는지 (저가매수 후보 진입 여부)
    in_taco = bool(tz and tz["low"] <= cur <= tz["high"])
    return {
        "symbol": sym,
        "price": round(cur, 2),
        "chg_5d_pct": round(chg5, 1),
        "chg_20d_pct": round(chg20, 1),
        "ma5": round(ma5, 2) if ma5 else None,
        "ma20": round(ma20, 2) if ma20 else None,
        "ma60": round(ma60, 2) if ma60 else None,
        "disparity_ma20_pct": round((cur / ma20 - 1) * 100, 1) if ma20 else None,
        "rsi14": round(rsi, 1) if rsi else None,
        "bollinger_pos_0to1": round((cur - (ma20 - 2 * sd)) / (4 * sd), 2) if ma20 and sd else None,
        "volume_vs_20d_avg": round(vol_ratio, 2),
        "support": [l["price"] for l in sr["support"]],
        "resistance": [l["price"] for l in sr["resistance"]],
        "fib": fib["levels"] if fib else None,
        "taco_zone": tz,
        "in_taco_zone": in_taco,
        "off_52w_high_pct": off_52w,
    }


def assess_regime():
    """시장 전체 국면 판단 — SPY 추세 + VIX로 하락장/조정/상승장 구분.
    역대 하락장(2008, 2020, 2022)의 공통 신호를 기준으로 함."""
    regime = {"label": "불명", "detail": "", "vix": None, "spy_vs_ma20": None, "spy_vs_ma60": None}
    try:
        spy = fetch_daily("SPY")
        closes = [r["close"] for r in spy]
        i = len(closes) - 1
        ma20 = sma(closes, 20, i)
        ma60 = sma(closes, 60, i)
        price = closes[-1]
        spy_ma20 = round((price / ma20 - 1) * 100, 1) if ma20 else None
        spy_ma60 = round((price / ma60 - 1) * 100, 1) if ma60 else None
        # 고점 대비 낙폭 (최근 120일 고점)
        hi = max(closes[-120:]) if len(closes) >= 20 else max(closes)
        drawdown = round((price / hi - 1) * 100, 1)
        regime["spy_vs_ma20"] = spy_ma20
        regime["spy_vs_ma60"] = spy_ma60
        regime["spy_drawdown_from_high_pct"] = drawdown

        vix_val = None
        try:
            vix = fetch_daily("^VIX") if False else None  # VIX 직접 조회는 소스마다 다름 — 아래 대체
        except Exception:
            vix = None
        # VIX는 별도 시도 (실패해도 무방)
        try:
            import urllib.request
            url = "https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX?range=5d&interval=1d"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as r:
                vj = json.load(r)
            vix_val = vj["chart"]["result"][0]["meta"].get("regularMarketPrice")
        except Exception:
            vix_val = None
        regime["vix"] = round(vix_val, 1) if vix_val else None

        # 국면 판정 (역대 하락장 교훈 기반)
        # 하락장: SPY가 60일선 아래 + 고점 대비 -10% 이상 + (VIX 높으면 강화)
        if spy_ma60 is not None and drawdown is not None:
            if spy_ma60 < -2 and drawdown <= -10:
                regime["label"] = "하락장/조정"
                regime["detail"] = "SPY가 장기추세(60일선) 아래이고 고점 대비 큰 폭 하락. 방어 우선 국면."
            elif spy_ma20 is not None and spy_ma20 < -3:
                regime["label"] = "단기 약세"
                regime["detail"] = "단기추세(20일선) 이탈. 신규 매수 신중, 손절 기준 엄격 적용."
            elif spy_ma20 is not None and spy_ma20 > 1 and (vix_val is None or vix_val < 20):
                regime["label"] = "상승장"
                regime["detail"] = "추세 양호, 변동성 안정. 정상 운용 가능."
            else:
                regime["label"] = "중립/혼조"
                regime["detail"] = "뚜렷한 추세 없음. 종목별 선별 대응."
        if vix_val is not None and vix_val >= 30:
            regime["detail"] += " VIX 30 이상 — 공포 극심, 변동성 매우 큼."
    except Exception as e:
        regime["detail"] = f"국면 판단 실패: {e}"
    return regime


# ── 섹터 흐름(로테이션) 분석 ──
# 주요 섹터 ETF의 상대 강도를 비교해 "돈이 어디로 도는지" 파악.
# 개별 텐버거는 못 골라도, 주도 섹터(샌디스크式 반도체 흐름 등)는 데이터로 읽을 수 있다.
SECTOR_ETFS = {
    "SOXX": "반도체",
    "XLK": "기술",
    "QQQ": "빅테크/나스닥",
    "XLF": "금융",
    "XLE": "에너지",
    "XLV": "헬스케어",
    "XBI": "바이오",
    "XLY": "임의소비재",
    "XLP": "필수소비재",
    "XLI": "산업재",
    "XLB": "소재",
    "XLU": "유틸리티",
    "XLRE": "부동산",
    "XLC": "커뮤니케이션",
}

def assess_sectors():
    """섹터별 1개월·3개월 수익률로 상대 강도 순위를 매긴다.
    주도 섹터(강세)와 소외 섹터(약세)를 구분해 봇이 흐름을 타게 한다."""
    sectors = []
    for sym, name in SECTOR_ETFS.items():
        try:
            bars = fetch_daily(sym)
            closes = [r["close"] for r in bars if r.get("close")]
            if len(closes) < 65:
                continue
            now = closes[-1]
            r1m = (now / closes[-21] - 1) * 100 if len(closes) >= 21 else None   # 약 1개월(21거래일)
            r3m = (now / closes[-63] - 1) * 100 if len(closes) >= 63 else None   # 약 3개월(63거래일)
            ma20 = sma(closes, 20, len(closes) - 1)
            above_ma20 = (now > ma20) if ma20 else None
            sectors.append({
                "sym": sym, "name": name,
                "ret_1m_pct": round(r1m, 1) if r1m is not None else None,
                "ret_3m_pct": round(r3m, 1) if r3m is not None else None,
                "above_ma20": above_ma20,
            })
        except Exception:
            continue
    if not sectors:
        return {"ranked": [], "leaders": [], "laggards": [], "summary": "섹터 데이터 수집 실패"}
    # 1개월 수익률 기준 정렬 (없으면 3개월)
    sectors.sort(key=lambda s: (s["ret_1m_pct"] if s["ret_1m_pct"] is not None else
                                (s["ret_3m_pct"] if s["ret_3m_pct"] is not None else -999)), reverse=True)
    leaders = sectors[:3]
    laggards = sectors[-3:]
    lead_str = ", ".join(f"{s['name']}({s['ret_1m_pct']:+.1f}%)" for s in leaders if s["ret_1m_pct"] is not None)
    lag_str = ", ".join(f"{s['name']}({s['ret_1m_pct']:+.1f}%)" for s in laggards if s["ret_1m_pct"] is not None)
    return {
        "ranked": sectors,
        "leaders": [s["sym"] for s in leaders],
        "laggards": [s["sym"] for s in laggards],
        "summary": f"주도 섹터: {lead_str} / 소외 섹터: {lag_str}",
    }


# ── 암호화폐 지원 ──
CRYPTO = {"ETH-USD"}                     # 코인은 이더리움만 (BTC는 알파카 모의계좌 체결 문제)


def is_crypto(sym):
    return sym.upper() in CRYPTO


def to_alpaca_symbol(sym):
    """BTC-USD → BTC/USD (알파카 주문용)"""
    return sym.replace("-", "/") if is_crypto(sym) else sym


def normalize_position_symbol(sym):
    """알파카 포지션의 BTCUSD → BTC-USD 로 통일"""
    for c in CRYPTO:
        if sym.upper() == c.replace("-", ""):
            return c
    return sym


# ── Alpaca 모의계좌 ──
def alpaca(path, method="GET", body=None):
    return http_json(ALPACA_BASE + path, method=method, body=body,
                     headers={"APCA-API-KEY-ID": ALPACA_KEY,
                              "APCA-API-SECRET-KEY": ALPACA_SECRET})


def get_market_session():
    """미국 주식 세션을 단일 진실 소스로 판정.
    반환: 'regular'(정규장) | 'pre'(프리마켓) | 'after'(애프터마켓) | 'closed'(휴장/주말)
    알파카 클락의 is_open + 현재시각(ET) + 다음 개장/폐장 시각으로 세션을 구분한다.
    두 AI(Claude·DeepSeek)에게 동일하게 주입해 판단을 통일하는 것이 목적."""
    try:
        clock = alpaca("/v2/clock")
    except Exception:
        return "closed"
    if clock.get("is_open"):
        return "regular"
    # 정규장이 아닐 때 pre/after/closed 구분 (ET 기준 시각 파싱)
    def _parse_et(ts):
        # 예: "2026-06-16T09:30:00-04:00"
        try:
            return datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except Exception:
            return None
    now = _parse_et(clock.get("timestamp", ""))
    nxt_open = _parse_et(clock.get("next_open", ""))
    nxt_close = _parse_et(clock.get("next_close", ""))
    def _closed_kind():
        # 거래 시간 외를 주말/공휴일/평일 시간외로 구분 (라벨 정확도용. 거래는 모두 불가)
        if not now:
            return "closed_offhours"
        wd = now.weekday()  # 0=월 ~ 6=일
        if wd >= 5:
            return "closed_weekend"
        # 평일인데 닫힘: 다음 개장이 '모레 이후'면 공휴일로 추정(평일 하루 통째로 닫힘)
        try:
            if nxt_open and (nxt_open.date() - now.date()).days >= 2:
                return "closed_holiday"
        except Exception:
            pass
        return "closed_offhours"   # 평일 장 마감 후 또는 개장 전

    if not now:
        return "closed_offhours"
    try:
        if nxt_open and now.date() == nxt_open.date():
            if now < nxt_open:
                return "pre"
        if nxt_open and nxt_close and now.date() != nxt_open.date():
            if 16 <= now.hour < 20:
                return "after"
        return _closed_kind()
    except Exception:
        return "closed_offhours"


_SESSION_KR = {"regular": "정규장(개장 중)", "pre": "프리마켓(개장 전)",
               "after": "애프터마켓(폐장 후)",
               "closed_offhours": "거래 시간 외 (미국 장 마감 상태)",
               "closed_weekend": "주말 휴장",
               "closed_holiday": "공휴일 휴장",
               "closed": "거래 시간 외 (미국 장 마감 상태)"}  # 하위호환


def market_is_open():
    """하위호환용: 정규장이면 True. (기존 호출부 유지)"""
    return get_market_session() == "regular"


# ── Claude에게 판단 요청 ──
def ask_claude(account, positions, market, regime=None, session="regular"):
    session_kr = _SESSION_KR.get(session, session)
    # 세션별로 두 AI에 동일한 거래 규칙을 주입 (판단 통일의 핵심)
    if session == "regular":
        session_rule = (
            f"[현재 미국장 세션] {session_kr}\n"
            "  → 정규장입니다. 평소 규칙대로 주식·코인 모두 정상 판단하세요.\n"
        )
    elif session == "after":
        session_rule = (
            f"[현재 미국장 세션] {session_kr}\n"
            "  → 애프터마켓입니다. 이 봇은 애프터마켓에도 거래하되 매우 신중하게 합니다. 핵심 주의사항:\n"
            "  · 실적 발표가 애프터마켓에 많이 나오므로, 보유 종목이 급변(afterhours_chg_pct 참고)했다면 대응(손절·익절)을 우선 검토하세요.\n"
            "  · 각 종목 price는 '애프터마켓 실시간 체결가'로 갱신돼 있고, regular_close(정규장 종가)·afterhours_chg_pct(장 외 변동%)가 함께 제공됩니다.\n"
            "  · 애프터마켓은 호가가 얇아 체결이 왜곡되기 쉽습니다. 이미 크게 급등한 종목을 추격 매수하는 것은 위험하니 신중하세요(신규 매수는 정말 확신될 때만, 포지션도 절반으로 축소됩니다).\n"
            "  · 손절은 보유 종목 보호를 위해 적극적으로, 신규 매수는 보수적으로. 애매하면 정규장까지 기다리세요.\n"
            "  · 주문은 시장가가 아니라 지정가로 나갑니다(체결이 안 될 수도 있음을 감안).\n"
        )
    else:
        session_rule = (
            f"[현재 미국장 세션] {session_kr}\n"
            f"  → 지금은 미국 주식 거래 시간이 아닙니다(위 세션 상태 그대로 표현하세요. "
            "정규장이 아닐 뿐 '휴장'으로 단정하지 말 것 — 평일 장 마감 후일 수 있음). "
            "미국 주식(코인 제외)에 대한 매수·매도 결정을 절대 내지 마세요. 주식 관련 decisions는 "
            "모두 빈 배열로 두고, 코인(예: ETH-USD)만 근거가 분명할 때 거래하세요. "
            "market_view에는 위 세션 상태를 정확히 반영해 '거래 시간이 아니라 주식은 관망'임을 명시하세요.\n"
        )
    prompt = (
        "당신은 미국 주식 포트폴리오 매니저입니다. 모의계좌를 운용 중입니다.\n"
        "기술적 지표 기반의 스윙 전략을 따르되, 확신이 없으면 거래하지 않는 것이 원칙입니다.\n\n"
        "핵심 매매 철학 (반드시 따를 것):\n"
        "- '기다리는 매매'가 가장 중요하다. 아무 때나 사지 말고, 좋은 자리가 올 때까지 기다린다. 애매하면 거래하지 않는 것이 정답.\n"
        "- 두 가지 진입 방식을 시장 국면에 맞게 골라 쓴다:\n"
        "  ① 저점매수(쌀 때 사서 비쌀 때 판다): 과매도(RSI 30 이하)·볼린저 하단·공포 극심 구간에서 역발상 매수. 단, 떨어지는 칼날 잡지 말고 하락이 멈추고 바닥 다지는 신호(거래량 동반 반등, MA5 회복) 확인 후 진입.\n"
        "    └ 괴리율 역추세 매매 (일본 전설 트레이더 BNF 기법): 주가가 20~25일 이동평균선 대비 과도하게 아래로 벌어졌을 때(예: -10% 이상 괴리), 평균으로 되돌아오는 반등을 노린다. 단 다음 조건을 지킬 것:\n"
        "      · 대형주·우량주 한정: 변동성 큰 잡주·소형주에는 적용 금지(망할 위험·반등 불확실). 시총 크고 펀더멘털 견고한 종목만.\n"
        "      · 반등 신호 필수: 단지 '많이 빠졌다'가 아니라, 하락이 멈추고 돌아서는 신호(MA5 회복·거래량 동반 반등·RSI 과매도 탈출)가 확인될 때만. 괴리만 보고 사지 말 것.\n"
        "      · 하락장/조정 국면에서는 적용 금지: 평균회귀가 깨지는 구간이라 위험. 상승장·중립 국면에서만.\n"
        "      · 업종 확산 참고: 같은 업종 대장주가 먼저 반등하면 관련 우량주로 확산되는 경향도 함께 고려.\n"
        "      · 각 종목 지표의 disparity_ma20_pct가 20일선 괴리율(%)이다. 음수로 크게 벌어진 우량주에서 반등 신호가 보이면 BNF식 역추세 기회로 판단하라.\n"
        "  ② 추세추종(비쌀 때 사서 더 비쌀 때 판다): 골든크로스·신고가 돌파·강한 상승추세에서 달리는 추세에 올라탐. 거래량이 뒷받침될 때만.\n"
        "- 시장 국면으로 둘 중 무엇을 쓸지 결정한다: 하락장/과매도 국면 → ①저점매수 위주(신중히). 상승장/추세 국면 → ②추세추종 위주. 혼조·불명확 → 기다림(관망).\n"
        "- 두 방식 모두 '좋은 자리를 기다린다'는 본질은 같다. 조급하게 진입하지 말 것.\n\n"
        "포트폴리오 구조 (중요):\n"
        "- 코어: M7(AAPL/MSFT/NVDA/GOOGL/AMZN/META/TSLA)과 기술 ETF(IGV/SOXX/SMH/QQQ)를 성장 축으로 운용\n"
        "- 분산: 금융·헬스케어·에너지·소비재·안전자산(GLD/TLT)에도 나눠 담아 한 섹터 쏠림을 피할 것\n"
        "- 레버리지(TQQQ/SOXL/UPRO/QLD/TNA/FNGU/TECL/LABU/NVDL/TSLL): 3배 변동이라 위험. 상승 추세가 명확할 때만 단기 전술용으로. 확신이 강하면 종목당 최대 8%까지, 아니면 5% 이내. 레버리지+인버스 합산 평가액은 총자산의 15%를 절대 넘기지 말 것\n"
        "- 인버스(SQQQ/SOXS/SH/SDS): 지수가 20일선 아래로 꺾이는 등 하락 신호가 분명할 때 헤지용으로만. 같은 15% 한도 적용\n"
        "- 레버리지·인버스는 변동성 잠식이 있으니 보유가 길어지거나 근거가 사라지면 우선 정리 대상\n"
        "- RSI 과매도 + 볼린저 하단 같은 '싸게 살 기회' 역발상도 적극 활용\n- 암호화폐(BTC-USD/ETH-USD): 시험 운용 중. 변동성이 매우 크니 합산 평가액 총자산 5% 이내, 소수점 수량 사용 가능 (예: 0.02)\n\n"
        "하락장·조정 대응 원칙 (역대 약세장 2008·2020·2022의 교훈):\n"
        "- 현금은 무기다: 하락장에선 현금 비중을 늘리는 것 자체가 좋은 결정. 억지로 매수하지 말 것. 관망·현금 보유가 종종 최선\n"
        "- 떨어지는 칼날 잡지 말 것: 급락 중인 종목을 '싸다'고 서둘러 사지 말 것. 하락 추세가 멈추고 바닥을 다지는 신호(거래량 동반 반등, MA5 회복)를 확인 후 진입\n"
        "- 손절은 빠르게: 하락장에선 손실이 더 빨리 커진다. 손절 기준(-7%, 레버리지 -5%)을 더 엄격히, 머뭇거리지 말 것\n"
        "- 분할 대응: 한 번에 다 사지 말고, 확신이 설 때 조금씩. 평균단가 낮추기(물타기)는 추세 확인 전엔 금물\n"
        "- 레버리지 금지에 가깝게: 하락·변동성 국면에서 레버리지 롱(TQQQ 등)은 변동성 잠식으로 치명적. 추세가 명확히 돌기 전엔 피할 것\n"
        "- 방어 자산: 하락장에선 GLD(금)·TLT(국채) 같은 안전자산 비중을 고려. 인버스(SQQQ 등)는 하락이 분명할 때만 소량 헤지\n"
        "- 공포에 휩쓸리지 말되 과신도 말 것: VIX가 극도로 높을 때(30+)는 역사적 저점 근처인 경우도 있으나, 섣부른 '바닥 매수'보다 안정 확인이 우선\n\n"
        "추가 매수(물타기로 평균단가 낮추기) 원칙 — 규율을 지킬 때만:\n"
        "- 보유 종목이 평단 대비 하락했을 때, 무작정 더 사지 말 것. 아래 조건이 '모두' 충족될 때만 추가 매수를 고려:\n"
        "  ① 추세 반전 확인: 하락이 멈추고 바닥을 다지는 신호 — 종가가 MA5를 회복, 또는 거래량 동반 반등, 또는 RSI가 과매도(30 이하)에서 상승 전환\n"
        "  ② 평단 대비 의미 있는 하락: 평단보다 -5% 이상 빠진 상태 (조금 빠졌다고 사지 말 것)\n"
        "  ③ 펀더멘털·시장환경이 여전히 유효: 종목이 망가진 게 아니라 시장 전체 조정에 휩쓸린 경우. 개별 악재로 빠진 거면 추가 매수 금지\n"
        "  ④ 하락장/조정 국면이 아닐 것: 시장 전체가 하락장이면 물타기는 위험하니 금지\n"
        "- 추가 매수도 분할로: 한 번에 평단을 다 낮추려 하지 말고, 반등 확인하며 나눠서. 추가 후에도 그 종목 합산은 포지션 한도(보통 5%, 강한확신 12%) 이내\n"
        "- 손절 우선순위가 물타기보다 높다: 추세 반전이 확인 안 되고 계속 빠지면, 물타기가 아니라 손절(-7%)을 검토. 떨어지는 칼날에 계속 돈을 넣는 건 가장 위험한 실수\n\n"
        f"하이리스크 슬롯 (저평가·과매도 역추세 전용, 격리 예산 총자산의 {int(MAX_HIGHRISK_BUDGET_PCT*100)}%):\n"
        "- 목적: 빅테크·지수·방어주 위주의 안전한 코어 외에, '지금 저평가·과매도된 종목의 반등'을 노리는 하이리스크 하이리턴 칸을 별도로 둔다.\n"
        f"- 격리 원칙: 하이리스크 포지션들의 '합계'는 총자산의 {int(MAX_HIGHRISK_BUDGET_PCT*100)}%를 넘지 않는다(시스템이 강제). 한 종목당 최대 {int(MAX_HIGHRISK_POSITION_PCT*100)}%. 코어 예산과 분리되어, 물려도 전체가 휘청이지 않게 칸막이.\n"
        "- 저평가 판정은 '두 가지를 결합'한다:\n"
        "  ① 기술적 과매도 반등: disparity_ma20_pct가 음수로 크게 벌어짐(예: -10% 이하) + RSI 과매도권 + 반등 신호(MA5 회복·거래량 동반 반등·RSI 과매도 탈출). '많이 빠졌다'만으로는 부족, 돌아서는 신호가 핵심.\n"
        "    └ 지지/저항·피보나치 활용: 각 종목 지표의 support(아래 지지대 가격들)·resistance(위 저항대)·fib(피보나치 되돌림 38.2/50/61.8%)·taco_zone(저가매수 후보 밴드 {low,high,basis})·in_taco_zone(현재가가 그 밴드 안인지)을 본다. 현재가가 강한 지지대나 피보 0.5~0.618 되돌림에 닿았고(=in_taco_zone true) 거기서 반등 신호가 나오면 저가매수 자리로 판단. 단 '지지에 닿음'만으론 부족, 매물대가 깨지면 손절(차트는 미래 보장이 아니라 확률 가이드일 뿐).\n"
        "  ② 밸류에이션 저평가: 각 종목 지표의 valuation 필드(pe=PER, fwd_pe=선행PER, pb=PBR, off_52w_high_pct=52주고점대비낙폭%)를 본다. 동종 섹터·과거 대비 PER/PBR이 낮은데 펀더멘털은 망가지지 않은 종목. (valuation이 없으면 ①기술적 신호로만 판정)\n"
        "  → ① 또는 ② 중 하나라도 분명하고, 펀더멘털이 망가진 게 아니면 하이리스크 후보. 둘 다 충족이면 더 강한 신호.\n"
        "- 하락장 예외(중요): BNF 역추세는 원래 하락장 금지지만, 하이리스크 슬롯에 한해 '확실한 추세 전환 신호'가 보이면 하락장에서도 저가매수를 시도할 수 있다. 단 매우 신중히 — 추세 전환이 애매하면 하지 말 것. 막연한 '싸 보임'은 금지, 반드시 돌아서는 신호 확인.\n"
        "- 레버리지(TQQQ 등)도 하이리스크 슬롯에 포함 가능. 단 한 종목 한도 5%는 동일.\n"
        f"- 각 주문에 \"tier\" 필드를 넣어라: \"core\"(일반 안전 매수) | \"highrisk\"(저평가·과매도 역추세). 미지정 시 core로 간주.\n"
        "- 하이리스크라도 두 AI(Claude·DeepSeek)가 모두 동의해야 실제 매수된다(합의 원칙 동일). 손절은 한쪽만 원해도 실행.\n\n"
        f"[현재 시장 국면] {json.dumps({k:v for k,v in regime.items() if k!='sectors'}, ensure_ascii=False) if regime else '판단 안 됨'}\n"
        "  → 위 국면을 반드시 반영할 것. '하락장/조정'이나 '단기 약세'면 신규 매수를 크게 줄이고 방어·현금 우선. '상승장'이면 정상 운용.\n"
        + (f"[섹터 흐름] {(regime or {}).get('sectors',{}).get('summary','')}\n"
           f"  섹터별 1·3개월 수익률: {json.dumps((regime or {}).get('sectors',{}).get('ranked',[]), ensure_ascii=False)}\n"
           "  → 돈이 어디로 도는지(로테이션) 파악하라. 주도 섹터(강세)의 우량주에 무게를 두고, 소외 섹터는 신중히. 단, 이미 많이 오른 섹터를 뒤늦게 추격하는 건 경계(고점 매수 위험). 섹터 흐름은 '근거'로 참고하되 개별 종목 신호·펀더멘털과 함께 종합 판단할 것.\n\n"
           if (regime or {}).get('sectors') else "\n")
        + session_rule
        + f"[계좌] 총자산 ${account['equity']}, 현금 ${account['cash']}\n"
        f"[보유 포지션]\n{json.dumps(positions, ensure_ascii=False, indent=1)}\n\n"
        f"[관심종목 지표]\n{json.dumps(market, ensure_ascii=False, indent=1)}\n\n"
        "규칙:\n"
        f"- 신규 매수 한도(종목당 총자산 대비): 확신이 보통이면 {int(MAX_POSITION_PCT*100)}%, 확신이 강하면 최대 {int(MAX_POSITION_HIGH*100)}%, 확신이 약하면 3% 이내\n"
        "- 각 주문에 \"conviction\" 필드를 넣으세요: \"high\"(강한 확신) | \"normal\"(보통) | \"low\"(시험적). 신호·펀더멘털·시장국면이 모두 우호적이고 자리가 분명할 때만 high.\n"
        "- high(큰 포지션)는 신중히: 한 종목에 자산을 크게 싣는 건 위험합니다. 정말 확신이 설 때만, 그리고 분할로 나눠 사세요(한 번에 한도를 다 채우지 말고 여러 번에 걸쳐).\n"
        "- 인버스 종목과 하락장·조정 국면에서는 conviction과 무관하게 5% 이내로 제한됩니다(시스템이 강제). 레버리지는 확신이 강할 때만 최대 8%.\n"
        "- 매수는 관심종목 내에서만, 매도는 보유 종목만, 공매도 금지\n"
        "- 손실 중인 포지션이 -7% 이하면 손절을 적극 검토 (레버리지는 -5%)\n"
        "- 거래할 이유가 약하면 빈 배열로 응답 (거래 안 함이 기본값)\n\n"
        "아래 JSON 형식으로만 응답하세요. 다른 텍스트, 마크다운 백틱 금지:\n"
        '{"decisions":[{"action":"buy|sell","symbol":"JPM","qty":3,"conviction":"normal","tier":"core","reason":"한 문장 근거"}],'
        '"market_view":"오늘 시장 국면 판단, 왜 매수/매도/관망했는지 핵심 근거, 포트폴리오 분산·레버리지·현금 비중에 대한 평가를 자세히. 나중에 사람이 봇의 판단을 복기할 수 있도록 솔직하고 구체적으로 충분히 설명하되, 5~8문장(800자 내외)을 넘지 말 것. 반드시 완결된 JSON으로 끝맺고 마지막 문장은 마침표로 끝낼 것."}'
    )
    res = http_json(
        "https://api.anthropic.com/v1/messages", method="POST",
        headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        body={"model": "claude-sonnet-4-6", "max_tokens": 4000,
              "messages": [{"role": "user", "content": prompt}]})
    text = "".join(b.get("text", "") for b in res.get("content", []))
    claude_plan = _parse_ai_json(text, "Claude")

    # ── DeepSeek 파트너 호출 (합의 방식) ──
    # DeepSeek이 독립적으로 판단 → 둘 다 동의한 매수만 실행. 손절은 한쪽만 원해도 실행(방어 우선).
    # DeepSeek 오류 시 Claude 단독으로 진행.
    if not DEEPSEEK_KEY:
        log("ℹ️ DeepSeek 키 없음 → Claude 단독 운용")
        return claude_plan
    try:
        ds_res = http_json(
            "https://api.deepseek.com/chat/completions", method="POST",
            headers={"Authorization": f"Bearer {DEEPSEEK_KEY}",
                     "content-type": "application/json"},
            body={"model": "deepseek-chat", "max_tokens": 4000,
                  "messages": [{"role": "user", "content": prompt}]})
        ds_text = ds_res.get("choices", [{}])[0].get("message", {}).get("content", "")
        deepseek_plan = _parse_ai_json(ds_text, "DeepSeek")
        log("✅ DeepSeek 판단 수신 — 합의 방식 적용")
        return _consensus(claude_plan, deepseek_plan)
    except Exception as e:
        log(f"⚠️ DeepSeek 호출 실패 ({e}) → Claude 단독으로 진행")
        return claude_plan


def _parse_ai_json(text, who):
    """AI 응답에서 JSON 추출. 응답이 토큰 한도로 잘려 JSON이 미완성이어도
    decisions 배열만이라도 복구해 거래는 진행되게 한다(market_view는 부가 정보)."""
    text = (text or "").replace("```json", "").replace("```", "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass  # 아래 복구 시도로

    # ── 복구: 응답이 잘려 완전한 JSON이 아닐 때 decisions 배열만 추출 ──
    if start != -1:
        import re
        m = re.search(r'"decisions"\s*:\s*(\[.*?\])', text[start:], re.DOTALL)
        if m:
            try:
                decisions = json.loads(m.group(1))
                # market_view도 가능한 만큼 살림 (잘렸으면 거기까지만)
                mv = ""
                mvm = re.search(r'"market_view"\s*:\s*"(.*)', text[start:], re.DOTALL)
                if mvm:
                    mv = mvm.group(1)
                    # 끝의 미완성 따옴표·중괄호 정리
                    mv = mv.rstrip().rstrip('"').rstrip('}').rstrip().rstrip('"')
                log(f"⚠️ {who} 응답이 잘려 부분 복구: 거래 {len(decisions)}건 추출"
                    + (" (market_view 일부 손실)" if not mv else ""))
                return {"decisions": decisions, "market_view": mv}
            except json.JSONDecodeError:
                pass

    raise ValueError(f"{who} 응답에 JSON이 없음: " + text[:200])


def _clip_sentence(text, limit=1000):
    """알림/표시용으로 길이를 제한하되 문장 중간에서 안 끊기게.
    limit를 넉넉히 두어 평소엔 거의 안 자르고, 넘칠 때만 마지막 문장
    종결부호(. ! ? 까지)에서 깔끔하게 자른다. 종결부호가 없으면 말줄임표."""
    if not text:
        return text
    text = text.rstrip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    idx = max(cut.rfind("."), cut.rfind("!"), cut.rfind("?"),
              cut.rfind("。"), cut.rfind("…"))
    if idx > limit * 0.5:          # 너무 앞에서 끊기면 차라리 말줄임
        return cut[:idx + 1]
    # 종결부호 못 찾음: 마지막 공백까지만 잘라 단어/이모지 조각이 남지 않게
    sp = cut.rfind(" ")
    if sp > limit * 0.6:
        cut = cut[:sp]
    return cut.rstrip() + "…"


def _consensus(claude_plan, deepseek_plan):
    """두 AI 판단을 합의: 둘 다 동의한 매수만 실행, 손절은 한쪽이라도 원하면 실행."""
    c_dec = claude_plan.get("decisions", []) or []
    d_dec = deepseek_plan.get("decisions", []) or []

    def key(x):
        return (str(x.get("symbol", "")).upper(), str(x.get("action", "")).lower())

    c_map = {key(x): x for x in c_dec}
    d_set = {key(x) for x in d_dec}

    merged = []
    consensus_log = []
    for k, x in c_map.items():
        sym, action = k
        if action == "sell":
            # 매도(손절)는 Claude가 원하면 실행 (방어 우선)
            merged.append(x); consensus_log.append(f"{sym} 매도(Claude)")
        elif action == "buy":
            if k in d_set:
                # 둘 다 매수 동의 → 실행 (수량은 더 보수적인 쪽=작은 쪽)
                d_x = next((y for y in d_dec if key(y) == k), None)
                try:
                    cq = float(x.get("qty", 0)); dq = float(d_x.get("qty", 0)) if d_x else cq
                    x = dict(x); x["qty"] = min(cq, dq)  # 보수적으로 작은 수량
                except Exception:
                    pass
                merged.append(x); consensus_log.append(f"{sym} 매수(합의)")
            else:
                consensus_log.append(f"{sym} 매수 보류(Claude만)")
    # DeepSeek만 원한 매도(손절)도 방어 차원에서 실행
    for y in d_dec:
        sym, action = key(y)
        if action == "sell" and (sym, "sell") not in c_map:
            merged.append(y); consensus_log.append(f"{sym} 매도(DeepSeek)")

    # 앱용 market_view는 잘림 없이 두 AI의 전체 분석을 그대로 담는다(사실상 무제한,
    # 폭주 방어용 넉넉한 상한만). 알림(ntfy)은 format_view_for_push에서 각 250자로
    # 별도 요약되므로, 여기서 길어도 알림이 4096바이트를 넘지 않는다.
    view = "🤝 합의: " + (", ".join(consensus_log) if consensus_log else "거래 없음") + \
           " | Claude: " + _clip_sentence(claude_plan.get("market_view", ""), 4000) + \
           " | DeepSeek: " + _clip_sentence(deepseek_plan.get("market_view", ""), 4000)
    return {"decisions": merged, "market_view": view}


# ── 주문 검증 + 실행 ──
STOCK_MARKET_OPEN = True   # 정규장 여부 (시장가 거래)
STOCK_TRADABLE = True      # 주식 거래 가능 여부 (정규장 OR 애프터마켓)
IS_AFTER_HOURS = False     # 애프터마켓 여부 (지정가+extended_hours, 포지션 축소)
MARKET_SESSION = "regular"  # main에서 매 실행마다 갱신 (regular|pre|after|closed)


def execute(decisions, account, positions, market, regime=None):
    equity = float(account["equity"])
    cash = float(account["cash"])
    held = {p["symbol"]: float(p["qty"]) for p in positions}
    prices = {m["symbol"]: m["price"] for m in market}
    # 하이리스크 격리 예산 추적: 이번 실행에서 하이리스크로 새로 집행한 금액 누적.
    # (기보유분 중 무엇이 하이리스크였는지는 봇이 기록하지 않으므로, 보수적으로
    #  이번 런에서 새로 늘리는 하이리스크 매수의 합계만 20% 한도로 통제한다.)
    highrisk_spent = 0.0
    highrisk_budget = equity * MAX_HIGHRISK_BUDGET_PCT
    results = []
    for d in decisions[:MAX_TRADES_PER_RUN]:
        sym = str(d.get("symbol", "")).upper()
        crypto = is_crypto(sym)
        # 주식 거래 불가 세션(프리마켓·휴장)이면 선차단 (코인은 24시간 진행).
        # 애프터마켓은 STOCK_TRADABLE=True라 통과 → 아래에서 지정가+축소로 처리.
        if not crypto and not STOCK_TRADABLE:
            results.append(f"⏸ {sym} 보류 ({_SESSION_KR.get(MARKET_SESSION,'장 외')} — 정규장에 재검토)")
            continue
        try:
            qty = round(float(d.get("qty", 0)), 6 if crypto else 4)
        except (TypeError, ValueError):
            qty = 0
        action = d.get("action")
        reason = str(d.get("reason", ""))[:120]
        conviction = str(d.get("conviction", "normal")).lower()  # high | normal | low
        tier = str(d.get("tier", "core")).lower()                 # core | highrisk
        is_highrisk = (tier == "highrisk")
        if qty <= 0 or sym not in prices:
            results.append(f"⛔ {sym} 건너뜀 (잘못된 주문)")
            continue
        cost = qty * prices[sym]
        if action == "buy":
            is_lev = sym in LEVERAGE_TICKERS
            is_inv = sym in INVERSE_TICKERS
            regime_label = (regime or {}).get("label", "")
            defensive = regime_label in ("하락장/조정", "단기 약세")
            if is_highrisk:
                # ── 하이리스크 슬롯: 종목당 5% 고정, 하락장이어도 진입 허용(추세전환 판단은 AI가 함) ──
                # 격리 예산(합계 20%)은 아래에서 별도 체크.
                pos_cap = MAX_HIGHRISK_POSITION_PCT                    # 5% (레버리지 포함 동일)
            elif is_inv or defensive:
                # 코어 매수: 인버스·하락장은 5% 방어
                pos_cap = MAX_POSITION_PCT                              # 5%
            elif is_lev:
                pos_cap = MAX_POSITION_LEV if conviction == "high" else MAX_POSITION_PCT  # 8% or 5%
            elif conviction == "high":
                pos_cap = MAX_POSITION_HIGH                             # 12%
            elif conviction == "low":
                pos_cap = 0.03                                          # 3%
            else:
                pos_cap = MAX_POSITION_PCT                              # 5%

            # 애프터마켓 매수는 유동성·변동성 리스크로 한도를 절반으로 축소
            if not crypto and IS_AFTER_HOURS:
                pos_cap *= 0.5

            # 이미 보유 중이면, 합산이 한도를 넘지 않게 (분할 매수 누적 방지)
            held_val = held.get(sym, 0) * prices[sym]
            max_total = equity * pos_cap
            room = max(0, max_total - held_val)
            if cost > room:
                qty = round(room / prices[sym], 6 if crypto else 4)
                cost = qty * prices[sym]

            # 하이리스크 격리 예산: 합계가 20%를 넘지 않도록 이번 매수액을 깎거나 보류
            if is_highrisk:
                hr_room = max(0, highrisk_budget - highrisk_spent)
                if cost > hr_room:
                    qty = round(hr_room / prices[sym], 6 if crypto else 4)
                    cost = qty * prices[sym]
                if qty <= 0:
                    results.append(f"⛔ {sym} 하이리스크 보류 (격리예산 {int(MAX_HIGHRISK_BUDGET_PCT*100)}% 소진)")
                    continue

            if qty <= 0 or cost > cash - equity * MIN_CASH_BUFFER_PCT:
                results.append(f"⛔ {sym} 매수 보류 (현금/한도)")
                continue
            if is_highrisk:
                highrisk_spent += cost  # 격리 예산 집행 누적
            side = "buy"
        elif action == "sell":
            if held.get(sym, 0) < qty:
                qty = round(held.get(sym, 0), 6 if crypto else 4)
            if qty <= 0:
                results.append(f"⛔ {sym} 매도 보류 (보유 없음)")
                continue
            side = "sell"
        else:
            continue
        try:
            if crypto and side == "buy":
                # 코인 매수는 금액(notional) 기준 — BTC 고가로 인한 수량/최소단위 문제 회피
                notional = round(qty * prices[sym], 2)
                if notional < 10:      # 알파카 코인 최소 주문 $10
                    notional = 10.0
                order = {"symbol": to_alpaca_symbol(sym), "notional": str(notional),
                         "side": side, "type": "market", "time_in_force": "gtc"}
                cost = notional
            elif crypto:
                # 코인 매도는 보유 수량 기준
                order = {"symbol": to_alpaca_symbol(sym), "qty": str(qty),
                         "side": side, "type": "market", "time_in_force": "gtc"}
            elif IS_AFTER_HOURS:
                # 애프터마켓: 시장가 불가 → 지정가 + extended_hours 필수.
                # 체결 가능성을 위해 매수는 현재가 +0.3%, 매도는 -0.3%로 살짝 양보.
                px = prices[sym]
                limit_px = round(px * (1.003 if side == "buy" else 0.997), 2)
                order = {"symbol": to_alpaca_symbol(sym), "qty": str(qty),
                         "side": side, "type": "limit", "limit_price": str(limit_px),
                         "time_in_force": "day", "extended_hours": True}
            else:
                order = {"symbol": to_alpaca_symbol(sym), "qty": str(qty),
                         "side": side, "type": "market", "time_in_force": "day"}
            alpaca("/v2/orders", method="POST", body=order)
            mark = "🔴 매수" if side == "buy" else "🔵 매도"
            hr_tag = " ⚡하이리스크" if (side == "buy" and is_highrisk) else ""
            ah_tag = " 🌙애프터" if (not crypto and IS_AFTER_HOURS) else ""
            results.append(f"{mark}{hr_tag}{ah_tag} {sym} {qty}{'개' if crypto else '주'} (~${cost:,.0f}) — {reason}")
            cash = cash - cost if side == "buy" else cash + cost
        except Exception as e:
            results.append(f"⛔ {sym} 주문 실패: {e}")
    return results


def format_view_for_push(mv):
    """market_view("🤝 합의: ... | Claude: ... | DeepSeek: ...")를
    ntfy 알림용으로 Claude·DeepSeek 구별되게 줄바꿈 분리.
    DeepSeek 오류로 구분자가 없으면 Claude 단독으로 표기."""
    if not mv:
        return ""
    consensus, claude, deep = "", "", ""
    ci = mv.find(" | Claude:")
    di = mv.find(" | DeepSeek:")
    if ci != -1:
        consensus = mv[:ci].strip()
        if di != -1 and di > ci:
            claude = mv[ci + len(" | Claude:"):di].strip()
            deep = mv[di + len(" | DeepSeek:"):].strip()
        else:
            claude = mv[ci + len(" | Claude:"):].strip()
    else:
        claude = mv.strip()
    consensus = consensus.replace("🤝 합의:", "").strip()

    # 알림(ntfy)은 ntfy 서버 4096바이트 제한이 있어, 각 AI 멘트를 250자로 요약한다.
    # 앱용 market_view는 1000자 풀버전 그대로 유지되고, 여기서 자르는 건 알림 표시용뿐.
    claude = _clip_sentence(claude, 250)
    deep = _clip_sentence(deep, 250)

    lines = []
    if consensus and consensus != "거래 없음":
        lines.append(f"🤝 합의: {consensus}")
    elif consensus == "거래 없음":
        lines.append("🤝 두 AI 합의: 관망 (거래 없음)")
    if claude:
        lines.append(f"🧠 Claude: {claude}")
    if deep:
        lines.append(f"🌊 DeepSeek: {deep}")
    elif claude:
        lines.append("ℹ️ 이번엔 Claude 단독 판단 (DeepSeek 응답 없음)")
    return "\n\n".join(lines)


def _fit_ntfy(message, max_bytes=3800):
    """ntfy 서버는 본문이 4096바이트를 넘으면 알림을 첨부파일로 돌려 '짤린 것처럼'
    보인다. 한도 안에 들도록 UTF-8 바이트 기준으로 안전하게 자른다(한글 글자 중간
    분할 방지). 잘릴 경우 전체는 앱에서 보라는 안내를 덧붙인다."""
    data = message.encode("utf-8")
    if len(data) <= max_bytes:
        return message
    note = "\n\n…(전체 내용은 사도될까 앱에서 확인)"
    budget = max_bytes - len(note.encode("utf-8"))
    cut = data[:budget].decode("utf-8", "ignore")  # 깨진 끝바이트는 버림
    # 줄 경계에서 끊어 자연스럽게
    nl = cut.rfind("\n\n")
    if nl > budget * 0.5:
        cut = cut[:nl]
    return cut.rstrip() + note


def send_push(title, message, urgent):
    if not NTFY_TOPIC:
        log("NTFY_TOPIC 없음 — 알림 생략")
        return
    message = _fit_ntfy(message)
    payload = json.dumps({"topic": NTFY_TOPIC, "title": title, "message": message,
                          "priority": 4 if urgent else 3,
                          "tags": ["robot"]}).encode("utf-8")
    req = urllib.request.Request("https://ntfy.sh/", data=payload,
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=20)


def _prev_last_trade_time():
    try:
        with open("bot_status.json", encoding="utf-8") as f:
            return json.load(f).get("last_trade_time")
    except Exception:
        return None


def _parse_positions(positions_raw):
    """알파카 raw 포지션을 앱·프롬프트용 형식으로 변환."""
    out = []
    for p in positions_raw:
        try:
            out.append({
                "symbol": normalize_position_symbol(p.get("symbol", "")),
                "qty": float(p.get("qty", 0)),
                "avg_cost": float(p.get("avg_entry_price", 0)),
                "now": float(p.get("current_price", 0) or p.get("avg_entry_price", 0)),
                "pnl_pct": round(float(p.get("unrealized_plpc", 0)) * 100, 1),
            })
        except Exception as e:
            log(f"⚠️ 포지션 파싱 오류 ({p.get('symbol','?')}): {e}")
    return out


def fetch_positions():
    """알파카 포지션을 조회해 파싱까지. 실패 시 빈 리스트."""
    try:
        return _parse_positions(alpaca("/v2/positions"))
    except Exception as e:
        log(f"⚠️ 포지션 조회 실패: {e}")
        return []


def main():
    # ── 0단계: 시크릿 존재 확인 ──
    missing = [n for n, v in [("ALPACA_API_KEY", ALPACA_KEY),
                              ("ALPACA_SECRET_KEY", ALPACA_SECRET),
                              ("ANTHROPIC_API_KEY", ANTHROPIC_KEY)] if not v]
    if missing:
        log(f"❌ [0단계] GitHub Secret 누락: {', '.join(missing)}")
        log("   Settings → Secrets and variables → Actions 에서 이름을 정확히 확인하세요.")
        return 1
    log(f"✅ [0단계] 시크릿 3개 확인 (ALPACA_API_KEY 앞 4자: {ALPACA_KEY[:4]}..., "
          f"ANTHROPIC 앞 7자: {ANTHROPIC_KEY[:7]}...)")

    # ── 1단계: Alpaca 모의계좌 연결 테스트 ──
    try:
        account = alpaca("/v2/account")
        log(f"✅ [1단계] Alpaca 연결 성공 — 총자산 ${account['equity']}, 현금 ${account['cash']}")
    except urllib.error.HTTPError as e:
        log(f"❌ [1단계] Alpaca 인증 실패 (HTTP {e.code})")
        if e.code in (401, 403):
            log("   → 키가 틀렸거나, 모의계좌(Paper)가 아닌 키일 가능성이 커요.")
            log("   → 알파카 대시보드 왼쪽 위가 'Paper Trading'인 상태에서 키를 재생성하고,")
            log("     GitHub Secret 값을 새로 붙여넣어 주세요 (앞뒤 공백·따옴표 없이).")
        return 1
    except Exception as e:
        log(f"❌ [1단계] Alpaca 연결 오류: {e}")
        return 1

    # ── 2단계: Anthropic(Claude) 연결 테스트 ──
    try:
        ping = http_json(
            "https://api.anthropic.com/v1/messages", method="POST",
            headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            body={"model": "claude-sonnet-4-6", "max_tokens": 16,
                  "messages": [{"role": "user", "content": "ping"}]})
        log("✅ [2단계] Claude API 연결 성공")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        log(f"❌ [2단계] Claude API 실패 (HTTP {e.code}) {detail}")
        if e.code == 401:
            log("   → API 키가 잘못됐어요. console.anthropic.com 에서 키를 재확인하세요.")
        elif e.code == 400 and "credit" in detail.lower():
            log("   → 크레딧 부족이에요. Billing에서 충전 상태를 확인하세요.")
        return 1
    except Exception as e:
        log(f"❌ [2단계] Claude API 오류: {e}")
        return 1

    # ── 3단계: 관심종목 시세 ──
    with open("watchlist.txt", encoding="utf-8") as f:
        watch = [t.strip().upper() for t in f
                 if t.strip() and not t.strip().startswith("#")]

    try:
        positions_raw = alpaca("/v2/positions")
    except Exception as e:
        log(f"⚠️ 포지션 조회 실패: {e}")
        positions_raw = []
    log(f"📊 알파카 포지션 조회 결과: {len(positions_raw)}개")
    global STOCK_MARKET_OPEN, STOCK_TRADABLE, IS_AFTER_HOURS, MARKET_SESSION
    MARKET_SESSION = get_market_session()
    STOCK_MARKET_OPEN = (MARKET_SESSION == "regular")
    IS_AFTER_HOURS = (MARKET_SESSION == "after")
    # 주식 거래 가능 = 정규장 또는 애프터마켓 (프리마켓·휴장은 코인만)
    STOCK_TRADABLE = MARKET_SESSION in ("regular", "after")
    _sess_note = ("" if STOCK_MARKET_OPEN else
                  (" — 애프터마켓: 지정가·축소 포지션으로 거래" if IS_AFTER_HOURS
                   else " — 주식 관망, 코인만 거래"))
    log(f"🕐 미국 주식장: {_SESSION_KR.get(MARKET_SESSION, MARKET_SESSION)}{_sess_note}")

    # 미체결 주문이 쌓여 있으면 구매력이 잠기므로, 일정 수 이상이면 이번 실행은 신규 주문 보류
    try:
        open_orders = alpaca("/v2/orders?status=open&limit=100")
        n_open = len(open_orders)
    except Exception:
        n_open = 0
    log(f"📋 현재 미체결 대기 주문: {n_open}건")
    if n_open >= 10:
        msg = (f"⚠️ 미체결 주문이 {n_open}건 쌓여 있어요. 구매력이 잠겨 신규 주문을 보류합니다.\n"
               "reset-account 워크플로로 주문을 정리한 뒤 다시 실행하세요.")
        log(msg)
        send_push("🤝 컨센서스 봇 — 주문 보류", msg, True)
        return 0
    positions = _parse_positions(positions_raw)

    # 미국장 마감(프리·휴장) 시에는 코인만 수집 (주식 거래 보류되므로 헛수집·크레딧 절약)
    if not STOCK_TRADABLE:
        targets = [s for s in watch if s in CRYPTO]
        log(f"💤 미국장 마감 — 코인만 점검 ({len(targets)}종목)")
    elif IS_AFTER_HOURS:
        # 애프터마켓: 실적 대응이 핵심이므로 보유 종목 + 코어만 집중 수집
        # (애프터마켓은 유동성 얇아 전체 스캔은 비효율, 보유분 방어·코어 급변 감지에 집중)
        CORE = {"AAPL","MSFT","NVDA","GOOGL","AMZN","META","TSLA",
                "QQQ","SPY","SMH","SOXX","IGV","XLK","GLD","TLT",
                "TQQQ","SOXL","SQQQ","SOXS","ETH-USD"}
        held_syms = {p["symbol"] for p in positions}
        targets = [s for s in watch if s in CORE or s in held_syms]
        log(f"🌙 애프터마켓 — 보유·코어 집중 점검 ({len(targets)}종목)")
    else:
        # 종목 풀이 크면(>70) 전부 매번 받지 않고 로테이션 스캔 (속도·데이터소스 보호)
        # 항상 받는 것: 보유 종목 + 코어(M7·주요 ETF·헤지·코인)
        CORE = {"AAPL","MSFT","NVDA","GOOGL","AMZN","META","TSLA",
                "QQQ","SPY","SMH","SOXX","IGV","XLK","GLD","TLT",
                "TQQQ","SOXL","SQQQ","SOXS","ETH-USD"}
        held_syms = {p["symbol"] for p in positions}
        always = [s for s in watch if s in CORE or s in held_syms]
        rest = [s for s in watch if s not in CORE and s not in held_syms and s not in CRYPTO]

        if len(watch) > 70 and rest:
            # 날짜+시간 기반으로 매 실행마다 다른 묶음을 스캔 (며칠이면 전체 1회전)
            import datetime as _dt
            _kst = _dt.timezone(_dt.timedelta(hours=9))
            _now = _dt.datetime.now(_kst)
            BATCH = 40  # 매 실행 확장풀 스캔 개수
            slot = (_now.hour + _now.day) % max(1, (len(rest) + BATCH - 1) // BATCH)
            start = slot * BATCH
            rotation = rest[start:start + BATCH]
            targets = always + rotation
            log(f"🔄 로테이션 스캔: 코어·보유 {len(always)}개 + 확장풀 {len(rotation)}개 (전체 {len(watch)}개 중)")
        else:
            targets = watch

    market = []
    for sym in targets:
        try:
            market.append(summarize(sym, fetch_daily(sym)))
        except Exception as e:
            log(f"⚠️ {sym} 시세 실패: {e}")
        time.sleep(0.4)  # 종목이 많아 데이터 소스 차단 방지용 간격
    log(f"✅ [3단계] 시세 확보 {len(market)}/{len(targets)} 종목")

    # 애프터마켓이면 최신 체결가로 현재가를 갱신 (실적 급변 반영). 일봉 종가 대비 변화도 계산.
    if IS_AFTER_HOURS and market:
        latest = fetch_latest_prices([m["symbol"] for m in market])
        for m in market:
            lp = latest.get(m["symbol"])
            if lp and m.get("price"):
                prev_close = m["price"]
                m["regular_close"] = prev_close          # 정규장 종가 보존
                m["price"] = round(lp, 2)                 # 현재가를 애프터마켓 체결가로
                m["afterhours_chg_pct"] = round((lp / prev_close - 1) * 100, 1)  # 장 외 변동
        moved = [m for m in market if abs(m.get("afterhours_chg_pct", 0)) >= 3]
        if moved:
            log("🌙 애프터마켓 급변(±3%↑): " +
                ", ".join(f"{m['symbol']} {m['afterhours_chg_pct']:+.1f}%" for m in moved))

    if not market:
        send_push("🤝 컨센서스 봇 — 실행 실패", "시세를 하나도 못 가져왔어요.", True)
        return 1

    # 밸류에이션(PER·PBR) 수집해 각 종목에 merge (저평가 판정용, 실패 허용).
    # 야후 인증이 실패해도 off_52w_high_pct는 일봉으로 계산돼 있어 항상 제공됨.
    try:
        vals = fetch_valuations([m["symbol"] for m in market])
        for m in market:
            v = vals.get(m["symbol"]) or {}
            val = {k: x for k, x in v.items() if x is not None}
            # 야후가 52주 낙폭을 안 줬으면 일봉 계산값으로 채움
            if "off_52w_high_pct" not in val and m.get("off_52w_high_pct") is not None:
                val["off_52w_high_pct"] = m["off_52w_high_pct"]
            if val:
                m["valuation"] = val
    except Exception as e:
        log(f"⚠️ 밸류에이션 merge 실패: {e}")

    # 시장 전체 국면 판단 (하락장 대응용)
    regime = assess_regime()
    log(f"✅ [3.5단계] 시장 국면: {regime.get('label')} — {regime.get('detail','')}")

    # 섹터 흐름(로테이션) 분석 — 돈이 어디로 도는지
    try:
        sectors = assess_sectors()
        regime["sectors"] = sectors
        log(f"✅ [3.6단계] 섹터 흐름: {sectors.get('summary','')}")
    except Exception as e:
        log(f"⚠️ [3.6단계] 섹터 분석 실패: {e}")
        regime["sectors"] = None

    try:
        plan = ask_claude(account, positions, market, regime, MARKET_SESSION)
        log("✅ [4단계] Claude 판단 수신")
    except Exception as e:
        log(f"❌ [4단계] Claude 판단 실패: {e}")
        send_push("🤝 컨센서스 봇 — 판단 실패", f"AI API 오류: {e}", True)
        return 1

    decisions = plan.get("decisions", [])
    view = plan.get("market_view", "")
    results = execute(decisions, account, positions, market, regime) if decisions else []

    # 체결(⛔ 제외)이 있었으면 포지션을 재조회해 '매도 전 스냅샷'이 아닌 최신 상태를 반영.
    # (예전엔 execute 전에 조회한 positions를 그대로 저장해, 방금 판 종목이 포트폴리오에
    #  남아 다음 실행에야 빠지는 '늦은 업데이트' 버그가 있었음.)
    executed = [r for r in results if not r.startswith("⛔")]
    if executed:
        time.sleep(3)  # 시장가 체결이 알파카에 반영될 시간을 잠깐 줌
        refreshed = fetch_positions()
        if refreshed or not positions:
            positions = refreshed
        # 계좌(현금·총자산)도 체결 반영해 다시 조회
        try:
            account = alpaca("/v2/account")
        except Exception as e:
            log(f"⚠️ 계좌 재조회 실패(기존 값 유지): {e}")
        log(f"🔄 체결 반영 후 포지션 재조회: {len(positions)}개")

    pos_line = ", ".join(f"{p['symbol']} {p['pnl_pct']:+.1f}%" for p in positions) or "없음"
    body_parts = [f"💼 총자산 ${float(account['equity']):,.0f} · 보유: {pos_line}"]
    if view:
        body_parts.append(format_view_for_push(view))
    body_parts.append("\n".join(results) if results else "오늘은 거래 없음 (관망)")
    body_parts.append("※ 모의계좌 자동매매 · 참고용")
    body = "\n\n".join(body_parts)

    kst = datetime.timezone(datetime.timedelta(hours=9))
    now_str = datetime.datetime.now(kst).strftime("%Y-%m-%d %H:%M")

    if executed:
        title = f"🚨 컨센서스 봇 거래 발생! {len(executed)}건 체결"
    else:
        title = "🤝 컨센서스 봇: 이번엔 관망 (거래 없음)"
    log(title)
    log(body)
    send_push(title, body, bool(executed))

    # ── 사도될까 앱 연동용 상태 파일 저장 ──
    # 앱 차트 오버레이용: 보유 종목 + 지수(SPY/QQQ)의 지지/저항·피보·TACO ZONE
    mkt_by_sym = {m["symbol"]: m for m in market}
    levels_syms = list({p["symbol"] for p in positions} | {"SPY", "QQQ"})
    chart_levels = {}
    for sym in levels_syms:
        m = mkt_by_sym.get(sym)
        if not m:
            continue
        chart_levels[sym] = {
            "price": m.get("price"),
            "support": m.get("support", []),
            "resistance": m.get("resistance", []),
            "fib": m.get("fib"),
            "taco_zone": m.get("taco_zone"),
            "in_taco_zone": m.get("in_taco_zone", False),
        }

    status = {
        "updated": now_str,
        "equity": float(account["equity"]),
        "cash": float(account["cash"]),
        "base": 100000.0,
        "positions": positions,
        "trades": results,
        "market_view": view,
        "chart_levels": chart_levels,
        "last_trade_time": now_str if executed else (
            _prev_last_trade_time()),
        "last_trade_count": len(executed),
    }
    with open("bot_status.json", "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=1)
    log("✅ [5단계] bot_status.json 저장 완료")

    # ── 판단 일지 누적 (행동 분석용) ──
    # 거래가 있었거나, market_view에 의미 있는 판단이 담겼을 때만 기록 (관망 반복은 생략해 용량 절약)
    try:
        if executed or (view and len(view) > 10):
            journal = []
            try:
                with open("trade_journal.json", encoding="utf-8") as f:
                    journal = json.load(f)
            except Exception:
                journal = []
            # 보유 종목 요약 (평단 대비 손익)
            pos_summary = [
                {"sym": p["symbol"], "qty": p["qty"], "pnl_pct": p.get("pnl_pct", 0)}
                for p in positions
            ]
            entry = {
                "time": now_str,
                "regime": (regime or {}).get("label", "?"),
                "sectors": (regime or {}).get("sectors", {}).get("summary", "") if (regime or {}).get("sectors") else "",
                "vix": (regime or {}).get("vix"),
                "equity": round(float(account["equity"]), 2),
                "cash_pct": round(float(account["cash"]) / float(account["equity"]) * 100, 1) if float(account["equity"]) else 0,
                "trades": [
                    {"sym": d.get("symbol"), "action": d.get("action"),
                     "qty": d.get("qty"), "conviction": d.get("conviction", "normal"),
                     "tier": d.get("tier", "core"),
                     "reason": str(d.get("reason", ""))[:100]}
                    for d in decisions
                ] if decisions else [],
                "executed": executed,
                "positions": pos_summary,
                "market_view": view,
            }
            journal.append(entry)
            # 최근 200개만 유지 (용량 관리)
            journal = journal[-200:]
            with open("trade_journal.json", "w", encoding="utf-8") as f:
                json.dump(journal, f, ensure_ascii=False, indent=1)
            log(f"📓 판단 일지 기록 (#{len(journal)})")
    except Exception as e:
        log(f"⚠️ 판단 일지 기록 실패: {e}")

    # ── 자산 히스토리 누적 (성과 추적용) ──
    try:
        try:
            with open("equity_history.json", encoding="utf-8") as f:
                hist = json.load(f)
        except Exception:
            hist = []
        today = datetime.datetime.now(kst).strftime("%Y-%m-%d")
        eq = round(float(account["equity"]), 2)
        # 같은 날짜는 최신값으로 갱신 (하루 한 점)
        if hist and hist[-1].get("date") == today:
            hist[-1] = {"date": today, "equity": eq}
        else:
            hist.append({"date": today, "equity": eq})
        # 최근 180일만 유지
        hist = hist[-180:]
        with open("equity_history.json", "w", encoding="utf-8") as f:
            json.dump(hist, f, ensure_ascii=False, indent=1)
        log(f"✅ 자산 히스토리 기록: {today} ${eq:,.0f} (총 {len(hist)}일)")
    except Exception as e:
        log(f"자산 히스토리 기록 실패: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
