#!/usr/bin/env python3
"""포켓몬 카드 30주년 응모 당첨 메일 확인 봇.

Gmail을 IMAP으로 열어 포켓몬/당첨 관련 메일을 찾고, 새 메일이 있으면 디스코드로 알린다.
트레이딩 봇과 완전히 분리된 별도 스크립트. GitHub Actions가 매시간 실행한다.

환경변수(GitHub Secrets):
  GMAIL_ADDRESS     : 응모에 쓴 Gmail 주소 (예: me@gmail.com)
  GMAIL_APP_PASSWORD: Gmail '앱 비밀번호' 16자리 (일반 비번 아님)
  DISCORD_WEBHOOK   : 알림 보낼 디스코드 웹훅 URL (트레이딩 봇과 같은 걸 써도 됨)

이미 알린 메일은 seen_mail.json에 기록해 중복 알림을 막는다.
"""
import os
import re
import sys
import json
import imaplib
import email
from email.header import decode_header
import urllib.request

GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "").strip()
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "").strip()

SEEN_FILE = "seen_mail.json"

# ── 포켓몬/당첨 관련으로 볼 키워드 (제목·발신자에 하나라도 있으면 후보) ──
KEYWORDS = [
    # 포켓몬 (한국어·영어·일본어)
    "포켓몬", "포켓몬카드", "pokemon", "pokémon", "ポケモン", "ポケカ",
    "30주년", "30th", "30周年",
    # 당첨/추첨 (한국어)
    "당첨", "당첨자", "추첨", "응모", "이벤트 당첨", "축하", "선정", "발표",
    # 당첨/초대 (영어)
    "winner", "congratulations", "선정",
    # 아마존 재팬 초대 리퀘스트 (일본어) — 핵심
    "当選", "ご当選", "抽選", "招待", "招待者に選ばれました",
    "おめでとう", "ご応募", "当選通知", "招待リクエスト",
]
# 발신 도메인·이름으로도 거른다. 아마존 재팬은 @amazon.co.jp에서 보냄.
SENDER_HINTS = [
    "pokemon", "pokemonkorea", "포켓몬",
    "amazon.co.jp", "amazon.com", "アマゾン", "amazon",
]


def log(msg):
    print(msg, flush=True)


def _decode(s):
    """이메일 헤더(인코딩된 한글 제목 등)를 사람이 읽는 문자열로."""
    if not s:
        return ""
    parts = decode_header(s)
    out = []
    for txt, enc in parts:
        if isinstance(txt, bytes):
            try:
                out.append(txt.decode(enc or "utf-8", "replace"))
            except Exception:
                out.append(txt.decode("utf-8", "replace"))
        else:
            out.append(txt)
    return "".join(out)


def load_seen():
    try:
        with open(SEEN_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return set(data) if isinstance(data, list) else set()
    except Exception:
        return set()


def save_seen(seen):
    try:
        # 너무 커지지 않게 최근 500개만 보관
        with open(SEEN_FILE, "w", encoding="utf-8") as f:
            json.dump(list(seen)[-500:], f, ensure_ascii=False, indent=1)
    except Exception as e:
        log(f"⚠️ seen_mail.json 저장 실패: {e}")


def send_discord(title, desc, is_win):
    if not DISCORD_WEBHOOK:
        log("DISCORD_WEBHOOK 없음 — 알림 생략")
        return
    color = 0xF1C40F if is_win else 0x3498DB   # 당첨 추정=금색, 일반=파랑
    payload = json.dumps({
        "embeds": [{
            "title": title[:256],
            "description": desc[:4000],
            "color": color,
        }],
    }).encode("utf-8")
    try:
        req = urllib.request.Request(DISCORD_WEBHOOK, data=payload,
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=20)
        log("📨 디스코드 알림 전송")
    except Exception as e:
        log(f"⚠️ 디스코드 전송 실패: {e}")


def looks_like_win(subject):
    """제목에 당첨/초대 선정 류가 있으면 당첨 추정(금색 강조).
    아마존 재팬 초대 리퀘스트 당첨: '招待者に選ばれました', 'ご当選' 등."""
    win_words = [
        "당첨", "축하", "선정", "winner", "congratulation",
        "当選", "ご当選", "招待者に選ばれました", "おめでとう", "当選通知",
    ]
    s = subject.lower()
    return any(w in subject or w in s for w in win_words)


def is_relevant(subject, sender):
    """이 메일이 포켓몬 응모/당첨과 관련 있는지 판정.
    - 아마존 발신이면: 포켓몬 또는 당첨/초대 키워드가 함께 있을 때만 (광고·주문 메일 제외)
    - 그 외 발신이면: 포켓몬 키워드가 있으면 후보
    헛알림을 줄이기 위해 아마존 일반 메일은 거른다."""
    subj_l = subject.lower()
    sender_l = sender.lower()
    is_amazon = any(h in sender_l for h in ["amazon.co.jp", "amazon.com", "amazon", "アマゾン"])

    poke_words = ["포켓몬", "포켓몬카드", "pokemon", "pokémon", "ポケモン", "ポケカ",
                  "30주년", "30th", "30周年"]
    win_words = ["당첨", "당첨자", "추첨", "응모", "축하", "선정", "winner",
                 "congratulation", "当選", "ご当選", "抽選", "招待", "おめでとう",
                 "ご応募", "招待リクエスト", "当選通知"]
    has_poke = any(w in subject or w in subj_l for w in poke_words)
    has_win = any(w in subject or w in subj_l for w in win_words)

    if is_amazon:
        # 아마존 메일은 포켓몬 관련이거나 당첨/초대 관련일 때만 (일반 광고·배송 제외)
        return has_poke or has_win
    # 비(非)아마존: 포켓몬 키워드가 있으면 후보
    return has_poke


def main():
    if not (GMAIL_ADDRESS and GMAIL_APP_PASSWORD):
        log("❌ GMAIL_ADDRESS / GMAIL_APP_PASSWORD 환경변수가 없습니다.")
        return 1

    try:
        M = imaplib.IMAP4_SSL("imap.gmail.com")
        M.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
    except Exception as e:
        log(f"❌ Gmail 로그인 실패: {e} (앱 비밀번호·2단계 인증 확인)")
        return 1

    log("✅ Gmail 연결 성공")
    seen = load_seen()
    found = []

    # 받은편지함 + 스팸함 둘 다 검사 (아마존 당첨 메일이 스팸으로 자주 분류되므로)
    # 폴더명이 환경에 따라 다를 수 있어 후보를 순서대로 시도.
    mailboxes = ["INBOX", "[Gmail]/Spam", "[Gmail]/스팸함"]
    for box in mailboxes:
        try:
            typ, _ = M.select(box, readonly=True)   # readonly: 읽음 표시 안 바꿈
            if typ != "OK":
                continue
        except Exception:
            continue
        try:
            # 최근 메일 위주로 검색(전체를 다 뒤지면 느림). 최근 200개 UID만 본다.
            typ, data = M.uid("search", None, "ALL")
            if typ != "OK" or not data or not data[0]:
                continue
            uids = data[0].split()
            recent = uids[-200:] if len(uids) > 200 else uids

            for uid in recent:
                # seen 키에 폴더명을 붙여 INBOX/스팸함 UID 충돌 방지
                seen_key = f"{box}:{uid.decode()}"
                if seen_key in seen:
                    continue
                typ, msg_data = M.uid("fetch", uid, "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE)])")
                if typ != "OK" or not msg_data or not msg_data[0]:
                    continue
                raw = msg_data[0][1]
                msg = email.message_from_bytes(raw)
                subject = _decode(msg.get("Subject", ""))
                sender = _decode(msg.get("From", ""))
                date = msg.get("Date", "")

                if is_relevant(subject, sender):
                    in_spam = (box != "INBOX")
                    found.append({"subject": subject, "sender": sender,
                                  "date": date, "spam": in_spam})
                # 검색한 건 다 본 것으로 기록(다음엔 새 메일만 검사)
                seen.add(seen_key)
        except Exception as e:
            log(f"⚠️ {box} 검색 중 오류: {e}")

    try:
        M.logout()
    except Exception:
        pass

    # 알림
    if found:
        log(f"🎯 포켓몬 관련 메일 {len(found)}건 발견")
        for f in found:
            is_win = looks_like_win(f["subject"])
            spam_tag = " (⚠️스팸함)" if f.get("spam") else ""
            head = ("🎉 포켓몬 당첨 메일일 수 있어요!" if is_win
                    else "📬 포켓몬 관련 새 메일") + spam_tag
            spam_note = ("\n\n⚠️ 이 메일은 **스팸함**에 있어요. 진짜 당첨 메일이 "
                         "스팸으로 분류된 걸 수도 있으니 꼭 확인하세요!") if f.get("spam") else ""
            desc = (f"**제목:** {f['subject']}\n"
                    f"**보낸이:** {f['sender']}\n"
                    f"**날짜:** {f['date']}\n\n"
                    f"Gmail에서 직접 확인하세요." + spam_note)
            send_discord(head, desc, is_win)
    else:
        log("새 포켓몬 관련 메일 없음")

    save_seen(seen)
    return 0


if __name__ == "__main__":
    sys.exit(main())
