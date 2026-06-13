# -*- coding: utf-8 -*-
"""계좌 정리용 일회성 스크립트.
   대기 중(미체결) 주문을 모두 취소하고, 보유 포지션이 있으면 전량 청산합니다.
   Actions에서 reset-account 워크플로로 수동 실행하세요."""
import json
import os
import sys
import urllib.request
import urllib.error

ALPACA_KEY = os.environ.get("ALPACA_API_KEY", "").strip()
ALPACA_SECRET = os.environ.get("ALPACA_SECRET_KEY", "").strip()
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
ALPACA_BASE = "https://paper-api.alpaca.markets"  # 모의계좌 전용
UA = {"User-Agent": "Mozilla/5.0"}


def alpaca(path, method="GET"):
    req = urllib.request.Request(ALPACA_BASE + path, method=method,
                                 headers={**UA,
                                          "APCA-API-KEY-ID": ALPACA_KEY,
                                          "APCA-API-SECRET-KEY": ALPACA_SECRET})
    with urllib.request.urlopen(req, timeout=30) as r:
        body = r.read().decode("utf-8", "replace")
        return json.loads(body) if body.strip() else {}


def push(msg):
    if not NTFY_TOPIC:
        return
    try:
        data = json.dumps({"topic": NTFY_TOPIC, "title": "🧹 계좌 정리 완료",
                           "message": msg, "tags": ["broom"]}).encode()
        urllib.request.urlopen(urllib.request.Request(
            "https://ntfy.sh/", data=data,
            headers={"Content-Type": "application/json"}), timeout=20)
    except Exception:
        pass


def main():
    if not (ALPACA_KEY and ALPACA_SECRET):
        print("❌ ALPACA 키 시크릿이 없습니다.")
        return 1

    # 1) 대기 중 주문 전체 취소
    try:
        orders = alpaca("/v2/orders?status=open")
        n_orders = len(orders)
        if n_orders:
            alpaca("/v2/orders", method="DELETE")  # 모든 열린 주문 취소
        print(f"🧾 대기 주문 {n_orders}건 취소")
    except Exception as e:
        print(f"⚠️ 주문 취소 중 오류: {e}")
        n_orders = "?"

    # 2) 보유 포지션 전량 청산
    try:
        positions = alpaca("/v2/positions")
        n_pos = len(positions)
        if n_pos:
            alpaca("/v2/positions", method="DELETE")  # 전 종목 청산
        print(f"📦 보유 포지션 {n_pos}건 청산")
    except Exception as e:
        print(f"⚠️ 포지션 청산 중 오류: {e}")
        n_pos = "?"

    # 3) 최종 잔액 확인
    try:
        acct = alpaca("/v2/account")
        msg = (f"주문 {n_orders}건 취소 · 포지션 {n_pos}건 청산\n"
               f"현금 ${float(acct['cash']):,.0f} · 총자산 ${float(acct['equity']):,.0f}")
    except Exception as e:
        msg = f"정리 완료 (잔액 조회 실패: {e})"

    print(msg)
    push(msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
