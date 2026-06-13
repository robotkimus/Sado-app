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
MAX_POSITION_PCT = 0.05         # 한 종목 신규 매수 한도: 총자산의 5%
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


# ── 암호화폐 지원 ──
CRYPTO = {"BTC-USD", "ETH-USD"}          # 관심종목 표기 (야후 형식)


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
def ask_claude(account, positions, market):
    prompt = (
        "당신은 미국 주식 포트폴리오 매니저입니다. 모의계좌를 운용 중입니다.\n"
        "기술적 지표 기반의 스윙 전략을 따르되, 확신이 없으면 거래하지 않는 것이 원칙입니다.\n\n"
        "포트폴리오 구조 (중요):\n"
        "- 코어: M7(AAPL/MSFT/NVDA/GOOGL/AMZN/META/TSLA)과 기술 ETF(IGV/SOXX/SMH/QQQ)를 성장 축으로 운용\n"
        "- 분산: 금융·헬스케어·에너지·소비재·안전자산(GLD/TLT)에도 나눠 담아 한 섹터 쏠림을 피할 것\n"
        "- 레버리지(TQQQ/SOXL/UPRO/QLD): 상승 추세가 명확할 때만 단기 전술용으로. 레버리지+인버스 합산 평가액은 총자산의 15%를 절대 넘기지 말 것\n"
        "- 인버스(SQQQ/SOXS/SH/SDS): 지수가 20일선 아래로 꺾이는 등 하락 신호가 분명할 때 헤지용으로만. 같은 15% 한도 적용\n"
        "- 레버리지·인버스는 변동성 잠식이 있으니 보유가 길어지거나 근거가 사라지면 우선 정리 대상\n"
        "- RSI 과매도 + 볼린저 하단 같은 '싸게 살 기회' 역발상도 적극 활용\n- 암호화폐(BTC-USD/ETH-USD): 시험 운용 중. 변동성이 매우 크니 합산 평가액 총자산 5% 이내, 소수점 수량 사용 가능 (예: 0.02)\n\n"
        f"[계좌] 총자산 ${account['equity']}, 현금 ${account['cash']}\n"
        f"[보유 포지션]\n{json.dumps(positions, ensure_ascii=False, indent=1)}\n\n"
        f"[관심종목 지표]\n{json.dumps(market, ensure_ascii=False, indent=1)}\n\n"
        "규칙:\n"
        f"- 주문은 최대 {MAX_TRADES_PER_RUN}건, 신규 매수는 종목당 총자산의 {int(MAX_POSITION_PCT*100)}% 이내\n"
        "- 매수는 관심종목 내에서만, 매도는 보유 종목만, 공매도 금지\n"
        "- 손실 중인 포지션이 -7% 이하면 손절을 적극 검토 (레버리지는 -5%)\n"
        "- 거래할 이유가 약하면 빈 배열로 응답 (거래 안 함이 기본값)\n\n"
        "아래 JSON 형식으로만 응답하세요. 다른 텍스트, 마크다운 백틱 금지:\n"
        '{"decisions":[{"action":"buy|sell","symbol":"JPM","qty":3,"reason":"한 문장 근거"}],'
        '"market_view":"오늘 시장·포트폴리오 분산·레버리지 노출에 대한 한두 문장 평가"}'
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


def execute(decisions, account, positions, market):
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
        if qty <= 0 or sym not in prices:
            results.append(f"⛔ {sym} 건너뜀 (잘못된 주문)")
            continue
        cost = qty * prices[sym]
        if action == "buy":
            if cost > equity * MAX_POSITION_PCT:
                qty = round(equity * MAX_POSITION_PCT / prices[sym], 6 if crypto else 4)
                cost = qty * prices[sym]
            if qty <= 0 or cost > cash - equity * MIN_CASH_BUFFER_PCT:
                results.append(f"⛔ {sym} 매수 보류 (현금 한도)")
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
            order = {"symbol": to_alpaca_symbol(sym), "qty": str(qty), "side": side}
            if crypto:
                # 코인은 시장가 + gtc 로 즉시 체결 유도
                order.update({"type": "market", "time_in_force": "gtc"})
            else:
                order.update({"type": "market", "time_in_force": "day"})
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

    market = []
    for sym in watch:
        try:
            market.append(summarize(sym, fetch_daily(sym)))
        except Exception as e:
            log(f"⚠️ {sym} 시세 실패: {e}")
        time.sleep(0.4)  # 종목이 많아 데이터 소스 차단 방지용 간격
    log(f"✅ [3단계] 시세 확보 {len(market)}/{len(watch)} 종목")

    if not market:
        send_push("🤖 Claude 봇 — 실행 실패", "시세를 하나도 못 가져왔어요.", True)
        return 1

    try:
        plan = ask_claude(account, positions, market)
        log("✅ [4단계] Claude 판단 수신")
    except Exception as e:
        log(f"❌ [4단계] Claude 판단 실패: {e}")
        send_push("🤖 Claude 봇 — 판단 실패", f"Claude API 오류: {e}", True)
        return 1

    decisions = plan.get("decisions", [])
    view = plan.get("market_view", "")
    results = execute(decisions, account, positions, market) if decisions else []

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
    return 0


if __name__ == "__main__":
    sys.exit(main())
