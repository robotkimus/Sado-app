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
    return {
        "symbol": sym,
        "price": round(closes[-1], 2),
        "chg_5d_pct": round(chg5, 1),
        "chg_20d_pct": round(chg20, 1),
        "ma5": round(ma5, 2) if ma5 else None,
        "ma20": round(ma20, 2) if ma20 else None,
        "ma60": round(ma60, 2) if ma60 else None,
        "rsi14": round(rsi, 1) if rsi else None,
        "bollinger_pos_0to1": round((closes[-1] - (ma20 - 2 * sd)) / (4 * sd), 2) if ma20 and sd else None,
        "volume_vs_20d_avg": round(vol_ratio, 2),
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


def market_is_open():
    """미국 주식 정규장이 열려 있는지 알파카 클락으로 확인"""
    try:
        return bool(alpaca("/v2/clock").get("is_open"))
    except Exception:
        return False


# ── Claude에게 판단 요청 ──
def ask_claude(account, positions, market, regime=None):
    prompt = (
        "당신은 미국 주식 포트폴리오 매니저입니다. 모의계좌를 운용 중입니다.\n"
        "기술적 지표 기반의 스윙 전략을 따르되, 확신이 없으면 거래하지 않는 것이 원칙입니다.\n\n"
        "핵심 매매 철학 (반드시 따를 것):\n"
        "- '기다리는 매매'가 가장 중요하다. 아무 때나 사지 말고, 좋은 자리가 올 때까지 기다린다. 애매하면 거래하지 않는 것이 정답.\n"
        "- 두 가지 진입 방식을 시장 국면에 맞게 골라 쓴다:\n"
        "  ① 저점매수(쌀 때 사서 비쌀 때 판다): 과매도(RSI 30 이하)·볼린저 하단·공포 극심 구간에서 역발상 매수. 단, 떨어지는 칼날 잡지 말고 하락이 멈추고 바닥 다지는 신호(거래량 동반 반등, MA5 회복) 확인 후 진입.\n"
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
        f"[현재 시장 국면] {json.dumps(regime, ensure_ascii=False) if regime else '판단 안 됨'}\n"
        "  → 위 국면을 반드시 반영할 것. '하락장/조정'이나 '단기 약세'면 신규 매수를 크게 줄이고 방어·현금 우선. '상승장'이면 정상 운용.\n\n"
        f"[계좌] 총자산 ${account['equity']}, 현금 ${account['cash']}\n"
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
        '{"decisions":[{"action":"buy|sell","symbol":"JPM","qty":3,"conviction":"normal","reason":"한 문장 근거"}],'
        '"market_view":"오늘 시장 국면 판단, 왜 매수/매도/관망했는지 핵심 근거, 포트폴리오 분산·레버리지·현금 비중에 대한 평가를 2~3문장으로. 나중에 사람이 봇의 판단을 복기할 수 있도록 솔직하고 구체적으로."}'
    )
    res = http_json(
        "https://api.anthropic.com/v1/messages", method="POST",
        headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        body={"model": "claude-sonnet-4-6", "max_tokens": 1500,
              "messages": [{"role": "user", "content": prompt}]})
    text = "".join(b.get("text", "") for b in res.get("content", []))
    text = text.replace("```json", "").replace("```", "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("Claude 응답에 JSON이 없음: " + text[:200])
    return json.loads(text[start:end + 1])


# ── 주문 검증 + 실행 ──
STOCK_MARKET_OPEN = True  # main에서 매 실행마다 갱신


def execute(decisions, account, positions, market, regime=None):
    equity = float(account["equity"])
    cash = float(account["cash"])
    held = {p["symbol"]: float(p["qty"]) for p in positions}
    prices = {m["symbol"]: m["price"] for m in market}
    results = []
    for d in decisions[:MAX_TRADES_PER_RUN]:
        sym = str(d.get("symbol", "")).upper()
        crypto = is_crypto(sym)
        try:
            qty = round(float(d.get("qty", 0)), 6 if crypto else 4)
        except (TypeError, ValueError):
            qty = 0
        action = d.get("action")
        reason = str(d.get("reason", ""))[:120]
        conviction = str(d.get("conviction", "normal")).lower()  # high | normal | low
        if qty <= 0 or sym not in prices:
            results.append(f"⛔ {sym} 건너뜀 (잘못된 주문)")
            continue
        cost = qty * prices[sym]
        if action == "buy":
            # 포지션 한도 결정 (확신도 차등)
            #  - 인버스(SQQQ 등): 항상 5% 고정 (헤지용)
            #  - 하락장/조정: 차등 없이 5% (방어)
            #  - 레버리지(TQQQ 등): 확신 강하면 최대 8%, 아니면 5% (3배 변동이라 일반주보다 낮게)
            #  - 일반주 강한 확신(high): 최대 12% / 보통: 5% / 약함(low): 3%
            is_lev = sym in LEVERAGE_TICKERS
            is_inv = sym in INVERSE_TICKERS
            regime_label = (regime or {}).get("label", "")
            defensive = regime_label in ("하락장/조정", "단기 약세")
            if is_inv or defensive:
                pos_cap = MAX_POSITION_PCT                              # 5%
            elif is_lev:
                pos_cap = MAX_POSITION_LEV if conviction == "high" else MAX_POSITION_PCT  # 8% or 5%
            elif conviction == "high":
                pos_cap = MAX_POSITION_HIGH                             # 12%
            elif conviction == "low":
                pos_cap = 0.03                                          # 3%
            else:
                pos_cap = MAX_POSITION_PCT                              # 5%

            # 이미 보유 중이면, 합산이 한도를 넘지 않게 (분할 매수 누적 방지)
            held_val = held.get(sym, 0) * prices[sym]
            max_total = equity * pos_cap
            room = max(0, max_total - held_val)
            if cost > room:
                qty = round(room / prices[sym], 6 if crypto else 4)
                cost = qty * prices[sym]
            if qty <= 0 or cost > cash - equity * MIN_CASH_BUFFER_PCT:
                results.append(f"⛔ {sym} 매수 보류 (현금/한도)")
                continue
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
        # 미국 주식장이 닫혀 있으면 주식 주문은 보류 (체결 대기 방지), 코인은 24시간 진행
        if not crypto and not STOCK_MARKET_OPEN:
            results.append(f"⏸ {sym} 보류 (미국장 마감 — 개장 시 재검토)")
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
            else:
                order = {"symbol": to_alpaca_symbol(sym), "qty": str(qty),
                         "side": side, "type": "market", "time_in_force": "day"}
            alpaca("/v2/orders", method="POST", body=order)
            mark = "🔴 매수" if side == "buy" else "🔵 매도"
            results.append(f"{mark} {sym} {qty}{'개' if crypto else '주'} (~${cost:,.0f}) — {reason}")
            cash = cash - cost if side == "buy" else cash + cost
        except Exception as e:
            results.append(f"⛔ {sym} 주문 실패: {e}")
    return results


def send_push(title, message, urgent):
    if not NTFY_TOPIC:
        log("NTFY_TOPIC 없음 — 알림 생략")
        return
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
    global STOCK_MARKET_OPEN
    STOCK_MARKET_OPEN = market_is_open()
    log(f"🕐 미국 주식장: {'개장 중' if STOCK_MARKET_OPEN else '마감 (코인만 거래)'}")

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
        send_push("🤖 Claude 봇 — 주문 보류", msg, True)
        return 0
    positions = []
    for p in positions_raw:
        try:
            positions.append({
                "symbol": normalize_position_symbol(p.get("symbol", "")),
                "qty": float(p.get("qty", 0)),
                "avg_cost": float(p.get("avg_entry_price", 0)),
                "now": float(p.get("current_price", 0) or p.get("avg_entry_price", 0)),
                "pnl_pct": round(float(p.get("unrealized_plpc", 0)) * 100, 1),
            })
        except Exception as e:
            log(f"⚠️ 포지션 파싱 오류 ({p.get('symbol','?')}): {e}")

    # 미국장 마감 시에는 코인만 수집 (주식은 어차피 보류되므로 헛수집·크레딧 절약)
    if not STOCK_MARKET_OPEN:
        targets = [s for s in watch if s in CRYPTO]
        log(f"💤 미국장 마감 — 코인만 점검 ({len(targets)}종목)")
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

    if not market:
        send_push("🤖 Claude 봇 — 실행 실패", "시세를 하나도 못 가져왔어요.", True)
        return 1

    # 시장 전체 국면 판단 (하락장 대응용)
    regime = assess_regime()
    log(f"✅ [3.5단계] 시장 국면: {regime.get('label')} — {regime.get('detail','')}")

    try:
        plan = ask_claude(account, positions, market, regime)
        log("✅ [4단계] Claude 판단 수신")
    except Exception as e:
        log(f"❌ [4단계] Claude 판단 실패: {e}")
        send_push("🤖 Claude 봇 — 판단 실패", f"Claude API 오류: {e}", True)
        return 1

    decisions = plan.get("decisions", [])
    view = plan.get("market_view", "")
    results = execute(decisions, account, positions, market, regime) if decisions else []

    pos_line = ", ".join(f"{p['symbol']} {p['pnl_pct']:+.1f}%" for p in positions) or "없음"
    body_parts = [f"💼 총자산 ${float(account['equity']):,.0f} · 보유: {pos_line}"]
    if view:
        body_parts.append(f"🧠 {view}")
    body_parts.append("\n".join(results) if results else "오늘은 거래 없음 (관망)")
    body_parts.append("※ 모의계좌 자동매매 · 참고용")
    body = "\n\n".join(body_parts)

    # 실제 체결(⛔ 제외)이 있었는지 판별
    executed = [r for r in results if not r.startswith("⛔")]
    kst = datetime.timezone(datetime.timedelta(hours=9))
    now_str = datetime.datetime.now(kst).strftime("%Y-%m-%d %H:%M")

    if executed:
        title = f"🚨 Claude 봇 거래 발생! {len(executed)}건 체결"
    else:
        title = "🤖 Claude 봇: 이번엔 관망 (거래 없음)"
    log(title)
    log(body)
    send_push(title, body, bool(executed))

    # ── 사도될까 앱 연동용 상태 파일 저장 ──
    status = {
        "updated": now_str,
        "equity": float(account["equity"]),
        "cash": float(account["cash"]),
        "base": 100000.0,
        "positions": positions,
        "trades": results,
        "market_view": view,
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
                "vix": (regime or {}).get("vix"),
                "equity": round(float(account["equity"]), 2),
                "cash_pct": round(float(account["cash"]) / float(account["equity"]) * 100, 1) if float(account["equity"]) else 0,
                "trades": [
                    {"sym": d.get("symbol"), "action": d.get("action"),
                     "qty": d.get("qty"), "conviction": d.get("conviction", "normal"),
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
