#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
신호 변화 알림 봇
- 관심종목(미국 + 국내)의 신호를 매번 계산
- 이전 상태(signal_state.json)와 비교
- 큰 변화(관망→매수, 매수→매도)일 때만 ntfy 푸시
- 일봉 기준이라 하루 2번(미국 마감 후·한국 마감 후) 실행 권장
"""
import os, json, time, urllib.request, urllib.parse

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
STATE_FILE = "signal_state.json"

# ── 관심종목 (여기에 알림 받을 종목을 적으세요) ──
# 미국: 티커 그대로 / 국내: 6자리코드.KS (코스피) 또는 .KQ (코스닥)
US_WATCH = ["AAPL", "NVDA", "TSLA", "MSFT", "AMZN", "GOOGL", "META"]
KR_WATCH = ["005930.KS", "000660.KS", "373220.KS", "207940.KS", "005380.KS"]
# 국내 종목 이름 (알림 표시용)
KR_NAMES = {
    "005930.KS": "삼성전자", "000660.KS": "SK하이닉스", "373220.KS": "LG엔솔",
    "207940.KS": "삼성바이오", "005380.KS": "현대차", "000270.KS": "기아",
    "068270.KS": "셀트리온",
}

def fetch_daily(symbol):
    """야후 파이낸스에서 일봉 1년치 가져오기"""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=1y&interval=1d"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.load(r)
        result = data["chart"]["result"][0]
        closes = result["indicators"]["quote"][0]["close"]
        vols = result["indicators"]["quote"][0]["volume"]
        rows = []
        for c, v in zip(closes, vols):
            if c is not None:
                rows.append({"close": c, "volume": v or 0})
        return rows if len(rows) >= 25 else None
    except Exception as e:
        print(f"  {symbol} 조회 실패: {e}")
        return None

def sma(arr, n, i):
    if i < n - 1:
        return None
    return sum(arr[i-n+1:i+1]) / n

def compute_signal(rows):
    """5신호 종합 점수 → buy/sell/hold"""
    closes = [r["close"] for r in rows]
    vols = [r["volume"] for r in rows]
    n = len(rows)
    # 이동평균
    ma5 = [sma(closes, 5, i) for i in range(n)]
    ma20 = [sma(closes, 20, i) for i in range(n)]
    # RSI
    gains, losses = 0, 0
    rsi = [None] * n
    for i in range(1, n):
        ch = closes[i] - closes[i-1]
        gain = max(ch, 0); loss = max(-ch, 0)
        if i <= 14:
            gains += gain / 14; losses += loss / 14
            if i == 14:
                rsi[i] = 100 - 100/(1 + (100 if losses == 0 else gains/losses))
        else:
            gains = (gains*13 + gain)/14; losses = (losses*13 + loss)/14
            rsi[i] = 100 - 100/(1 + (100 if losses == 0 else gains/losses))
    # 볼린저
    last = n - 1
    total = 0
    if ma20[last] is not None:
        total += 1 if closes[last] > ma20[last] else -1
    # 골든/데드크로스 (최근 7일)
    for i in range(max(1, n-7), n):
        if ma5[i-1] and ma20[i-1] and ma5[i] and ma20[i]:
            if ma5[i-1] <= ma20[i-1] and ma5[i] > ma20[i]:
                total += 1.5
            if ma5[i-1] >= ma20[i-1] and ma5[i] < ma20[i]:
                total -= 1.5
    if rsi[last] is not None:
        if rsi[last] <= 30: total += 1
        elif rsi[last] >= 70: total -= 1
    # 볼린저
    if ma20[last] is not None and last >= 19:
        sq = sum((closes[k]-ma20[last])**2 for k in range(last-19, last+1))
        sd = (sq/20) ** 0.5
        upper, lower = ma20[last]+2*sd, ma20[last]-2*sd
        if upper != lower:
            pos = (closes[last]-lower)/(upper-lower)
            if pos <= 0.15: total += 1
            elif pos >= 0.85: total -= 1
    # 거래량
    va = sma(vols, 20, last)
    if va and va > 0:
        ratio = vols[last]/va
        up = closes[last] >= closes[last-1]
        if ratio >= 1.2:
            total += 0.5 if up else -0.5
    if total >= 1.5: return "buy"
    if total <= -1.5: return "sell"
    return "hold"

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except:
        return {}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def push(title, msg, priority="default", tags=""):
    if not NTFY_TOPIC:
        print("  (NTFY_TOPIC 없음 — 알림 생략)")
        return
    try:
        data = msg.encode("utf-8")
        req = urllib.request.Request(
            f"https://ntfy.sh/{NTFY_TOPIC}", data=data,
            headers={"Title": title.encode("utf-8"), "Priority": priority, "Tags": tags})
        urllib.request.urlopen(req, timeout=10)
        print(f"  📲 알림 전송: {title}")
    except Exception as e:
        print(f"  알림 실패: {e}")

def main():
    state = load_state()
    new_state = {}
    alerts = []

    all_watch = [(s, s) for s in US_WATCH] + [(s, KR_NAMES.get(s, s)) for s in KR_WATCH]

    for symbol, name in all_watch:
        print(f"분석: {name} ({symbol})")
        rows = fetch_daily(symbol)
        if not rows:
            # 못 가져오면 이전 상태 유지
            if symbol in state:
                new_state[symbol] = state[symbol]
            continue
        sig = compute_signal(rows)
        prev = state.get(symbol)
        new_state[symbol] = sig
        print(f"  이전: {prev} → 현재: {sig}")

        # 큰 변화만: 관망→매수, 매수→매도
        if prev == "hold" and sig == "buy":
            alerts.append(("🔴 매수 신호 전환", f"{name} — 관망에서 매수 우위로 바뀌었어요"))
        elif prev == "buy" and sig == "sell":
            alerts.append(("🔵 매도 신호 전환", f"{name} — 매수에서 매도 우위로 바뀌었어요"))
        time.sleep(0.5)  # 야후 부하 방지

    save_state(new_state)

    # 알림 전송
    if alerts:
        if len(alerts) == 1:
            title, msg = alerts[0]
            push(title, msg, priority="high", tags="rotating_light")
        else:
            lines = [f"{t.split()[0]} {m}" for t, m in alerts]
            push(f"🚨 신호 변화 {len(alerts)}건",
                 "\n".join(lines), priority="high", tags="rotating_light")
    else:
        print("변화 없음 — 알림 생략")

if __name__ == "__main__":
    main()
