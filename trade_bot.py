# -*- coding: utf-8 -*-
"""Claude AI 모의투자 봇
   매 실행마다: 시세/지표 수집 → Claude에게 판단 요청 → Alpaca 모의계좌에 주문 → 디스코드 알림
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
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "").strip()  # 디스코드 알림

ALPACA_BASE = "https://paper-api.alpaca.markets"  # 모의계좌 전용 (변경 금지)
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}

# ── 안전장치 ──
MAX_TRADES_PER_RUN = 5          # 한 번 실행에 최대 주문 수

# ── 서킷브레이커 (계좌 전체 방어) ──
# 종목별 손절과 별개로, 계좌 전체가 무너질 때 신규 매수를 멈춰 '하락장에 덜 잃는다'.
CIRCUIT_DRAWDOWN_PCT = -15.0    # 자산이 직전 고점 대비 이만큼↓ 빠지면 신규 매수 전면 중단
CIRCUIT_RESUME_PCT = -10.0      # 고점 대비 이 수준까지 회복하면 매수 재개(반복 진입/이탈 방지 히스테리시스)
MAX_POSITION_PCT = 0.05         # 한 종목 신규 매수 한도: 총자산의 5% (기본)
MAX_POSITION_HIGH = 0.12        # 강한 확신 시 최대 한도: 12% (일반주, 하락장 제외)
MAX_POSITION_LEV = 0.08         # 레버리지: 확신 시 최대 8% (3배 변동이라 일반주보다 낮게)
LEVERAGE_TICKERS = {"TQQQ", "SOXL", "UPRO", "QLD", "TNA", "FNGU", "TECL", "LABU", "NVDL", "TSLL"}
INVERSE_TICKERS = {"SQQQ", "SOXS", "SH", "SDS"}
MIN_CASH_BUFFER_PCT = 0.10      # 현금 10%는 항상 남김

# ── 자동 손절(로스컷) ──
# AI 판단에 의존하지 않고 코드가 강제로 자른다. AI가 깜빡하거나 판단이 흔들려도
# 손실이 이 선을 넘으면 무조건 매도 신호를 건다(텐버거 장기보유 종목은 예외).
STOPLOSS_PCT = -7.0             # 일반 종목 자동 손절선
STOPLOSS_LEV_PCT = -5.0        # 레버리지 종목은 변동이 커서 더 빠르게

# ── 텐버거(러너) 트레일링 스탑 ──
# 수익이 크게 난 종목은 끝까지 태우되(let winners run), 고점 대비 일정폭 꺾이면 수익을 보전한다.
RUNNER_MIN_PEAK_PCT = 15.0       # 트레일링 보호 대상이 되는 최소 고점 수익률(+15%↑ 찍은 종목만)
RUNNER_TRAIL_DROP_PCT = 20.0     # 고점 대비 이만큼(%p) 빠지면 트레일링 스탑 발동(자동 매도 신호)
RUNNER_BIG_PEAK_PCT = 50.0       # 대박 구간(+50%↑): 변동성이 크므로 트레일링 폭을 더 넓게
RUNNER_BIG_TRAIL_DROP_PCT = 30.0 # 대박 구간은 고점 대비 -30%p로 더 여유있게(텐버거 일찍 안 끊기게)

# ── 텐버거(10배주) 후보 발굴 ──
# 봇은 '후보 추천'만, 최종 편입은 사람이 tenbagger.txt에 추가. 거기 적힌 종목은
# 장기보유로 취급해 단기 손절·트레일링을 면제(엉덩이 싸움 존중).
TENBAGGER_FILE = "tenbagger.txt"          # 사람이 확정한 장기보유 텐버거 종목(한 줄 1티커)
TENBAGGER_AUTO_SCORE = 10                  # 이 점수 이상이 자동 편입 '결승 진출' 자격(만점 12)
TENBAGGER_MAX_HOLDINGS = 0                 # 텐버거 종목 수 상한(0=무제한, 꾸준히 적립)
TENBAGGER_CAND_FILE = "tenbagger_candidates.json"  # 봇이 추천한 후보(앱·알림용)
TENBAGGER_SMALLCAP_MAX = 20_000_000_000   # 소형~미드캡 상한: 시총 200억 달러 미만 가산
TENBAGGER_DEEP_DIP_PCT = -25.0            # 52주 고점 대비 이만큼↓ 빠진 '소외 우량주' 가산
# 대형주·ETF는 텐버거(가벼운 몸집) 대상이 아니므로 후보에서 제외
TENBAGGER_EXCLUDE = {
    "SPY", "QQQ", "DIA", "IWM", "VOO", "VTI", "SOXX", "SMH", "XLK", "XLF", "XLE",
    "GLD", "TLT", "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA",
    "TQQQ", "SOXL", "UPRO", "QLD", "SQQQ", "SOXS",
}

# ── 하이리스크 슬롯 (저평가·과매도 역추세 전용 격리 예산) ──
# 일반(core) 매수와 별도로 운용. 한 종목 물려도 전체가 휘청이지 않게 칸막이.
MAX_HIGHRISK_BUDGET_PCT = 0.20   # 하이리스크 포지션 '합계' 상한: 총자산의 20%
MAX_HIGHRISK_POSITION_PCT = 0.05 # 하이리스크 한 종목 상한: 총자산의 5% (레버리지 포함 동일)


def _urlopen_text(url, method="GET", headers=None, body=None, timeout=30):
    """urllib 요청 공통 코어. 응답 본문을 문자열로 반환.
    http_json/_get이 공유해 중복 제거. timeout·헤더는 호출부가 지정."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={**UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def http_json(url, method="GET", headers=None, body=None):
    return json.loads(_urlopen_text(url, method=method, headers=headers,
                                    body=body, timeout=60))


# ── 시세 (1순위: Alpaca 자체 데이터 → 2순위: Stooq → 3순위: Yahoo) ──
def _get(url, headers=None):
    return _urlopen_text(url, headers=headers, timeout=30)


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
                    "market_cap": q.get("marketCap"),
                    # 일간 등락률(히트맵용, 야후 quote 제공)
                    "chg_pct": (round(q.get("regularMarketChangePercent"), 2)
                                if isinstance(q.get("regularMarketChangePercent"), (int, float)) else None),
                    "price": q.get("regularMarketPrice"),
                    # 가치투자 지표(야후 quote에 있을 때만, 누락 잦음)
                    "roe": (round(q.get("returnOnEquity") * 100, 1)
                            if isinstance(q.get("returnOnEquity"), (int, float)) else None),
                    "psr": q.get("priceToSalesTrailing12Months"),
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


def fetch_market_news(symbols=None, limit=30):
    """알파카(Benzinga) 뉴스 API로 시장 주요 뉴스를 수집. 썸네일·출처·시각 포함.
    symbols 주면 해당 종목 뉴스, 없으면 전체 시장 뉴스. 앱 표시용(news.json).
    실패해도 빈 리스트 반환(봇 거래엔 영향 없음)."""
    try:
        url = (f"https://data.alpaca.markets/v1beta1/news?limit={limit}&sort=desc"
               "&include_content=false&exclude_contentless=true")
        if symbols:
            url += "&symbols=" + urllib.parse.quote(",".join(symbols))
        j = json.loads(_get(url, headers={"APCA-API-KEY-ID": ALPACA_KEY,
                                          "APCA-API-SECRET-KEY": ALPACA_SECRET}))
        out = []
        for n in j.get("news", []):
            # 썸네일: images 배열에서 small/thumb 우선
            thumb = ""
            for img in (n.get("images") or []):
                if img.get("url"):
                    thumb = img["url"]
                    if img.get("size") in ("thumb", "small"):
                        break
            out.append({
                "headline": n.get("headline", ""),
                "summary": (n.get("summary", "") or "")[:200],
                "source": n.get("source", "") or n.get("author", ""),
                "url": n.get("url", ""),
                "created_at": n.get("created_at", ""),
                "symbols": n.get("symbols", [])[:5],
                "image": thumb,
            })
        log(f"✅ 시장 뉴스 수집: {len(out)}건")
        return out
    except Exception as e:
        log(f"⚠️ 시장 뉴스 수집 실패: {e}")
        return []


def fetch_snapshots(symbols):
    """알파카 snapshot으로 최신가 + 당일 저가/고가를 batch 수집.
    '장중 저점 대비 반등률'을 계산해, SOXL 같은 변동성 큰 종목의 급락 후 반등을
    봇이 포착할 수 있게 한다. 반환: {SYM: {price, day_low, day_high, rebound_from_low_pct, off_day_high_pct}}
    실패해도 {} 반환."""
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
            data = j.get("snapshots", j) if isinstance(j, dict) else {}
            for sym, snap in data.items():
                if not isinstance(snap, dict):
                    continue
                lt = snap.get("latestTrade") or {}
                db = snap.get("dailyBar") or {}
                price = lt.get("p") or db.get("c")
                if not price:
                    continue
                price = float(price)
                low = float(db.get("l") or 0) or None     # 당일 저가
                high = float(db.get("h") or 0) or None     # 당일 고가
                rec = {"price": price, "day_low": low, "day_high": high}
                if low and low > 0:
                    rec["rebound_from_low_pct"] = round((price / low - 1) * 100, 1)
                if high and high > 0:
                    rec["off_day_high_pct"] = round((price / high - 1) * 100, 1)
                out[str(sym).upper()] = rec
        except Exception as e:
            log(f"⚠️ snapshot 수집 실패(chunk {i}): {e}")
        time.sleep(0.3)
    if out:
        log(f"✅ snapshot 수집: {len(out)}종목 (최신가·장중 저점)")
    return out


def fetch_latest_prices(symbols):
    """fetch_snapshots에서 가격만 추려 {SYM: price}로 반환 (기존 호환)."""
    snaps = fetch_snapshots(symbols)
    return {s: v["price"] for s, v in snaps.items() if v.get("price")}



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


def ichimoku(closes):
    """일목균형표(이치모쿠) — 종가 기반 근사 계산.
    (정밀하게는 고가·저가가 필요하나, 데이터 소스에 종가만 안정적으로 들어와
     기간 내 종가 최고/최저로 근사한다. 구름 위/안/아래 판정엔 충분.)
    반환: {tenkan, kijun, cloud_top, cloud_bottom, pos, dist_to_cloud_pct} 또는 None
      pos: 'above'(구름 위·강세) | 'in'(구름 안·중립) | 'below'(구름 아래·약세)
      dist_to_cloud_pct: 현재가가 구름에서 떨어진 거리%(구름 위면 구름상단까지, 안이면 0)
    """
    if len(closes) < 52:
        return None

    def mid(period, end):
        seg = closes[max(0, end - period + 1):end + 1]
        return (max(seg) + min(seg)) / 2 if seg else None

    n = len(closes)
    cur = closes[-1]
    tenkan = mid(9, n - 1)           # 전환선(9)
    kijun = mid(26, n - 1)           # 기준선(26)
    # 선행스팬은 26일 앞으로 그리므로, '현재 위치의 구름'은 26일 전 시점 기준값이다.
    past = n - 1 - 26
    if past < 51:
        return None
    span_a = (mid(9, past) + mid(26, past)) / 2   # 선행스팬1(26일 전 산출 → 현재에 투영)
    span_b = mid(52, past)                          # 선행스팬2
    if span_a is None or span_b is None:
        return None
    cloud_top = max(span_a, span_b)
    cloud_bottom = min(span_a, span_b)

    if cur > cloud_top:
        pos = "above"
        dist = (cur / cloud_top - 1) * 100        # 구름 상단까지 내려올 여지(양수=위에 떠있음)
    elif cur < cloud_bottom:
        pos = "below"
        dist = (cur / cloud_bottom - 1) * 100     # 음수
    else:
        pos = "in"
        dist = 0.0

    return {
        "tenkan": round(tenkan, 2),
        "kijun": round(kijun, 2),
        "cloud_top": round(cloud_top, 2),
        "cloud_bottom": round(cloud_bottom, 2),
        "pos": pos,
        "dist_to_cloud_pct": round(dist, 1),
    }


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


def adx_from_closes(closes, period=14):
    """ADX(추세 강도) 근사 계산 — 종가만으로.
    원래 ADX는 고가·저가(True Range)가 필요하지만, 데이터 소스가 종가만 안정적으로
    주므로 일목균형표처럼 종가 변화로 근사한다. 추세의 '방향'보다 '강도'를 보는 용도.
    값이 클수록(보통 25~30↑) 추세가 강함. 정밀도는 OHLC 버전보다 낮음(근사임).
    반환: ADX 값(0~100) 또는 None."""
    if len(closes) < period * 2:
        return None
    # +DM/-DM을 종가 변화로 근사 (고저 대신 종가 간 차이)
    plus_dm, minus_dm, tr = [], [], []
    for k in range(1, len(closes)):
        ch = closes[k] - closes[k - 1]
        plus_dm.append(max(ch, 0))      # 상승분
        minus_dm.append(max(-ch, 0))    # 하락분
        tr.append(abs(ch) or 1e-9)      # 변동폭 근사(0 방지)

    def _smooth(arr):
        # Wilder 평활
        s = sum(arr[:period])
        out = [s]
        for x in arr[period:]:
            s = s - (s / period) + x
            out.append(s)
        return out

    if len(tr) < period:
        return None
    str_ = _smooth(tr)
    sp = _smooth(plus_dm)
    sm = _smooth(minus_dm)
    dx = []
    for i in range(len(str_)):
        if str_[i] == 0:
            continue
        pdi = 100 * sp[i] / str_[i]
        mdi = 100 * sm[i] / str_[i]
        denom = pdi + mdi
        if denom == 0:
            continue
        dx.append(100 * abs(pdi - mdi) / denom)
    if len(dx) < period:
        return round(sum(dx) / len(dx), 1) if dx else None
    # ADX = DX의 평활 평균
    adx = sum(dx[:period]) / period
    for x in dx[period:]:
        adx = (adx * (period - 1) + x) / period
    return round(adx, 1)


def summarize(sym, series):
    # 입력 방어: series가 비었거나 close가 없는 행이 섞여도 죽지 않게 정제
    series = [r for r in (series or []) if isinstance(r, dict) and r.get("close") is not None]
    if len(series) < 2:
        return {"symbol": sym, "price": None, "error": "데이터 부족"}
    closes = []
    vols = []
    for r in series:
        try:
            closes.append(float(r["close"]))
            vols.append(float(r.get("volume") or 0))
        except (TypeError, ValueError):
            continue
    if len(closes) < 2:
        return {"symbol": sym, "price": None, "error": "유효 종가 부족"}
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
    chg1 = (closes[-1] / closes[-2] - 1) * 100 if len(closes) >= 2 else 0
    # ── 홀리 그레일(린다 라쉬케) 신호 — ADX 강추세 + 20일선 눌림목 ──
    adx = adx_from_closes(closes)
    holy_grail = None
    if adx is not None and ma20 and ma5:
        strong_trend = adx >= 30                       # 강한 추세 확정
        near_ma20 = abs(cur / ma20 - 1) * 100 <= 2     # 20일선 ±2% 눌림목
        uptrend = cur >= ma20 and ma5 >= ma20          # 상승 추세 구조
        if strong_trend and near_ma20 and uptrend:
            holy_grail = "long_setup"   # 강추세 상승 중 20일선 눌림목 = 매수 자리
        elif strong_trend and near_ma20 and (cur < ma20 and ma5 < ma20):
            holy_grail = "short_setup"  # 강추세 하락 중 20일선 반등 = (참고용)
    return {
        "symbol": sym,
        "price": round(cur, 2),
        "chg_1d_pct": round(chg1, 1),
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
        "ichimoku": ichimoku(closes),
        "adx14": adx,                  # 추세 강도(근사) — 30↑ 강추세
        "holy_grail": holy_grail,      # 린다 라쉬케 눌림목 신호
    }


HEDGE_STATE_FILE = "hedge_state.json"   # 헷지 진입/청산 시각 기록(휩쏘 방지)
HEDGE_MIN_HOLD_HOURS = 6                # 한 번 헷지하면 최소 6시간 유지(잦은 진입·청산 차단)


def _load_hedge_state():
    try:
        with open(HEDGE_STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_hedge_state(state):
    try:
        with open(HEDGE_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=1)
    except Exception as e:
        log(f"⚠️ hedge_state 저장 실패: {e}")


def _hours_since(iso_str):
    """ISO 시각 문자열로부터 지금까지 몇 시간 지났는지. 파싱 실패 시 큰 값."""
    try:
        t = datetime.datetime.fromisoformat(iso_str)
        now = datetime.datetime.now(t.tzinfo) if t.tzinfo else datetime.datetime.now()
        return (now - t).total_seconds() / 3600
    except Exception:
        return 9999


def hedge_can_exit(positions):
    """현재 인버스를 청산해도 되는지(쿨다운 경과 여부). 휩쏘 방지용.
    헷지 진입 후 HEDGE_MIN_HOLD_HOURS 안 지났으면 청산 금지."""
    held = {str(p.get("symbol", "")).upper() for p in (positions or [])}
    if not (held & INVERSE_TICKERS):
        return True   # 인버스 없으면 청산할 것도 없음
    state = _load_hedge_state()
    entered = state.get("entered_at")
    if not entered:
        return True   # 진입 기록 없으면(과거 매수 등) 청산 허용
    h = _hours_since(entered)
    return h >= HEDGE_MIN_HOLD_HOURS


def auto_hedge_decision(regime, positions, account, market):
    """하락장에서 인버스(SQQQ/SOXS) 헷지를 자동으로 넣는다(AI 재량이 아니라 코드로 강제).
    - 조정 국면: 헷지 안 함(현금 확보로 방어 — execute에서 매수 축소).
    - 하락장 국면: 인버스 미보유 + 쿨다운 경과 시 소량(자산 3%) 자동 진입.
    휩쏘 방지: 청산 직후 재진입을 막기 위해 마지막 청산 후에도 쿨다운을 본다.
    반환: 헷지 매수 decision 또는 None."""
    label = (regime or {}).get("label", "")
    if label != "하락장":
        return None
    # 이미 인버스 보유 중이면 추가 안 함
    held = {str(p.get("symbol", "")).upper() for p in (positions or [])}
    if held & INVERSE_TICKERS:
        return None
    # 휩쏘 방지: 직전 청산 후 쿨다운(6시간) 안 지났으면 재진입 금지
    state = _load_hedge_state()
    exited = state.get("exited_at")
    if exited and _hours_since(exited) < HEDGE_MIN_HOLD_HOURS:
        log(f"⏸ 헷지 재진입 보류 — 직전 청산 후 {HEDGE_MIN_HOLD_HOURS}시간 미경과(휩쏘 방지)")
        return None
    # 헷지 대상 선택: 반도체가 더 약하면 SOXS, 아니면 SQQQ
    breadth = regime.get("breadth", {})
    # 시장 전반 약세 → 나스닥 인버스(SQQQ) 기본. 반도체 신저가 많으면 SOXS.
    hedge_sym = "SQQQ"
    soxx = next((m for m in market if m.get("symbol") == "SOXX"), None)
    qqq = next((m for m in market if m.get("symbol") == "QQQ"), None)
    if soxx and qqq:
        if (soxx.get("chg_1d_pct") or 0) < (qqq.get("chg_1d_pct") or 0) - 1:
            hedge_sym = "SOXS"   # 반도체가 더 약하면 반도체 인버스
    # 인버스 시세가 watchlist에 있어야 매수 가능
    inv = next((m for m in market if m.get("symbol") == hedge_sym), None)
    if not inv or not inv.get("price"):
        return None
    try:
        equity = float(account.get("equity", 0) or 0)
    except (TypeError, ValueError):
        return None
    price = inv["price"]
    try:
        price = float(price)
    except (TypeError, ValueError):
        return None
    # 자산 3% 한도(분할 진입 — 한 번에 크게 헷지하지 않음), 최소 1주
    budget = equity * 0.03
    qty = int(budget / price) if price > 0 else 0
    if qty < 1:
        return None
    # 진입 시각 기록(청산 쿨다운 계산용)
    state["entered_at"] = datetime.datetime.now(
        datetime.timezone(datetime.timedelta(hours=9))).isoformat()
    state["entered_sym"] = hedge_sym
    state.pop("exited_at", None)
    _save_hedge_state(state)
    return {
        "symbol": hedge_sym, "action": "buy", "qty": qty,
        "conviction": "normal",
        "reason": f"[자동 헷지] 하락장 국면 — {hedge_sym} 인버스로 포트폴리오 방어(자산 3% 한도). "
                  f"폭: {breadth.get('summary','')}",
    }


def assess_breadth(market):
    """시장 폭(breadth) 계산 — 지수가 아니라 '실제 종목들이 얼마나 무너졌나'를 본다.
    트레이더들이 단기 조정과 진짜 하락장을 구분할 때 쓰는 핵심 지표.
    SPY는 빅테크가 버티면 멀쩡해 보여도, 폭은 표면 아래 약세를 드러낸다.
    반환: {pct_above_ma5, pct_declining, pct_new_low, count, summary}"""
    total = above5 = declining = newlow = 0
    for m in market:
        sym = m.get("symbol", "")
        if not sym or is_crypto(sym):
            continue
        # 인버스/레버리지는 폭 계산에서 제외(시장 방향과 반대거나 왜곡)
        if sym in INVERSE_TICKERS or sym in LEVERAGE_TICKERS:
            continue
        price = m.get("price")
        ma5 = m.get("ma5")
        chg = m.get("chg_1d_pct")
        off = m.get("off_52w_high_pct")
        if price is None:
            continue
        total += 1
        if ma5 and price >= ma5:
            above5 += 1
        if chg is not None and chg < 0:
            declining += 1
        if off is not None and off <= -25:   # 52주 고점 대비 -25%↓ = 신저가권
            newlow += 1
    if total < 10:
        return {"count": total, "pct_above_ma5": None, "pct_declining": None,
                "pct_new_low": None, "summary": "표본 부족"}
    pa = round(above5 / total * 100)
    pd = round(declining / total * 100)
    pn = round(newlow / total * 100)
    return {
        "count": total,
        "pct_above_ma5": pa,        # 5일선 위 비율 (높을수록 건강)
        "pct_declining": pd,        # 당일 하락 비율 (높을수록 약세)
        "pct_new_low": pn,          # 신저가권 비율 (높을수록 위험)
        "summary": f"5일선 위 {pa}% · 당일 하락 {pd}% · 신저가권 {pn}%",
    }


def assess_regime(market=None):
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

        # ── 국면 판정: 지수 추세 + 시장 폭(breadth) + VIX 종합 ──
        # 트레이더 방식: 지수만 보지 말고 '실제 종목들이 얼마나 무너졌나'(폭)를 함께 본다.
        breadth = assess_breadth(market) if market else {"pct_above_ma5": None,
            "pct_declining": None, "pct_new_low": None, "summary": "폭 미계산"}
        regime["breadth"] = breadth
        pa = breadth.get("pct_above_ma5")    # 5일선 위 비율
        pn = breadth.get("pct_new_low")      # 신저가권 비율

        # 폭이 나쁜지 판정 (과반이 5일선 아래 + 신저가 다수)
        breadth_bad = (pa is not None and pa <= 35) or (pn is not None and pn >= 30)
        breadth_weak = (pa is not None and pa <= 50) or (pn is not None and pn >= 20)

        if spy_ma60 is not None and drawdown is not None:
            # 1) 하락장: 지수도 무너지고(60일선 아래+고점-10%) 폭도 나쁨 → 헷지 국면
            if spy_ma60 < -2 and drawdown <= -10:
                regime["label"] = "하락장"
                regime["detail"] = "지수가 장기추세(60일선) 아래·고점 대비 큰 폭 하락. 방어/헷지 우선 국면."
            # 2) 폭이 심각하게 나쁘면 지수가 버텨도 하락장 취급 (섹터 광범위 붕괴)
            elif breadth_bad and (spy_ma20 is None or spy_ma20 < 0):
                regime["label"] = "하락장"
                regime["detail"] = f"지수는 버텨도 시장 폭 붕괴({breadth['summary']}). 표면 아래 광범위 약세 — 헷지 검토."
            # 3) 조정: 지수 단기 이탈 또는 폭 약세 → 현금 확보 국면
            elif (spy_ma20 is not None and spy_ma20 < -3) or breadth_weak:
                regime["label"] = "조정"
                regime["detail"] = f"단기추세 이탈 또는 폭 약화({breadth['summary']}). 신규 매수 축소·현금 확보 국면."
            # 4) 상승장: 지수 강세 + 폭 양호 + 변동성 안정
            elif (spy_ma20 is not None and spy_ma20 > 1
                  and (pa is None or pa >= 55)
                  and (vix_val is None or vix_val < 20)):
                regime["label"] = "상승장"
                regime["detail"] = "추세 양호·시장 폭 건강·변동성 안정. 정상 운용 가능."
            else:
                regime["label"] = "중립/혼조"
                regime["detail"] = f"뚜렷한 추세 없음({breadth['summary']}). 종목별 선별 대응."
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

# ── 종목 → 섹터 매핑 (히트맵 그룹핑용) ──
# 야후 batch quote는 섹터를 안 줘서 주요 종목을 직접 분류. 없는 종목은 "기타"로.
STOCK_SECTOR = {
    # 기술/반도체
    "NVDA": "반도체", "AVGO": "반도체", "AMD": "반도체", "MU": "반도체", "INTC": "반도체",
    "QCOM": "반도체", "TXN": "반도체", "AMAT": "반도체", "LRCX": "반도체", "KLAC": "반도체",
    "ADI": "반도체", "MRVL": "반도체", "NXPI": "반도체", "ON": "반도체", "MCHP": "반도체",
    "ASML": "반도체", "TSM": "반도체", "ARM": "반도체", "SMCI": "반도체",
    "CRDO": "반도체", "AMBA": "반도체", "NVTS": "반도체", "INDI": "반도체", "ALAB": "반도체",
    "SITM": "반도체", "MTSI": "반도체",
    "MSFT": "기술", "AAPL": "기술", "ORCL": "기술", "CRM": "기술", "ADBE": "기술",
    "NOW": "기술", "INTU": "기술", "CSCO": "기술", "IBM": "기술", "PLTR": "기술",
    "PANW": "기술", "CRWD": "기술", "SNPS": "기술", "CDNS": "기술", "FTNT": "기술",
    "WDAY": "기술", "DELL": "기술", "HPQ": "기술", "PATH": "기술",
    # 빅테크/커뮤니케이션
    "GOOGL": "커뮤니케이션", "META": "커뮤니케이션", "NFLX": "커뮤니케이션",
    "DIS": "커뮤니케이션", "CMCSA": "커뮤니케이션", "T": "커뮤니케이션", "TMUS": "커뮤니케이션",
    "VZ": "커뮤니케이션",
    # 임의소비재
    "AMZN": "임의소비재", "TSLA": "임의소비재", "HD": "임의소비재", "MCD": "임의소비재",
    "NKE": "임의소비재", "SBUX": "임의소비재", "LOW": "임의소비재", "GM": "임의소비재",
    "F": "임의소비재", "ABNB": "임의소비재", "AFRM": "임의소비재", "UPST": "임의소비재",
    # 필수소비재
    "WMT": "필수소비재", "COST": "필수소비재", "PG": "필수소비재", "KO": "필수소비재",
    "PEP": "필수소비재", "MO": "필수소비재", "KHC": "필수소비재", "PM": "필수소비재",
    # 금융
    "JPM": "금융", "BAC": "금융", "WFC": "금융", "GS": "금융", "MS": "금융",
    "C": "금융", "V": "금융", "MA": "금융", "AXP": "금융", "BRK-B": "금융",
    "SCHW": "금융", "BLK": "금융", "SOFI": "금융",
    # 헬스케어/바이오
    "LLY": "헬스케어", "UNH": "헬스케어", "JNJ": "헬스케어", "ABBV": "헬스케어",
    "MRK": "헬스케어", "PFE": "헬스케어", "TMO": "헬스케어", "ABT": "헬스케어",
    "CVS": "헬스케어", "GILD": "헬스케어", "BMY": "헬스케어",
    "TEM": "바이오", "RXRX": "바이오", "HIMS": "바이오", "CERT": "바이오", "NTLA": "바이오",
    # 산업재/우주항공
    "GE": "산업재", "CAT": "산업재", "BA": "산업재", "RTX": "산업재", "HON": "산업재",
    "UNP": "산업재", "DE": "산업재", "LMT": "산업재", "NOC": "산업재", "GEV": "산업재",
    "LUNR": "우주항공", "RKLB": "우주항공", "RDW": "우주항공", "ASTS": "우주항공", "PL": "우주항공",
    "SERV": "산업재",
    # 에너지
    "XOM": "에너지", "CVX": "에너지", "COP": "에너지", "SLB": "에너지",
    "BE": "에너지", "OKLO": "에너지", "SMR": "에너지", "FLNC": "에너지",
    # 양자/차세대
    "IONQ": "양자컴퓨팅", "RGTI": "양자컴퓨팅", "QBTS": "양자컴퓨팅",
}


def stock_sector(sym):
    """종목의 섹터를 반환. 매핑에 없으면 '기타'."""
    return STOCK_SECTOR.get(str(sym).upper(), "기타")


# ── 히트맵 전용 종목 (Finviz 스타일 촘촘한 트리맵용) ──
# 거래 watchlist와 분리. 섹터별 시총 상위 대표주를 꽉 채워 시장 전경을 보여준다.
# ETF는 제외(개별 종목만). 봇이 이들 시총·등락률만 따로 받아 heatmap.json에 저장.
HEATMAP_STOCKS = {
    "반도체": ["NVDA","AVGO","AMD","TSM","ASML","MU","INTC","QCOM","TXN","AMAT",
              "LRCX","KLAC","ADI","MRVL","NXPI","MCHP","ON","ARM","SMCI"],
    "기술": ["MSFT","AAPL","ORCL","CRM","ADBE","NOW","INTU","IBM","CSCO","ACN",
            "PLTR","PANW","CRWD","ANET","SNPS","CDNS","FTNT","DELL","WDAY"],
    "커뮤니케이션": ["GOOGL","META","NFLX","DIS","CMCSA","TMUS","VZ","T","CHTR","EA"],
    "임의소비재": ["AMZN","TSLA","HD","MCD","NKE","LOW","SBUX","BKNG","TJX","GM",
                "F","ABNB","MAR","ORLY","CMG"],
    "필수소비재": ["WMT","COST","PG","KO","PEP","PM","MO","MDLZ","CL","KHC","TGT","KDP"],
    "금융": ["BRK-B","JPM","V","MA","BAC","WFC","GS","MS","AXP","C","SCHW","BLK",
            "SPGI","C","PGR","CB"],
    "헬스케어": ["LLY","UNH","JNJ","ABBV","MRK","TMO","ABT","DHR","PFE","AMGN",
              "BMY","CVS","GILD","MDT","ISRG","VRTX"],
    "산업재": ["GE","CAT","RTX","HON","UNP","BA","DE","LMT","UPS","ETN","ADP",
            "NOC","GD","EMR","CSX","NSC"],
    "에너지": ["XOM","CVX","COP","SLB","EOG","MPC","PSX","WMB","OXY","VLO"],
    "필수재": ["LIN","SHW","APD","ECL","FCX","NEM","DOW","DD"],
    "유틸리티": ["NEE","SO","DUK","CEG","AEP","D","EXC","SRE"],
    "부동산": ["PLD","AMT","EQIX","WELL","SPG","O","DLR","PSA"],
}


def heatmap_symbols():
    """히트맵에 그릴 전체 종목 리스트(중복 제거)."""
    out = []
    seen = set()
    for syms in HEATMAP_STOCKS.values():
        for s in syms:
            if s not in seen:
                seen.add(s); out.append(s)
    return out


def heatmap_sector(sym):
    """히트맵 전용 섹터 매핑 (HEATMAP_STOCKS 기준)."""
    s = str(sym).upper()
    for sec, syms in HEATMAP_STOCKS.items():
        if s in syms:
            return sec
    return "기타"

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
        # 애프터마켓: 평일(월~금)이고 ET 16~20시일 때만. (주말 오후를 애프터로 오인하던 버그 수정)
        if nxt_open and nxt_close and now.date() != nxt_open.date():
            if now.weekday() < 5 and 16 <= now.hour < 20:
                return "after"
        return _closed_kind()
    except Exception:
        return "closed_offhours"


_SESSION_KR = {"regular": "미국 정규장(개장 중)", "pre": "미국 프리마켓(개장 전)",
               "after": "미국 애프터마켓(폐장 후)",
               "closed_offhours": "미국 장 마감 (거래 시간 외)",
               "closed_weekend": "미국 주말 휴장",
               "closed_holiday": "미국 공휴일 휴장",
               "closed": "미국 장 마감 (거래 시간 외)"}  # 하위호환


def market_is_open():
    """하위호환용: 정규장이면 True. (기존 호출부 유지)"""
    return get_market_session() == "regular"


# ── Claude에게 판단 요청 ──
def _claude_call_with_retry(body, tries=4):
    """Claude API 호출 + 529(Overloaded)·일시 오류 시 대기 후 재시도.
    Anthropic 서버 혼잡으로 봇이 통째로 죽는 걸 막는다."""
    last = None
    for attempt in range(tries):
        try:
            return http_json(
                "https://api.anthropic.com/v1/messages", method="POST",
                headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                body=body)
        except urllib.error.HTTPError as e:
            # 401·크레딧 부족 등 영구 오류는 재시도 안 함
            if e.code in (401, 403):
                raise
            last = e
            if attempt < tries - 1:
                wait = 5 * (attempt + 1)
                log(f"⏳ Claude 호출 일시 오류(HTTP {e.code}) — {wait}초 후 재시도 ({attempt+1}/{tries})")
                time.sleep(wait)
        except Exception as e:
            last = e
            if attempt < tries - 1:
                wait = 5 * (attempt + 1)
                log(f"⏳ Claude 호출 오류({e}) — {wait}초 후 재시도 ({attempt+1}/{tries})")
                time.sleep(wait)
    raise last if last else RuntimeError("Claude 호출 실패")


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
        "  ② 추세추종(비쌀 때 사서 더 비쌀 때 판다): 골든크로스·신고가 돌파·강한 상승추세에서 달리는 추세에 올라탐. 거래량이 뒷받침될 때만. ★ 핵심은 '주도 섹터에 올라타는 것'이다 — 섹터 흐름에서 주도 섹터(강세)가 확인되면 그 섹터 ETF·종목을 추세추종으로 따라붙어라. 이미 올랐다고 피하지 말고, 추세가 살아있으면 분할로 진입한다.\n"
        "- 시장 국면으로 둘 중 무엇을 쓸지 결정한다: 하락장/과매도 국면 → ①저점매수 위주(신중히). 상승장/추세 국면 → ②추세추종 위주. 혼조·불명확 → 기다림(관망).\n"
        "- 두 방식 모두 '좋은 자리를 기다린다'는 본질은 같다. 조급하게 진입하지 말 것.\n\n"
        "포트폴리오 구조 (중요):\n"
        "- 코어: M7(AAPL/MSFT/NVDA/GOOGL/AMZN/META/TSLA)과 기술 ETF(IGV/SOXX/SMH/QQQ)를 성장 축으로 운용. ★ 단 코어를 지수(QQQ/SPY)와 빅테크로만 채우지 말 것 — 지금 주도하는 섹터의 ETF·종목을 반드시 코어에 포함하라. 반도체 사이클이면 SOXX·SMH와 강한 반도체 종목(NVDA·AVGO·AMD·MU·AMAT·LRCX·KLAC 등)을 적극 담는 게 추세추종의 정석이다. '지수+빅테크 디폴트'로만 돌면 정작 사이클의 수익을 놓친다.\n"
        "- 분산: 금융·헬스케어·에너지·소비재·안전자산(GLD/TLT)에도 나눠 담아 한 섹터 쏠림을 피할 것. 단 분산이 '주도 섹터 회피'의 핑계가 되면 안 된다 — 분산은 하되 주도 섹터에 무게중심을 둬라.\n"
        "- 레버리지(TQQQ/SOXL/UPRO/QLD/TNA/FNGU/TECL/LABU/NVDL/TSLL): 3배 변동이라 위험. 상승 추세가 명확할 때만 단기 전술용으로. 확신이 강하면 종목당 최대 8%까지, 아니면 5% 이내. 레버리지+인버스 합산 평가액은 총자산의 15%를 절대 넘기지 말 것\n"
        "- 인버스(SQQQ/SOXS/SH/SDS): 지수가 20일선 아래로 꺾이는 등 하락 신호가 분명할 때 헤지용으로만. 같은 15% 한도 적용\n"
        "- 레버리지·인버스는 변동성 잠식이 있으니 보유가 길어지거나 근거가 사라지면 우선 정리 대상\n"
        "- RSI 과매도 + 볼린저 하단 같은 '싸게 살 기회' 역발상도 적극 활용\n- 암호화폐(BTC-USD/ETH-USD): 시험 운용 중. 변동성이 매우 크니 합산 평가액 총자산 5% 이내, 소수점 수량 사용 가능 (예: 0.02)\n\n"
        "하락장·조정 대응 원칙 (역대 약세장 2008·2020·2022의 교훈):\n"
        "- 현금은 무기다: 하락장에선 현금 비중을 늘리는 것 자체가 좋은 결정. 억지로 매수하지 말 것. 관망·현금 보유가 종종 최선\n"
        "- ★ 다만 현금 과다 보유도 '리스크'다: 상승장·중립장에서 현금을 80~90%씩 쌓아두는 것은 기회손실이다. 시장 국면이 하락장이 아니고, 주도 섹터에 명확한 추세가 있으면 '기다리는 매매'를 핑계로 무한정 관망하지 말고 적극 진입하라. 특히 주도 섹터 추세추종 매수는 확신이 서면 망설이지 말 것. 현금을 무기로 쓰는 것과, 기회를 놓치며 현금만 쌓는 것은 다르다.\n"
        "- 떨어지는 칼날 잡지 말 것: 급락 중인 종목을 '싸다'고 서둘러 사지 말 것. 하락 추세가 멈추고 바닥을 다지는 신호(거래량 동반 반등, MA5 회복)를 확인 후 진입\n"
        "- 손절은 빠르게: 하락장에선 손실이 더 빨리 커진다. 손절 기준(-7%, 레버리지 -5%)을 더 엄격히, 머뭇거리지 말 것\n"
        "- 분할 대응: 한 번에 다 사지 말고, 확신이 설 때 조금씩. 평균단가 낮추기(물타기)는 추세 확인 전엔 금물\n"
        "- 레버리지는 양날의 검: 하락·변동성 국면에서 레버리지 롱(TQQQ 등)은 변동성 잠식으로 치명적이라 추세가 명확히 돌기 전엔 피한다. 다만 '주도 섹터가 강한데 단기 급락 후 분명한 반등 신호가 나온 경우'는 하이리스크 슬롯으로 적극 포착할 가치가 있다(아래 하이리스크 규칙 참고). 무작정 금지가 아니라, 신호가 분명할 때만 빠르게 잡고 빠르게 손절한다.\n"
        "- 방어 자산: 하락장에선 GLD(금)·TLT(국채) 같은 안전자산 비중을 고려. 인버스(SQQQ 등)는 하락이 분명할 때만 소량 헤지\n"
        "- 공포에 휩쓸리지 말되 과신도 말 것: VIX가 극도로 높을 때(30+)는 역사적 저점 근처인 경우도 있으나, 섣부른 '바닥 매수'보다 안정 확인이 우선\n\n"
        "★★ 봇의 최우선 철학 — '하락장에 덜 잃는다': 월가의 명언처럼, 장기 수익률은 '크게 버는 것'보다 '크게 잃지 않는 것'이 결정한다. -50% 손실은 +100%를 벌어야 본전이다(복리의 비대칭). 그러니 ① 하락장·약세 신호가 보이면 방어(현금·손절)를 주저하지 말고, ② 상승·중립장에선 기회를 놓치지 말고 적극 매수해 현금이 무의미하게 쌓이지 않게 하라. 이 둘의 균형이 핵심이다.\n"
        "★★ 목표 포트폴리오 배분(상승·중립장 기준): ① 텐버거/하이리스크 슬롯에 시드의 약 20%까지 — '다음에 올 섹터'의 낌새(소외된 저평가 우량주, 신저가 근처 턴어라운드)가 보이면 미리 선점. ② 나머지는 현재 주도 섹터 추세추종(반도체 등 강세 섹터 ETF·대장주)으로 채워 분산. 한쪽에 몰지 말고 '미래 선점 20% + 현재 주도 다수'로 다각화하라. 단 하락장이면 이 배분을 미루고 방어 우선.\n\n"
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
        "    └ 일목균형표(ichimoku) 활용 — '기다리는 매매'의 핵심 도구: 각 종목의 ichimoku 필드를 본다. pos는 현재가와 구름의 관계다. 'above'=구름 위(상승추세·강세), 'in'=구름 안(중립·방향 모색), 'below'=구름 아래(하락추세·약세). dist_to_cloud_pct는 현재가가 구름에서 떨어진 거리%다. ★ 가장 좋은 매수 자리: 'above'(상승추세 유지)이면서 dist_to_cloud_pct가 작게 양수(예: +0~5%) = 오르던 주가가 구름 상단까지 눌려 내려와 '지지'를 테스트하는 눌림목이다. 구름이 지지선 역할을 하며 추세가 살아있을 가능성이 높아, 거기서 반등 신호(MA5 회복·거래량)가 나오면 '기다리던 자리'로 본다. 반대로 'below'(구름 아래)는 추세가 꺾인 약세라 떨어지는 칼날 위험이 크니 신규 매수를 피한다. 'in'(구름 안)은 방향 불명확이니 신중히. 단 일목도 만능이 아니라 다른 지표·펀더멘털과 함께 종합 판단하는 한 재료일 뿐이다.\n"
        "  ② 밸류에이션 저평가: 각 종목 지표의 valuation 필드(pe=PER, fwd_pe=선행PER, pb=PBR, roe=자기자본이익률%, psr=주가매출비율, off_52w_high_pct=52주고점대비낙폭%)를 본다. 동종 섹터·과거 대비 PER/PBR이 낮은데 펀더멘털은 망가지지 않은 종목. (valuation이 없으면 ①기술적 신호로만 판정)\n"
        "    └ 가치투자 참고 기준(데이터 있을 때): 그레이엄식 'PER 15 이하·PBR 1.5 이하(둘의 곱 22.5 이하)'는 싸게 사는 좋은 가이드다. 버핏식으로는 'ROE 15% 이상을 꾸준히 내는 자본효율 좋은 기업'이 위대한 기업이다. ★ 단 핵심은 '좋은 기업을 적당히 싸게'이지 PER 15·PSR 1.5 같은 고정 숫자에 기계적으로 얽매이는 게 아니다 — 애플·코카콜라처럼 위대한 기업은 PER이 높아도 살 가치가 있다. 숫자는 참고일 뿐, 펀더멘털(섹터 지위·성장성)과 함께 종합 판단하라.\n"
        "  → ① 또는 ② 중 하나라도 분명하고, 펀더멘털이 망가진 게 아니면 하이리스크 후보. 둘 다 충족이면 더 강한 신호.\n"
        "- 하락장 예외(중요): BNF 역추세는 원래 하락장 금지지만, 하이리스크 슬롯에 한해 '확실한 추세 전환 신호'가 보이면 하락장에서도 저가매수를 시도할 수 있다. 단 매우 신중히 — 추세 전환이 애매하면 하지 말 것. 막연한 '싸 보임'은 금지, 반드시 돌아서는 신호 확인.\n"
        "- 레버리지(TQQQ 등)도 하이리스크 슬롯에 포함 가능. 단 한 종목 한도 5%는 동일.\n"
        "- ★ 주도 섹터 레버리지 급락 반등 (적극 포착): 주도 섹터(예: 반도체)의 레버리지 ETF(SOXL·TQQQ 등)가 단기 급락(예: 하루 -12% 이상 또는 disparity_ma20_pct -15% 이하)한 뒤 '돌아서는 신호'가 나오면 하이리스크 슬롯으로 적극 진입하라. 신호 우선순위: ① rebound_from_low_pct(장중 저점 대비 반등률)가 크게 양수(+5% 이상) = 저점 찍고 튀는 중, ② MA5 회복 또는 거래량 동반 반등, ③ RSI 과매도 탈출. 이런 변동성 큰 종목의 'V자 반등'은 큰 수익 기회다. 단 레버리지는 손절 -5%로 더 빠르게, 포지션 5% 이내, 두 AI 합의 필수.\n"
        "  └ rebound_from_low_pct(장중 저점 대비 현재 반등률%)와 off_day_high_pct(당일 고점 대비 현재 낙폭%)가 제공되면 적극 활용하라. 예: rebound_from_low_pct +8%면 장중 바닥에서 이미 8% 튄 것 = 반등 시작 신호. 단 '급락 자체'가 아니라 '급락 후 반등 확인'이 핵심 — 아직 떨어지는 중(rebound 거의 0)이면 칼날이니 기다려라.\n"
        f"- 각 주문에 \"tier\" 필드를 넣어라: \"core\"(일반 안전 매수) | \"highrisk\"(저평가·과매도 역추세). 미지정 시 core로 간주.\n"
        "- 하이리스크(저평가·과매도 역추세) 매수는 원칙적으로 두 AI 합의 시 실행하되, '격리 예산(작은 금액)'이라 한쪽 AI라도 high 확신이면 소량(절반) 시험 진입이 허용된다. 그러니 저평가 우량주에 확신이 서면 conviction을 'high'로 명확히 표하라. '많이 빠진 저평가 우량주'를 영영 안 사고 현금만 쌓는 것은 기회손실이다 — 하이리스크 예산 범위 안에서는 적극적으로 담아라.\n"
        "- 매도: 보유 손익이 손절선(-7%, 레버리지 -5%) 이하인 '진짜 손절'은 한쪽만 원해도 즉시 실행(방어 우선). 그러나 손절선 위인데 '리밸런싱·자금확보·약세' 같은 이유로 파는 '재량 매도'는 두 AI가 모두 동의해야 실행된다. 단순히 '주도섹터 자금 확보'를 위해 멀쩡한 보유 종목을 혼자 팔지 말 것 — 현금은 이미 충분하다.\n\n"
        f"[현재 시장 국면] {json.dumps({k:v for k,v in regime.items() if k!='sectors'}, ensure_ascii=False) if regime else '판단 안 됨'}\n"
        "  → 위 국면을 반드시 반영할 것. '하락장'이면 인버스 헷지+현금 우선, '조정'이면 신규 매수를 크게 줄이고 현금 확보 우선. '상승장'이면 정상 운용.\n"
        + (f"[섹터 흐름] {(regime or {}).get('sectors',{}).get('summary','')}\n"
           f"  섹터별 1·3개월 수익률: {json.dumps((regime or {}).get('sectors',{}).get('ranked',[]), ensure_ascii=False)}\n"
           "  → ★ 돈이 도는 주도 섹터(강세)를 추세추종으로 적극 따라붙어라. 이게 이 봇의 핵심 수익원이다. 주도 섹터(예: 반도체 강세 사이클)면 그 섹터 ETF(SOXX/SMH 등)와 그 섹터 강한 개별 종목을 분할로 매수하라. '이미 많이 올랐다'는 이유만으로 주도 섹터를 통째로 회피하지 말 것 — 추세추종은 원래 오르는 걸 사는 전략이다. 추세가 살아있으면(MA5·MA20 위, 거래량 동반) 분할로 따라붙고, 눌림목(단기 조정)이 오면 추가하라.\n"
           "  → 단 '과열 극단'에서만 신규 진입을 자제한다: RSI 75 이상 + 볼린저 상단 크게 초과(1.1+) + 거래량 급감 같은 명백한 과열 신호가 동시에 보일 때. 그 외 추세 중간 구간(RSI 55~72)은 추세추종 정상 매수 구간이다. 막연히 '많이 올랐다'로 멈추지 말고, 과열 지표가 실제로 극단인지 확인하라.\n"
           "  → 소외 섹터(약세, 1개월 마이너스)는 신중히. 섹터 흐름은 개별 종목 신호·펀더멘털과 함께 종합 판단하되, 주도 섹터를 비우고 지수·빅테크만 도는 편향을 경계하라.\n\n"
           if (regime or {}).get('sectors') else "\n")
        + ("수익 종목 다루는 법 (★ 수익은 길게 가져간다 — let winners run):\n"
           "- 손실은 빨리 자르되(손절 -7%), 수익 나는 종목은 섣불리 팔지 마라. 큰 수익(텐버거)은 끝까지 들고 가야 나온다.\n"
           "- ★ 러너 보호: 평단 대비 +15% 이상 수익 중인 종목은 'RSI 과매수' 하나만으로 절대 팔지 마라. RSI가 70~80이어도 추세(MA20 위, 상승 흐름)가 살아있으면 계속 보유한다. 과매수는 강세장에서 오래 지속될 수 있다.\n"
           "- ★ 트레일링 스탑: 각 보유 종목에 peak_pnl_pct(보유 중 최고 수익률)와 drawdown_from_peak_pct(고점 대비 현재 하락폭, 음수)가 제공된다. 수익 종목은 이걸로 관리하라:\n"
           "  · peak가 큰 종목(+15% 이상 찍었던 종목)은, 고점 대비 -20%p 이상 빠지기 전(drawdown_from_peak_pct > -20)까지는 홀드. 추세가 살아있는 한 끝까지 태운다.\n"
           "  · 고점 대비 -20%p 이상 빠지고(drawdown_from_peak_pct <= -20) 추세도 꺾이면(MA20 이탈 등) 그때 매도해 수익을 보전한다. 이게 '수익은 길게, 꺾이면 보전'이다.\n"
           "  · 예: A종목이 +200% 찍었다가 현재 +175%(고점 대비 -25%p)면서 MA20 이탈 → 트레일링 스탑 매도. +50% 찍었다가 현재 +45%면 아직 홀드(고점 대비 -5%p).\n"
           "- 단 이건 '수익 종목'에만 적용. 손실 종목은 기존 손절 규칙(-7%)을 따른다. 또 개별 악재(실적 쇼크 등)로 펀더멘털이 망가지면 트레일링과 무관하게 매도 검토.\n"
           "- ★ 텐버거(장기보유) 종목: 보유 포지션 중 is_tenbagger=true인 종목은 '10배주 후보'로 확정된 장기보유 종목이다. 5~10년 보고 묻어두는 자리라 단기 손절(-7%)·트레일링·과매수 매도를 적용하지 마라. 일시적으로 마이너스여도 버틴다(엉덩이 싸움). 오직 '펀더멘털이 실제로 망가졌다'고 판단될 때만 매도를 검토하라.\n"
           "- ★ 텐버거 적립 원칙: 텐버거 종목은 종목 수 제한 없이 하이리스크 예산(20%) 안에서 '꾸준히 분할 적립'하는 게 목표다. 신규 편입 종목이나 기존 텐버거가 저가(눌림목·과매도)에 오면 소량씩 계속 모아가라. 한 번에 크게 사지 말고 시간을 두고 평단을 쌓는 적립식으로.\n\n")
        + session_rule
        + f"[계좌] 총자산 ${account['equity']}, 현금 ${account['cash']}\n"
        f"[보유 포지션]\n{json.dumps(positions, ensure_ascii=False, indent=1)}\n\n"
        f"[관심종목 지표]\n{json.dumps(market, ensure_ascii=False, indent=1)}\n\n"
        "규칙:\n"
        f"- 신규 매수 한도(종목당 총자산 대비): 확신이 보통이면 {int(MAX_POSITION_PCT*100)}%, 확신이 강하면 최대 {int(MAX_POSITION_HIGH*100)}%, 확신이 약하면 3% 이내\n"
        "- 각 주문에 \"conviction\" 필드를 넣으세요: \"high\"(강한 확신) | \"normal\"(보통) | \"low\"(시험적). 신호·펀더멘털·시장국면이 모두 우호적이고 자리가 분명할 때만 high.\n"
        "- ★ 매수 크기를 의미 있게: 진입할 거면 제대로 진입하라. high 확신이고 자리가 좋으면 첫 진입부터 한도의 절반 이상(자산 5~6%)을 담아라. normal이어도 최소 자산 2~3%는 들어가라. '1주씩 찔끔' 매수는 금지 — 이겨도 수익이 작아 다른 손실을 못 메운다. 단 한 종목 한도(normal 5%, high 12%)는 지킬 것.\n"
        "- ★★ 진입 타이밍 엄격(눌림목·지지선에서만): 추격 매수 금지. 신규 매수는 다음 중 하나가 충족되는 '좋은 자리'에서만 하라 — ① RSI가 과매도권(40 이하)에서 반등 조짐, ② 일목균형표 구름 상단·지지선 근처로 눌린 자리, ③ MA20 위에서 MA5까지 되돌린 눌림목. 이미 단기 급등(5일 +10%↑)했거나 자리가 애매하면 아무리 좋은 종목이어도 '관망'하라. 좋은 종목을 나쁜 자리에 사면 물린다.\n"
        "- ★★★ 홀리 그레일 신호(린다 라쉬케 추세 눌림목): 종목 지표의 holy_grail='long_setup'은 'ADX 30↑ 강추세 + 20일선 눌림목'이 동시 충족된 최우선 매수 자리다(adx14로 추세 강도 확인). 이 신호가 뜬 종목은 추세가 강하면서 마침 눌린 것이라 가장 좋은 진입처다 — 발견 시 우선 고려하고 conviction을 높여 의미 있는 크기로 진입하라. 단 신호가 떠도 시장 국면이 하락장이면 신중히(방어 우선). adx14가 30 미만이면 추세가 약하니 추세추종 매수는 자제.\n"
        "- 인버스 종목과 하락장·조정 국면에서는 conviction과 무관하게 5% 이내로 제한됩니다(시스템이 강제). 레버리지는 확신이 강할 때만 최대 8%.\n"
        "- 매수는 관심종목 내에서만, 매도는 보유 종목만, 공매도 금지\n"
        "- 손실 중인 포지션이 -7% 이하면 손절을 적극 검토 (레버리지는 -5%)\n"
        "- 거래할 이유가 약하면 빈 배열로 응답 (거래 안 함이 기본값)\n\n"
        "아래 JSON 형식으로만 응답하세요. 다른 텍스트, 마크다운 백틱 금지:\n"
        '{"decisions":[{"action":"buy|sell","symbol":"JPM","qty":3,"conviction":"normal","tier":"core","reason":"한 문장 근거"}],'
        '"market_view":"오늘 시장 국면 판단, 왜 매수/매도/관망했는지 핵심 근거, 포트폴리오 분산·레버리지·현금 비중에 대한 평가를 자세히. 나중에 사람이 봇의 판단을 복기할 수 있도록 솔직하고 구체적으로 충분히 설명하되, 5~8문장(800자 내외)을 넘지 말 것. 반드시 완결된 JSON으로 끝맺고 마지막 문장은 마침표로 끝낼 것."}'
    )
    res = _claude_call_with_retry(
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
        return _consensus(claude_plan, deepseek_plan, positions, regime)
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


def _consensus(claude_plan, deepseek_plan, positions=None, regime=None):
    """두 AI 판단을 합의.
    매수(C안): 둘 다 동의 시 실행. 주도섹터 코어는 한쪽 high 확신+상대 매도 안 하면 소량 진입.
    매도(A안): '진짜 손절'(보유 손익이 손절선 -7%, 레버리지 -5% 이하)은 한쪽만 원해도 즉시 실행(방어 우선).
              그 외 '재량 매도'(리밸런싱·자금확보·약세 등 손절선 위)는 양쪽 합의해야 실행.
              → DeepSeek이 손절선 한참 위인데 '주도섹터 자금 확보'라며 혼자 다 팔아 현금만
                쌓이던 문제를 막는다. 진짜 위험할 때 손절은 그대로 빠르게 작동."""
    c_dec = claude_plan.get("decisions", []) or []
    d_dec = deepseek_plan.get("decisions", []) or []

    LEAD_CORE = {"SOXX", "SMH", "NVDA", "AVGO", "AMD", "MU", "AMAT", "LRCX", "KLAC", "QQQ"}
    tenbaggers = load_tenbaggers()   # 장기보유: 단기 손절 면제(합의 있어야 매도)

    # 보유 종목의 현재 손익률 맵 (손절선 판정용)
    pnl_map = {}
    for p in (positions or []):
        try:
            pnl_map[str(p.get("symbol", "")).upper()] = float(p.get("pnl_pct", 0))
        except Exception:
            pass

    def _is_stoploss(sym):
        """해당 매도가 진짜 손절선(-7%, 레버리지 -5%)을 넘었는지.
        단 텐버거(장기보유) 종목은 단기 손절 면제 → 재량 매도로 간주(합의 필요)."""
        if sym in tenbaggers:
            return False
        if sym not in pnl_map:
            return False   # 보유 정보 없으면 재량으로 간주(합의 필요)
        line = -5.0 if sym in LEVERAGE_TICKERS else -7.0
        return pnl_map[sym] <= line

    def key(x):
        return (str(x.get("symbol", "")).upper(), str(x.get("action", "")).lower())

    c_map = {key(x): x for x in c_dec}
    d_set = {key(x) for x in d_dec}
    c_sell = {s for (s, a) in c_map if a == "sell"}
    d_sell = {key(y)[0] for y in d_dec if key(y)[1] == "sell"}
    # 양쪽이 같이 팔자고 한 종목(재량 매도라도 합의되면 실행)
    both_sell = {s for s in c_sell if s in d_sell}

    merged = []
    consensus_log = []
    done_sell = set()   # 중복 매도 방지
    regime_label = (regime or {}).get("label", "")
    defensive = regime_label in ("하락장", "조정")   # 방어 국면이면 솔로 진입 보수적

    def _solo_lead_buy(x, sym, opponent_sells):
        # 한쪽 AI만 매수를 원할 때 소량 진입을 허용할지 판정.
        if sym in opponent_sells:   # 상대가 매도를 원함 = 적극 반대 → 진입 안 함
            return False
        conv = str(x.get("conviction", "")).lower()
        tier = str(x.get("tier", "core")).lower()
        # 주도섹터 코어: high 확신은 항상, normal 확신도 '방어 국면이 아니면' 소량 진입
        #  → 상승·중립장에서 주도섹터를 사자는데 합의가 안 돼 현금만 쌓이던 교착을 푼다.
        if sym in LEAD_CORE:
            if conv == "high":
                return True
            if conv == "normal" and not defensive:
                return True
        # 저평가·과매도 하이리스크: 격리 예산(작은 금액)이라 한쪽 high 확신이면 소량 진입
        if tier == "highrisk" and conv == "high":
            return True
        return False

    def _handle_sell(x, sym, who):
        """A안 매도 처리: 손절선 초과면 한쪽만으로 실행, 아니면 합의(both_sell) 필요."""
        if sym in done_sell:
            return
        if _is_stoploss(sym):
            done_sell.add(sym); merged.append(x)
            consensus_log.append(f"{sym} 손절({who})")
        elif sym in both_sell:
            done_sell.add(sym); merged.append(x)
            consensus_log.append(f"{sym} 매도(합의)")
        else:
            consensus_log.append(f"{sym} 매도 보류({who}만·손절선 위)")

    for k, x in c_map.items():
        sym, action = k
        if action == "sell":
            _handle_sell(x, sym, "Claude")
        elif action == "buy":
            if k in d_set:
                d_x = next((y for y in d_dec if key(y) == k), None)
                try:
                    cq = float(x.get("qty", 0)); dq = float(d_x.get("qty", 0)) if d_x else cq
                    x = dict(x); x["qty"] = min(cq, dq)
                except Exception:
                    pass
                merged.append(x); consensus_log.append(f"{sym} 매수(합의)")
            elif _solo_lead_buy(x, sym, d_sell):
                x = dict(x)
                try:
                    x["qty"] = max(1, int(float(x.get("qty", 1)) / 2))
                except Exception:
                    x["qty"] = 1
                merged.append(x); consensus_log.append(f"{sym} 매수(Claude주도·소량)")
            else:
                consensus_log.append(f"{sym} 매수 보류(Claude만)")

    # DeepSeek만 원한 매수 중 주도섹터 코어 소량 진입
    for y in d_dec:
        sym, action = key(y)
        if action == "buy" and (sym, "buy") not in c_map:
            if _solo_lead_buy(y, sym, c_sell):
                y = dict(y)
                try:
                    y["qty"] = max(1, int(float(y.get("qty", 1)) / 2))
                except Exception:
                    y["qty"] = 1
                merged.append(y); consensus_log.append(f"{sym} 매수(DeepSeek주도·소량)")

    # DeepSeek 매도 처리 (A안: 손절선 초과만 단독 실행, 재량은 합의 필요)
    for y in d_dec:
        sym, action = key(y)
        if action == "sell":
            _handle_sell(y, sym, "DeepSeek")

    view = "🤝 합의: " + (", ".join(consensus_log) if consensus_log else "거래 없음") + \
           " | Claude: " + _clip_sentence(claude_plan.get("market_view", ""), 4000) + \
           " | DeepSeek: " + _clip_sentence(deepseek_plan.get("market_view", ""), 4000)
    return {"decisions": merged, "market_view": view}

    # 앱용 market_view는 잘림 없이 두 AI의 전체 분석을 그대로 담는다(사실상 무제한,
    # 폭주 방어용 넉넉한 상한만). 알림은 format_view_for_push에서 각 250자로
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


def execute(decisions, account, positions, market, regime=None, block_buys=False):
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
        # 자동 매도(손절·트레일링)인지 판별 — 인버스 쿨다운에서 예외 처리용
        is_auto_sell = ("자동 손절" in reason or "트레일링" in reason
                        or d.get("stoploss_triggered") or d.get("trailing_stop_triggered"))
        conviction = str(d.get("conviction", "normal")).lower()  # high | normal | low
        tier = str(d.get("tier", "core")).lower()                 # core | highrisk
        is_highrisk = (tier == "highrisk")
        # 서킷브레이커: 계좌가 고점 대비 크게 빠진 상태면 신규 매수 전면 차단(손절·매도는 허용)
        if action == "buy" and block_buys:
            results.append(f"⛔ {sym} 매수 차단 (서킷브레이커 발동 — 계좌 방어)")
            continue
        if qty <= 0 or sym not in prices:
            results.append(f"⛔ {sym} 건너뜀 (잘못된 주문)")
            continue
        cost = qty * prices[sym]
        if action == "buy":
            is_lev = sym in LEVERAGE_TICKERS
            is_inv = sym in INVERSE_TICKERS
            regime_label = (regime or {}).get("label", "")
            defensive = regime_label in ("하락장", "조정")
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

            # ★ 최소 매수 보장: 일반주를 살 거면 '의미 있는 크기'로 산다(1주씩 찔끔 방지).
            # AI가 너무 작게 주문하면 자산 2%까지 끌어올린다(단 한도 pos_cap 이내).
            # 하이리스크·인버스·레버리지·코인은 위험하므로 제외(작게 사는 게 맞음).
            MIN_BUY_PCT = 0.02
            if (not is_highrisk and not is_inv and not is_lev and not crypto
                    and not defensive and conviction in ("high", "normal")):
                min_cost = equity * MIN_BUY_PCT
                cap_cost = equity * pos_cap
                if cost < min_cost and min_cost <= cap_cost and prices[sym] > 0:
                    qty = round(min_cost / prices[sym], 4)
                    cost = qty * prices[sym]

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
            # 휩쏘 방지: 인버스(헷지)는 쿨다운(6시간) 안에 AI 재량으로 못 판다.
            # 단 손절·트레일링(자동 매도)은 진짜 손실 방어이므로 통과시킨다.
            if sym in INVERSE_TICKERS and not is_auto_sell:
                if not hedge_can_exit(positions):
                    results.append(f"⏸ {sym} 헷지 청산 보류 (최소 보유 {HEDGE_MIN_HOLD_HOURS}시간 미경과 — 휩쏘 방지)")
                    continue
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
            # 인버스(헷지) 청산이면 시각 기록 → 재진입 쿨다운 계산용(휩쏘 방지)
            if sym in INVERSE_TICKERS and side == "sell":
                st = _load_hedge_state()
                st["exited_at"] = datetime.datetime.now(
                    datetime.timezone(datetime.timedelta(hours=9))).isoformat()
                st.pop("entered_at", None)
                _save_hedge_state(st)
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
    알림용으로 Claude·DeepSeek 구별되게 줄바꿈 분리.
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

    # 알림은 길이 제한을 고려해 각 AI 멘트를 250자로 요약한다.
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


def send_discord(title, message, urgent):
    """디스코드 웹훅으로 알림 전송(임베드 카드). 색: 긴급=빨강, 일반=청록.
    웹훅이 설정 안 됐으면 조용히 건너뜀. 실패해도 봇 진행에 지장 없게 예외 흡수."""
    if not DISCORD_WEBHOOK:
        return
    # 디스코드 임베드 description은 4096자 제한 → 넉넉히 자름
    desc = message if len(message) <= 4000 else message[:3990] + "…"
    color = 0xE74C3C if urgent else 0x1ABC9C   # 빨강 / 청록
    payload = json.dumps({
        "embeds": [{
            "title": title[:256],
            "description": desc,
            "color": color,
        }],
    }).encode("utf-8")
    try:
        req = urllib.request.Request(
            DISCORD_WEBHOOK, data=payload,
            headers={"Content-Type": "application/json",
                     "User-Agent": "ConsensusBot/1.0 (+https://github.com/robotkimus/Sado-app)"})
        urllib.request.urlopen(req, timeout=20)
    except urllib.error.HTTPError as e:
        # 403·400 등은 응답 본문에 진짜 이유가 들어있음 → 찍어서 원인 파악
        try:
            body = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            body = ""
        log(f"⚠️ 디스코드 전송 실패(무시): HTTP {e.code} — {body}")
    except Exception as e:
        log(f"⚠️ 디스코드 전송 실패(무시): {e}")


def send_push(title, message, urgent):
    # 디스코드 웹훅으로 알림 전송.
    if not DISCORD_WEBHOOK:
        log("DISCORD_WEBHOOK 없음 — 알림 생략")
        return
    send_discord(title, message, urgent)


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


def _fill_price_for(sym, market):
    """거래 종목의 현재가(≈체결가)를 market 데이터에서 찾는다. journal 진입가 기록용."""
    if not sym:
        return None
    for m in market or []:
        if str(m.get("symbol", "")).upper() == str(sym).upper():
            return m.get("price")
    return None


def _archive_journal(entries):
    """trade_journal에서 잘려나가는 오래된 기록을 월별 파일로 영구 보존.
    journal_archive/2026-06.json 형태로 누적 → 2~3년치 빅데이터로 쌓인다.
    (trade_journal.json은 앱 표시용 최근 200개만, 아카이브는 전체 보존)"""
    if not entries:
        return
    try:
        os.makedirs("journal_archive", exist_ok=True)
    except Exception as e:
        log(f"⚠️ 아카이브 폴더 생성 실패: {e}")
        return
    # 기록을 연-월별로 묶어 각 파일에 append
    by_month = {}
    for e in entries:
        ym = str(e.get("time", ""))[:7] or "unknown"   # "2026-06"
        by_month.setdefault(ym, []).append(e)
    for ym, items in by_month.items():
        path = f"journal_archive/{ym}.json"
        try:
            existing = []
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    existing = json.load(f)
                if not isinstance(existing, list):
                    existing = []
            existing.extend(items)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(existing, f, ensure_ascii=False, indent=1)
            log(f"🗄 아카이브 보존: {path} (+{len(items)}건, 총 {len(existing)}건)")
        except Exception as e:
            log(f"⚠️ 아카이브 저장 실패({ym}): {e}")


def track_runners(positions):
    """종목별 '보유 중 최고 수익률(peak_pnl)'을 runners.json에 누적 추적하고,
    텐버거 트레일링 스탑을 '자동으로' 발동한다.
    - 각 포지션에 peak_pnl_pct(최고점), drawdown_from_peak_pct(고점 대비 하락폭),
      trailing_stop_triggered(자동 매도 발동 여부)를 붙인다.
    - 수익을 길게 태우되(let winners run), +15%↑ 찍었던 종목이 고점 대비 -20%p(대박 +50%↑는
      -30%p) 꺾이면 자동으로 매도 신호를 만들어 AI가 깜빡해도 수익을 보전한다.
    반환: (positions, trailing_sells) — trailing_sells는 자동 발동된 매도 decision 리스트."""
    try:
        with open("runners.json", encoding="utf-8") as f:
            peaks = json.load(f)
        if not isinstance(peaks, dict):
            peaks = {}
    except Exception:
        peaks = {}

    trailing_sells = []
    stoploss_sells = []
    held = set()
    tenbaggers = load_tenbaggers()   # 장기보유 종목: 트레일링·손절 면제
    for p in positions:
        sym = p.get("symbol")
        if not sym:
            continue
        held.add(sym)
        try:
            pnl = float(p.get("pnl_pct", 0))
        except (TypeError, ValueError):
            pnl = 0.0
        prev_peak = peaks.get(sym, {}).get("peak_pnl_pct", pnl)
        try:
            prev_peak = float(prev_peak)
        except (TypeError, ValueError):
            prev_peak = pnl
        peak = max(prev_peak, pnl)
        drawdown = round(pnl - peak, 1)   # 0 또는 음수(고점 대비 하락폭 %p)
        peaks[sym] = {"peak_pnl_pct": round(peak, 1)}
        p["peak_pnl_pct"] = round(peak, 1)
        p["drawdown_from_peak_pct"] = drawdown

        # ── 자동 트레일링 스탑 판정 ──
        # 충분히 수익났던(peak>=15%) 종목이 고점 대비 과하게 빠지면 자동 매도.
        # 단 현재도 손실 종목(pnl<=0)은 트레일링이 아니라 기존 손절(-7%) 규칙 영역이라 제외.
        triggered = False
        if sym not in tenbaggers and peak >= RUNNER_MIN_PEAK_PCT and pnl > 0:
            trail = RUNNER_BIG_TRAIL_DROP_PCT if peak >= RUNNER_BIG_PEAK_PCT else RUNNER_TRAIL_DROP_PCT
            if drawdown <= -trail:
                triggered = True
                trailing_sells.append({
                    "symbol": sym, "action": "sell",
                    "qty": p.get("qty"), "conviction": "high", "tier": "core",
                    "reason": f"트레일링 스탑 자동 발동 — 고점 +{peak:.0f}% 대비 {drawdown:.0f}%p 하락, 수익 보전",
                })
        p["trailing_stop_triggered"] = triggered
        p["is_tenbagger"] = sym in tenbaggers   # 앱·AI 표시용(장기보유 종목)

        # ── 자동 손절(로스컷) 판정 ──
        # 손실이 손절선(-7%, 레버리지 -5%)을 넘으면 AI 판단과 무관하게 강제 매도.
        # 텐버거(장기보유)는 면제. 트레일링이 이미 걸린 종목은 중복 방지.
        stopped = False
        if sym not in tenbaggers and not triggered:
            line = STOPLOSS_LEV_PCT if sym in LEVERAGE_TICKERS else STOPLOSS_PCT
            # 분할 착시 방어: 하루에 정상적으로 -35%를 넘는 손실은 거의 없다.
            # 액면분할 후 Alpaca 평단가가 미조정돼 생기는 가짜 대손실로 멀쩡한 포지션을
            # 손절하는 사고를 막는다. 이런 종목은 손절 보류 + 경고만(사람이 확인).
            if pnl <= -35:
                log(f"⚠️ {sym} 손실 {pnl:.1f}% — 비정상적으로 큼. 액면분할/데이터 오류 의심으로 "
                    f"자동 손절 '보류'. 평단가를 직접 확인하세요(분할이면 정상 포지션).")
                p["split_suspect"] = True
            elif pnl <= line:
                stopped = True
                stoploss_sells.append({
                    "symbol": sym, "action": "sell",
                    "qty": p.get("qty"), "conviction": "high", "tier": "core",
                    "reason": f"자동 손절 발동 — 손실 {pnl:.1f}% (기준 {line:.0f}%), 추가 하락 방어",
                })
        p["stoploss_triggered"] = stopped

    # 더 이상 보유하지 않는 종목은 정리(매도됨)
    for sym in list(peaks.keys()):
        if sym not in held:
            peaks.pop(sym, None)

    try:
        with open("runners.json", "w", encoding="utf-8") as f:
            json.dump(peaks, f, ensure_ascii=False, indent=1)
    except Exception as e:
        log(f"⚠️ runners.json 저장 실패: {e}")
    # 자동 발동 매도 = 트레일링(수익 보전) + 손절(손실 방어)
    return positions, (trailing_sells + stoploss_sells)


def load_tenbaggers():
    """사람이 확정한 장기보유 텐버거 종목 집합을 로드(tenbagger.txt, 한 줄 1티커).
    여기 있는 종목은 단기 손절·트레일링을 면제해 장기 보유를 존중한다."""
    out = set()
    try:
        with open(TENBAGGER_FILE, encoding="utf-8") as f:
            for line in f:
                t = line.strip().upper()
                if t and not t.startswith("#"):
                    out.add(t)
    except Exception:
        pass
    return out


def score_tenbagger_candidates(market):
    """watchlist 데이터에서 텐버거 4박자를 정량 점수화해 상위 후보를 추린다.
    기준(받을 수 있는 데이터만): 소외(신저가 근처) + 과매도 반등 + 저평가 + 작은 몸집.
    메가트렌드·해자 같은 정성 판단은 사람 몫이라 여기선 정량 신호만 본다."""
    cands = []
    for m in market:
        sym = m.get("symbol", "")
        if not sym or is_crypto(sym) or sym in TENBAGGER_EXCLUDE:
            continue
        val = m.get("valuation") or {}
        score = 0
        reasons = []

        # ① 소외된 우량주: 52주 고점 대비 깊은 하락(신저가 근처)
        off = m.get("off_52w_high_pct")
        if off is None:
            off = val.get("off_52w_high_pct")
        if off is not None and off <= TENBAGGER_DEEP_DIP_PCT:
            score += 2; reasons.append(f"52주고점 대비 {off:.0f}%")

        # ② 과매도 탈출 + 반등 신호 (떨어지는 칼날 회피)
        rsi = m.get("rsi14")
        price, ma5 = m.get("price"), m.get("ma5")
        if rsi is not None and rsi < 40 and price and ma5 and price >= ma5:
            score += 2; reasons.append("과매도 탈출+MA5 회복")
        elif rsi is not None and rsi < 35:
            score += 1; reasons.append(f"RSI {rsi:.0f} 과매도")

        # ③ 저평가(그레이엄 기준): 선행 PER 낮음 + PBR 낮음 + 둘의 곱 22.5 이하
        fpe = val.get("fwd_pe")
        pe = val.get("pe")
        pb = val.get("pb")
        use_pe = fpe if (fpe is not None and fpe > 0) else (pe if (pe is not None and pe > 0) else None)
        if use_pe is not None and use_pe < 15:
            score += 2; reasons.append(f"PER {use_pe:.0f}(저평가)")
        elif use_pe is not None and use_pe < 20:
            score += 1
        if pb is not None and 0 < pb < 1.5:
            score += 1; reasons.append(f"PBR {pb:.1f}")
        # 그레이엄 복합 기준: PER × PBR ≤ 22.5 (둘 다 있을 때)
        if use_pe is not None and pb is not None and 0 < use_pe * pb <= 22.5:
            score += 1; reasons.append("그레이엄 기준 충족")

        # ③-2 우량성(버핏 ROE): ROE 15% 이상 = 자본 효율 좋은 위대한 기업 (데이터 있을 때만)
        roe = val.get("roe")
        if roe is not None and roe >= 15:
            score += 2; reasons.append(f"ROE {roe:.0f}%(우량)")

        # ④ 가벼운 몸집: 소형~미드캡 가산
        mcap = val.get("market_cap")
        if mcap is not None and 0 < mcap < TENBAGGER_SMALLCAP_MAX:
            score += 2; reasons.append(f"시총 ${mcap/1e9:.1f}B(소형)")

        if score >= 4:   # 4점 이상만 후보로 (4박자 중 2개 이상 강하게 충족)
            cands.append({"symbol": sym, "score": score,
                          "price": m.get("price"), "reasons": reasons})

    cands.sort(key=lambda x: x["score"], reverse=True)
    return cands[:8]   # 상위 8개만


def _ai_verify_tenbagger(cand):
    """후보 1종목을 Claude+DeepSeek가 교차검증. 둘 다 'YES'면 (True, 사유).
    장기보유 텐버거로 편입해도 되는지 — 적자 지속·상폐·펀더멘털 훼손 위험을 본다.
    한쪽이라도 실패/거부면 편입 안 함(엄격)."""
    sym = cand["symbol"]
    reasons = ", ".join(cand.get("reasons", []))
    q = (
        f"종목 {sym}을(를) '장기보유 텐버거(10배주 후보)'로 편입할지 판단해줘.\n"
        f"이 종목은 정량 스크리닝에서 {cand['score']}점(만점12)을 받았다. 근거: {reasons}.\n"
        "텐버거로 편입되면 5~10년 장기보유하며 단기 손절·트레일링이 면제된다(매우 신중해야 함).\n"
        "다음 위험을 점검해: ① 만성 적자로 생존이 위태로운가 ② 상장폐지·감자 위험이 있는가 "
        "③ 펀더멘털(사업모델·재무)이 구조적으로 망가졌는가 ④ 단순 테마성 급등주인가.\n"
        "위 위험이 없고 '장기 우상향 잠재력이 있는 저평가 우량주'라고 판단되면 YES, "
        "조금이라도 위험하면 NO.\n"
        '반드시 이 JSON 형식으로만: {"verdict":"YES|NO","reason":"한 문장 사유"}'
    )
    def _ask_one(call_fn):
        try:
            text = call_fn(q)
            j = _parse_ai_json(text, "verify")
            v = str(j.get("verdict", "")).upper()
            return ("YES" in v, j.get("reason", ""))
        except Exception as e:
            log(f"⚠️ 텐버거 검증 호출 실패({e}) → 안전하게 NO 처리")
            return (False, "검증 실패")

    # Claude
    def claude_fn(prompt):
        res = _claude_call_with_retry(
            body={"model": "claude-sonnet-4-6", "max_tokens": 300,
                  "messages": [{"role": "user", "content": prompt}]})
        return "".join(b.get("text", "") for b in res.get("content", []))
    c_ok, c_reason = _ask_one(claude_fn)

    # DeepSeek (없으면 편입 보류 — 엄격 원칙상 단독 편입 금지)
    if not DEEPSEEK_KEY:
        return (False, "DeepSeek 없음 — 교차검증 불가로 편입 보류")
    def ds_fn(prompt):
        res = http_json(
            "https://api.deepseek.com/chat/completions", method="POST",
            headers={"Authorization": f"Bearer {DEEPSEEK_KEY}", "content-type": "application/json"},
            body={"model": "deepseek-chat", "max_tokens": 300,
                  "messages": [{"role": "user", "content": prompt}]})
        return res.get("choices", [{}])[0].get("message", {}).get("content", "")
    d_ok, d_reason = _ask_one(ds_fn)

    both = c_ok and d_ok
    reason = f"Claude: {c_reason} / DeepSeek: {d_reason}"
    return (both, reason)


def auto_enroll_tenbagger(candidates):
    """배틀로얄 자동 편입: 10점 이상 후보 중 최고점 1개를 교차검증해
    둘 다 OK면 tenbagger.txt에 추가한다. 매 실행 최대 1종목(신중·비용절감).
    반환: 편입된 종목명 또는 None."""
    existing = load_tenbaggers()
    # 갯수 상한(0=무제한)
    if TENBAGGER_MAX_HOLDINGS and len(existing) >= TENBAGGER_MAX_HOLDINGS:
        return None
    # 10점 이상 & 아직 미편입 = 결승 진출자
    finalists = [c for c in candidates
                 if c["score"] >= TENBAGGER_AUTO_SCORE
                 and c["symbol"].upper() not in existing]
    if not finalists:
        return None
    # 배틀로얄 우승자: 최고점 → (동점 시) 소형주·신저가 깊은 순은 점수에 이미 반영됨
    finalists.sort(key=lambda x: x["score"], reverse=True)
    winner = finalists[0]
    log(f"🏆 텐버거 결승 진출 {len(finalists)}종목 → 우승 후보 {winner['symbol']}({winner['score']}점) 교차검증 시작")

    ok, reason = _ai_verify_tenbagger(winner)
    if not ok:
        log(f"🚫 {winner['symbol']} 편입 보류 — 교차검증 실패. {reason}")
        return None

    # tenbagger.txt에 추가
    try:
        line = f"{winner['symbol']}  # 자동편입 {winner['score']}점 · {datetime.date.today().isoformat()}\n"
        with open(TENBAGGER_FILE, "a", encoding="utf-8") as f:
            f.write(line)
        log(f"✅ 텐버거 자동 편입: {winner['symbol']} ({winner['score']}점) — {reason}")
        return winner["symbol"]
    except Exception as e:
        log(f"⚠️ 텐버거 파일 추가 실패: {e}")
        return None


def check_circuit_breaker(current_equity):
    """계좌 전체 방어. equity_history 기준 '직전 고점 대비 현재 낙폭'을 계산해,
    -15% 넘게 빠졌으면 신규 매수를 중단(보유 유지·손절은 허용)한다.
    반환: (block_buys: bool, drawdown_pct: float, peak: float)
    '하락장에 덜 잃는다'를 종목별 손절 위에서 한 번 더 보장하는 상위 안전장치."""
    try:
        with open("equity_history.json", encoding="utf-8") as f:
            hist = json.load(f)
    except Exception:
        hist = []
    equities = []
    for h in hist:
        try:
            equities.append(float(h.get("equity")))
        except (TypeError, ValueError):
            continue
    try:
        cur = float(current_equity)
    except (TypeError, ValueError):
        return False, 0.0, 0.0
    equities.append(cur)
    if len(equities) < 2:
        return False, 0.0, cur
    peak = max(equities)
    if peak <= 0:
        return False, 0.0, cur
    drawdown = (cur / peak - 1) * 100
    block = drawdown <= CIRCUIT_DRAWDOWN_PCT
    return block, round(drawdown, 1), peak


def _has_crypto_opportunity(market, positions):
    """프리마켓·휴장(코인만 보는 시간)에 AI를 부를 가치가 있는지 판정.
    - 보유 코인이 있으면 True (손절·익절 판단 필요)
    - 코인이 '저가 구간(taco_zone/과매도/큰 괴리)'에 있으면서 '반등이 실제로 시작된 신호'
      (MA5 회복 또는 거래량 동반 또는 저점 대비 반등)가 함께 있을 때만 True
    - 그 외에는 False → AI 호출을 건너뛰어 비용 절감.
    두 AI는 매수의 마지막 조건으로 '반등 확인'을 요구하므로, 반등 신호 없이
    과매도/taco_zone만으로 부르면 100% 관망으로 끝나 토큰만 낭비됨. 그래서 반등 신호를 필수로 둠."""
    # 보유 코인이 있으면 무조건 점검 필요(손절·트레일링)
    if any(is_crypto(p.get("symbol", "")) for p in positions):
        return True, "보유 코인 점검"

    for m in market:
        if not is_crypto(m.get("symbol", "")):
            continue
        rsi = m.get("rsi")
        disp = m.get("disparity_ma20_pct")
        price = m.get("price")
        ma5 = m.get("ma5")
        vol_ratio = m.get("volume_vs_20d_avg", 0) or 0
        rebound = m.get("rebound_from_low_pct", 0) or 0

        # 1) '저가 구간'인가 (싸게 살 만한 위치)
        cheap = bool(m.get("in_taco_zone")) \
            or (rsi is not None and rsi < 35) \
            or (disp is not None and disp <= -10)

        # 2) '반등이 실제로 시작된 신호'가 있는가 (떨어지는 칼날이 아닌 증거)
        rebound_started = (
            (price is not None and ma5 is not None and price >= ma5)  # MA5 회복
            or vol_ratio >= 1.2          # 거래량 동반(평균의 1.2배↑)
            or rebound >= 5              # 장중 저점 대비 +5%↑ 반등
        )

        # 저가 구간 + 반등 시작이 '동시에' 있어야 AI 호출 (둘 중 하나만으론 관망 확정 → 스킵)
        if cheap and rebound_started:
            return True, f"{m['symbol']} 저가구간+반등신호"

        # 강한 과매도 극단(RSI 30 미만)은 반등 전이라도 한 번은 볼 가치가 있음(예외)
        if rsi is not None and rsi < 30:
            return True, f"{m['symbol']} RSI {rsi:.0f} 극단 과매도"

    return False, ""


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
    # 529(Overloaded)·500대 일시 오류는 Anthropic 서버 혼잡이므로 몇 번 재시도한다.
    ping_ok = False
    last_err = ""
    for attempt in range(4):
        try:
            ping = http_json(
                "https://api.anthropic.com/v1/messages", method="POST",
                headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                body={"model": "claude-sonnet-4-6", "max_tokens": 16,
                      "messages": [{"role": "user", "content": "ping"}]})
            log("✅ [2단계] Claude API 연결 성공")
            ping_ok = True
            break
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            last_err = f"HTTP {e.code} {detail}"
            # 401(키 오류)·크레딧 부족은 재시도해도 소용없으니 즉시 중단
            if e.code == 401:
                log(f"❌ [2단계] Claude API 실패 ({last_err})")
                log("   → API 키가 잘못됐어요. console.anthropic.com 에서 키를 재확인하세요.")
                return 1
            if e.code == 400 and "credit" in detail.lower():
                log(f"❌ [2단계] Claude API 실패 ({last_err})")
                log("   → 크레딧 부족이에요. Billing에서 충전 상태를 확인하세요.")
                return 1
            # 529(과부하)·500대 등 일시 오류 → 대기 후 재시도
            wait = 5 * (attempt + 1)   # 5, 10, 15초
            log(f"⏳ [2단계] Claude API 일시 오류({last_err}) — {wait}초 후 재시도 ({attempt+1}/4)")
            time.sleep(wait)
        except Exception as e:
            last_err = str(e)
            wait = 5 * (attempt + 1)
            log(f"⏳ [2단계] Claude API 오류({last_err}) — {wait}초 후 재시도 ({attempt+1}/4)")
            time.sleep(wait)

    if not ping_ok:
        # 4번 재시도도 실패: Anthropic 서버 혼잡일 가능성이 큼.
        # 봇을 죽이지 않고 '이번 실행만 관망'으로 우아하게 종료(다음 시간에 재시도).
        log(f"❌ [2단계] Claude API 연결 실패(재시도 소진): {last_err}")
        log("   → Anthropic 서버 과부하일 수 있어요. 이번 실행은 건너뛰고 다음 시간에 재시도합니다.")
        return 0   # exit 0: 워크플로 실패로 안 뜨게(빨간 X 방지)


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
    positions, auto_sells = track_runners(positions)  # 트레일링(수익보전) + 손절(손실방어) 자동 발동
    if auto_sells:
        for ts in auto_sells:
            log(f"🔔 자동 매도 발동: {ts['symbol']} — {ts['reason']}")

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
            s = summarize(sym, fetch_daily(sym))
            if s.get("price") is None or s.get("error"):
                log(f"⚠️ {sym} 데이터 부실 — 제외 ({s.get('error','가격없음')})")
            else:
                market.append(s)
        except Exception as e:
            log(f"⚠️ {sym} 시세 실패: {e}")
        time.sleep(0.4)  # 종목이 많아 데이터 소스 차단 방지용 간격
    log(f"✅ [3단계] 시세 확보 {len(market)}/{len(targets)} 종목")

    # 거래 가능 세션(정규장·애프터마켓)이면 snapshot으로 장중 저점 대비 반등 신호를 주입.
    # SOXL 등 변동성 큰 종목이 장중 -16% 빠졌다 반등하는 흐름을 봇이 포착하게 함.
    if STOCK_TRADABLE and market:
        snaps = fetch_snapshots([m["symbol"] for m in market])
        for m in market:
            snap = snaps.get(m["symbol"])
            if not snap:
                continue
            lp = snap.get("price")
            if lp and m.get("price"):
                if IS_AFTER_HOURS:
                    m["regular_close"] = m["price"]               # 정규장 종가 보존
                    m["afterhours_chg_pct"] = round((lp / m["price"] - 1) * 100, 1)
                m["price"] = round(lp, 2)                          # 현재가를 최신 체결가로
            # 장중 저점 대비 반등률 / 고점 대비 낙폭 (급락 후 반등 포착용)
            if snap.get("rebound_from_low_pct") is not None:
                m["rebound_from_low_pct"] = snap["rebound_from_low_pct"]
            if snap.get("off_day_high_pct") is not None:
                m["off_day_high_pct"] = snap["off_day_high_pct"]
        # 로그: 장중 저점에서 크게 튄 종목(반등 +5%↑) 부각
        rebounders = [m for m in market if m.get("rebound_from_low_pct", 0) >= 5]
        if rebounders:
            log("📈 장중 저점 대비 반등(+5%↑): " +
                ", ".join(f"{m['symbol']} +{m['rebound_from_low_pct']:.1f}%" for m in rebounders))
        if IS_AFTER_HOURS:
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

    # ── 텐버거(10배주) 후보 추천 — 봇은 후보만, 편입은 사람이 tenbagger.txt에 ──
    tenbagger_candidates = []
    try:
        tenbagger_candidates = score_tenbagger_candidates(market)
        kst_now = datetime.datetime.now(
            datetime.timezone(datetime.timedelta(hours=9))).strftime("%Y-%m-%d %H:%M")
        with open(TENBAGGER_CAND_FILE, "w", encoding="utf-8") as f:
            json.dump({"updated": kst_now, "candidates": tenbagger_candidates},
                      f, ensure_ascii=False, indent=1)
        if tenbagger_candidates:
            finalists = [c for c in tenbagger_candidates if c["score"] >= TENBAGGER_AUTO_SCORE]
            if finalists:
                fl = ", ".join(f"{c['symbol']}({c['score']})" for c in finalists)
                log(f"🏆 텐버거 결승 진출({TENBAGGER_AUTO_SCORE}점+): {fl}")
            else:
                top = tenbagger_candidates[0]
                log(f"💎 텐버거 후보 {len(tenbagger_candidates)}개 (최고 {top['symbol']} {top['score']}점, 결승 진출자 없음)")
        # ── 배틀로얄 자동 편입: 10점 이상 후보 중 최고점 1개를 교차검증해 편입 ──
        # 정규장에서만(비용·데이터 신뢰성), 매 실행 최대 1종목.
        if STOCK_TRADABLE:
            enrolled = auto_enroll_tenbagger(tenbagger_candidates)
            if enrolled:
                send_push(
                    "🏆 텐버거 자동 편입",
                    f"{enrolled} 종목이 두 AI 교차검증을 통과해 장기보유 텐버거로 편입됐어요.\n"
                    f"이제 단기 손절·트레일링 면제로 꾸준히 적립합니다.",
                    True)
    except Exception as e:
        log(f"⚠️ 텐버거 후보 분석 실패: {e}")

    # 시장 전체 국면 판단 (지수 + 시장 폭 종합 — 하락장/조정 대응용)
    regime = assess_regime(market)
    log(f"✅ [3.5단계] 시장 국면: {regime.get('label')} — {regime.get('detail','')}")

    # 섹터 흐름(로테이션) 분석 — 돈이 어디로 도는지
    try:
        sectors = assess_sectors()
        regime["sectors"] = sectors
        log(f"✅ [3.6단계] 섹터 흐름: {sectors.get('summary','')}")
    except Exception as e:
        log(f"⚠️ [3.6단계] 섹터 분석 실패: {e}")
        regime["sectors"] = None

    # ── 비용 절감: 코인만 보는 시간대(프리마켓·휴장)에 코인 기회가 없으면 AI 호출 스킵 ──
    # 두 AI를 풀로 부르는 건 비싸므로, 살 신호도 없고 보유 코인도 없으면 관망 기록만 남기고 종료.
    if not STOCK_TRADABLE:
        has_opp, why = _has_crypto_opportunity(market, positions)
        if not has_opp:
            log("💤 코인 신호 없음 — AI 호출 건너뜀(비용 절감). 관망 기록만 저장.")
            now_str = datetime.datetime.now(
                datetime.timezone(datetime.timedelta(hours=9))).strftime("%Y-%m-%d %H:%M")
            status = {
                "updated": now_str,
                "session": _SESSION_KR.get(MARKET_SESSION, MARKET_SESSION),
                "equity": float(account.get("equity", 0)),
                "cash": float(account.get("cash", 0)),
                "base": 100000.0,
                "positions": positions,
                "market_view": f"[{_SESSION_KR.get(MARKET_SESSION, MARKET_SESSION)}] "
                               "미국 주식 거래 시간이 아니고 코인도 살 만한 신호가 없어 관망. "
                               "(AI 판단 생략 — 비용 절감)",
                "trades": [],
            }
            try:
                with open("bot_status.json", "w", encoding="utf-8") as f:
                    json.dump(status, f, ensure_ascii=False, indent=1)
            except Exception as e:
                log(f"⚠️ bot_status.json 저장 실패: {e}")
            # 뉴스는 계속 갱신 (앱 표시용, AI와 무관)
            try:
                held_syms = [p["symbol"] for p in positions if not is_crypto(p["symbol"])]
                general = fetch_market_news(limit=30)
                held_news = fetch_market_news(held_syms, limit=15) if held_syms else []
                seen, merged = set(), []
                for n in held_news + general:
                    u = n.get("url", "")
                    if u and u not in seen:
                        seen.add(u); merged.append(n)
                with open("news.json", "w", encoding="utf-8") as f:
                    json.dump({"updated": now_str, "items": merged[:40]}, f, ensure_ascii=False, indent=1)
            except Exception as e:
                log(f"⚠️ news.json 저장 실패: {e}")
            return 0

    try:
        plan = ask_claude(account, positions, market, regime, MARKET_SESSION)
        log("✅ [4단계] Claude 판단 수신")
    except Exception as e:
        log(f"❌ [4단계] Claude 판단 실패: {e}")
        send_push("🤝 컨센서스 봇 — 판단 실패", f"AI API 오류: {e}", True)
        return 1

    decisions = plan.get("decisions", []) or []
    view = plan.get("market_view", "")
    # 자동 발동 매도(트레일링=수익보전, 손절=손실방어)를 AI 판단과 합친다.
    # AI가 깜빡하거나 판단이 흔들려도 강제 실행. 같은 종목을 AI도 팔기로 했으면 중복 제거.
    if auto_sells:
        ai_sell_syms = {str(d.get("symbol", "")).upper() for d in decisions
                        if str(d.get("action", "")).lower() == "sell"}
        for ts in auto_sells:
            if str(ts["symbol"]).upper() not in ai_sell_syms:
                decisions.append(ts)
    # 하락장 자동 헷지: regime이 '하락장'이고 인버스 미보유면 코드로 강제 진입(AI 재량 아님).
    # 조정 국면은 헷지 대신 현금 확보(execute에서 매수 축소)로 방어.
    hedge = auto_hedge_decision(regime, positions, account, market)
    if hedge:
        already = {str(d.get("symbol", "")).upper() for d in decisions}
        if hedge["symbol"].upper() not in already:
            decisions.append(hedge)
            log(f"🛡️ 하락장 자동 헷지 추가: {hedge['symbol']} {hedge['qty']}주")
    # 서킷브레이커: 계좌 전체가 고점 대비 크게 빠졌으면 신규 매수 중단(방어 최우선)
    block_buys, dd_pct, _peak = check_circuit_breaker(account.get("equity"))
    if block_buys:
        log(f"🛑 서킷브레이커 발동 — 고점 대비 {dd_pct}% 하락. 신규 매수 중단(보유 유지·손절만 허용).")
    results = execute(decisions, account, positions, market, regime, block_buys) if decisions else []

    # 체결(⛔ 제외)이 있었으면 포지션을 재조회해 '매도 전 스냅샷'이 아닌 최신 상태를 반영.
    # (예전엔 execute 전에 조회한 positions를 그대로 저장해, 방금 판 종목이 포트폴리오에
    #  남아 다음 실행에야 빠지는 '늦은 업데이트' 버그가 있었음.)
    # 실제 '체결'만 센다. ⛔(차단)·⏸(보류)는 체결이 아니므로 제외.
    executed = [r for r in results
                if not r.startswith("⛔") and not r.startswith("⏸")]
    if executed:
        time.sleep(3)  # 시장가 체결이 알파카에 반영될 시간을 잠깐 줌
        refreshed = fetch_positions()
        if refreshed or not positions:
            positions, _ = track_runners(refreshed)
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
    # 알림 발송 정책:
    # - 거래가 체결됐으면 시간 무관 항상 알림(중요). 애프터마켓에서 체결돼도 알림 옴.
    # - 거래 없는 관망: 정규장(STOCK_MARKET_OPEN)에서만 알림.
    #   프리마켓·애프터마켓·휴장에 거래 없이 관망한 건 알림이 무의미·피곤 → 생략.
    #   (애프터마켓도 관망 시엔 알림 생략. 단 애프터에 실제 체결되면 위 executed 조건으로 알림 옴.)
    #   bot_status·뉴스는 아래에서 계속 갱신되므로 앱에서 보는 데는 지장 없음.
    if executed or STOCK_MARKET_OPEN:
        send_push(title, body, bool(executed))
    else:
        log("🔕 정규장 외 관망 — 거래 없어 알림 생략(앱 상태·뉴스는 갱신).")

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
        # 합의는 됐지만 한도·예산·서킷브레이커로 이번에 못 산 건수(앱에 '+N건 보류'로 표시)
        "last_hold_count": len([
            r for r in results
            if (r.startswith("⛔") or r.startswith("⏸")) and "주문 실패" not in r
        ]),
    }
    with open("bot_status.json", "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=1)
    log("✅ [5단계] bot_status.json 저장 완료")

    # ── 시장 뉴스 수집 → news.json (앱 표시용, 거래 판단과 무관) ──
    try:
        held_syms = [p["symbol"] for p in positions if not is_crypto(p["symbol"])]
        general = fetch_market_news(limit=30)              # 전체 시장 주요 뉴스
        held_news = fetch_market_news(held_syms, limit=15) if held_syms else []
        # 중복 제거(url 기준), 보유종목 뉴스 우선 + 일반 뉴스
        seen, merged = set(), []
        for n in held_news + general:
            u = n.get("url", "")
            if u and u not in seen:
                seen.add(u); merged.append(n)
        news_doc = {"updated": now_str, "items": merged[:40]}
        with open("news.json", "w", encoding="utf-8") as f:
            json.dump(news_doc, f, ensure_ascii=False, indent=1)
        log(f"✅ news.json 저장 완료 ({len(news_doc['items'])}건)")
    except Exception as e:
        log(f"⚠️ news.json 저장 실패(앱 표시만 영향): {e}")

    # ── 히트맵 데이터 저장 (heatmap.json) — Finviz 스타일 전용 종목 ──
    # 거래 watchlist와 별개로 섹터별 대표주(시총 상위)를 촘촘히 보여준다.
    # 시총·등락률을 batch quote로 한 번에 받아 효율적(일봉 안 받음).
    try:
        hsyms = heatmap_symbols()
        hvals = fetch_valuations(hsyms)   # 시총·등락률·가격을 한 번에
        heat_items = []
        for sym in hsyms:
            v = hvals.get(sym.upper()) or {}
            chg = v.get("chg_pct")
            mcap = v.get("market_cap")
            if chg is None and mcap is None:
                continue   # 데이터 아예 못 받은 종목은 제외
            heat_items.append({
                "sym": sym,
                "sector": heatmap_sector(sym),
                "chg": chg if chg is not None else 0,
                "mcap": mcap,
                "price": v.get("price"),
            })
        heat_doc = {
            "updated": datetime.datetime.now(
                datetime.timezone(datetime.timedelta(hours=9))).strftime("%Y-%m-%d %H:%M"),
            "items": heat_items,
        }
        with open("heatmap.json", "w", encoding="utf-8") as f:
            json.dump(heat_doc, f, ensure_ascii=False, indent=1)
        log(f"✅ heatmap.json 저장 완료 ({len(heat_items)}종목)")
    except Exception as e:
        log(f"⚠️ heatmap.json 저장 실패(앱 표시만 영향): {e}")

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
            # 보유 종목 요약 (평단 대비 손익 + 진입 추적용 평단가·현재가)
            pos_summary = [
                {"sym": p["symbol"], "qty": p["qty"], "pnl_pct": p.get("pnl_pct", 0),
                 "avg_cost": p.get("avg_cost"), "now": p.get("now")}
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
                     # 체결가(진입/청산가) — 나중에 '이 판단이 수익이었나' 추적의 핵심
                     "fill_price": _fill_price_for(d.get("symbol"), market),
                     "reason": str(d.get("reason", ""))[:100]}
                    for d in decisions
                ] if decisions else [],
                "executed": executed,
                "positions": pos_summary,
                "market_view": view,
            }
            journal.append(entry)
            # 최근 200개는 trade_journal.json(앱 표시·빠른 조회용)에 유지하되,
            # 잘려나가는 오래된 기록은 월별 아카이브에 영구 보존 → 2~3년 빅데이터로 축적.
            if len(journal) > 200:
                overflow = journal[:-200]
                _archive_journal(overflow)
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
