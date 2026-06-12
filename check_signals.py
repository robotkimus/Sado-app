# -*- coding: utf-8 -*-
"""매일 미국 장 마감 후 관심종목의 매매 신호를 점검하고 폰으로 푸시를 보냅니다.
   앱(index.html)과 동일한 5가지 신호 로직을 사용합니다.
   데이터: Stooq 일별 시세 / 알림: ntfy.sh"""
import csv
import io
import json
import os
import sys
import urllib.parse
import urllib.request

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}


def _get(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def fetch_stooq(sym):
    text = _get(f"https://stooq.com/q/d/l/?s={sym.lower()}.us&i=d")
    rows = []
    for row in csv.DictReader(io.StringIO(text)):
        try:
            rows.append({
                "date": row["Date"],
                "close": float(row["Close"]),
                "volume": float(row.get("Volume") or 0),
            })
        except (KeyError, ValueError):
            continue
    return rows


def fetch_yahoo(sym):
    text = _get(f"https://query1.finance.yahoo.com/v8/finance/chart/"
                f"{urllib.parse.quote(sym)}?range=1y&interval=1d")
    j = json.loads(text)
    result = j["chart"]["result"][0]
    ts = result["timestamp"]
    q = result["indicators"]["quote"][0]
    rows = []
    for i, t in enumerate(ts):
        c, v = q["close"][i], q["volume"][i]
        if c is None:
            continue
        import datetime as _dt
        d = _dt.datetime.utcfromtimestamp(t).strftime("%Y-%m-%d")
        rows.append({"date": d, "close": float(c), "volume": float(v or 0)})
    return rows


def fetch_daily(sym):
    errors = []
    for fn in (fetch_stooq, fetch_yahoo):
        try:
            rows = fn(sym)
            if len(rows) >= 25:
                rows.sort(key=lambda r: r["date"])
                return rows[-160:]
            errors.append(f"{fn.__name__}: 데이터 부족")
        except Exception as e:
            errors.append(f"{fn.__name__}: {e}")
    raise RuntimeError(" / ".join(errors))


def sma(vals, n, i):
    if i < n - 1:
        return None
    return sum(vals[i - n + 1:i + 1]) / n


def compute(series):
    closes = [r["close"] for r in series]
    vols = [r["volume"] for r in series]
    rows = []
    for i, r in enumerate(series):
        ma5 = sma(closes, 5, i)
        ma20 = sma(closes, 20, i)
        upper = lower = None
        if i >= 19 and ma20 is not None:
            var = sum((closes[k] - ma20) ** 2 for k in range(i - 19, i + 1)) / 20
            sd = var ** 0.5
            upper, lower = ma20 + 2 * sd, ma20 - 2 * sd
        rows.append({**r, "ma5": ma5, "ma20": ma20, "upper": upper,
                     "lower": lower, "vol_avg20": sma(vols, 20, i), "rsi": None})
    g = l = 0.0
    for i in range(1, len(rows)):
        ch = closes[i] - closes[i - 1]
        gain, loss = max(ch, 0), max(-ch, 0)
        if i <= 14:
            g += gain / 14
            l += loss / 14
            if i == 14:
                rows[i]["rsi"] = 100 - 100 / (1 + (100 if l == 0 else g / l))
        else:
            g = (g * 13 + gain) / 14
            l = (l * 13 + loss) / 14
            rows[i]["rsi"] = 100 - 100 / (1 + (100 if l == 0 else g / l))
    return rows


def judge(rows):
    last = rows[-1]
    score = 0.0
    reasons = []
    if last["ma20"]:
        above = last["close"] > last["ma20"]
        score += 1 if above else -1
        reasons.append("20일선 위" if above else "20일선 아래")
    cross = None
    for i in range(max(1, len(rows) - 7), len(rows)):
        p, c = rows[i - 1], rows[i]
        if not (p["ma5"] and p["ma20"] and c["ma5"] and c["ma20"]):
            continue
        if p["ma5"] <= p["ma20"] and c["ma5"] > c["ma20"]:
            cross = "골든크로스"
        if p["ma5"] >= p["ma20"] and c["ma5"] < c["ma20"]:
            cross = "데드크로스"
    if cross == "골든크로스":
        score += 1.5
        reasons.append("골든크로스!")
    elif cross == "데드크로스":
        score -= 1.5
        reasons.append("데드크로스")
    r = last["rsi"]
    if r is not None:
        if r <= 30:
            score += 1
            reasons.append(f"RSI {r:.0f} 과매도")
        elif r >= 70:
            score -= 1
            reasons.append(f"RSI {r:.0f} 과열")
    if last["upper"] and last["lower"]:
        pos = (last["close"] - last["lower"]) / (last["upper"] - last["lower"])
        if pos <= 0.15:
            score += 1
            reasons.append("볼린저 하단")
        elif pos >= 0.85:
            score -= 1
            reasons.append("볼린저 상단")
    if last["vol_avg20"]:
        ratio = last["volume"] / last["vol_avg20"]
        up = last["close"] >= rows[-2]["close"] if len(rows) > 1 else True
        if ratio >= 1.2:
            score += 0.5 if up else -0.5
            reasons.append("거래량 동반" if up else "매물 출회")
    verdict = "매수" if score >= 1.5 else ("매도" if score <= -1.5 else "관망")
    return verdict, score, reasons, last


def send_push(title, message, urgent):
    if not NTFY_TOPIC:
        print("NTFY_TOPIC 시크릿이 없어 알림을 건너뜁니다.")
        return
    payload = json.dumps({
        "topic": NTFY_TOPIC,
        "title": title,
        "message": message,
        "priority": 5 if urgent else 3,
        "tags": ["rotating_light"] if urgent else ["bar_chart"],
    }).encode("utf-8")
    req = urllib.request.Request("https://ntfy.sh/", data=payload,
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=20)


def main():
    with open("watchlist.txt", encoding="utf-8") as f:
        watch = [t.strip().upper() for t in f
                 if t.strip() and not t.strip().startswith("#")]
    lines, hot = [], []
    for sym in watch:
        try:
            series = fetch_daily(sym)
            if len(series) < 25:
                lines.append(f"{sym}: 데이터 부족")
                continue
            verdict, score, reasons, last = judge(compute(series))
            mark = {"매수": "▲", "매도": "▼", "관망": "—"}[verdict]
            line = f"{mark} {sym} {verdict} ({score:+.1f}) ${last['close']:.2f} · " + ", ".join(reasons[:3])
            lines.append(line)
            if verdict != "관망":
                hot.append(f"{sym} {verdict}")
        except Exception as e:  # 한 종목 실패가 전체를 막지 않도록
            lines.append(f"{sym}: 조회 실패 ({e})")
    urgent = bool(hot)
    title = "🚨 지금 볼 종목: " + ", ".join(hot) if urgent else "오늘 신호 요약 — 전 종목 관망"
    body = "\n".join(lines) + "\n\n※ 기계적 신호 참고용 · 투자 판단은 본인 몫"
    print(title)
    print(body)
    send_push(title, body, urgent)


if __name__ == "__main__":
    sys.exit(main())
