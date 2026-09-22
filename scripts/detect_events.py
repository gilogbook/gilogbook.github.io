# -*- coding: utf-8 -*-
"""
detect_events.py — 이벤트 감지 (v1 · 2026-09-22)

원본 위치는 저장소다.  C:\\gilogbook-repo\\gilogbook.github.io\\scripts\\detect_events.py
GitHub Actions(daily.yml)가 NAV 를 새로 기록한 실행(daily_nav.py 종료코드 0) 뒤에 실행한다.
OneDrive 에 사본을 두지 않는다.

무엇을 하나
  nav.csv 를 처음부터 끝까지 읽어 아래 두 규칙을 모든 행에 적용하고,
  events.csv 에 아직 없는 (date, type) 조합만 파일 끝에 덧붙인다.
  이미 있는 행은 절대 고치지도 지우지도 않는다 (절대 규칙 1).

    nav_move     전 기록일 대비 NAV 등락률의 절대값이 MOVE_PCT(5.0%) 이상인 날
                 value = 등락률(%). 예: -5.31
    excess_sign  개시 대비 누적 초과수익의 부호가 + ↔ − 로 바뀐 날
                 초과수익(%p) = (NAV/개시NAV − 1) − (코스피/개시코스피 − 1), 단위 %p
                 0 은 부호가 없다. 마지막으로 부호가 있었던 날과 비교한다
                 value = 바뀐 뒤의 부호. "+" 또는 "-"

  결측 확정 행(날짜만 있고 값이 전부 빈 행, 방법론 §7)은 건너뛴다. 그 다음 기록일의
  nav_move 는 직전 값이 없으므로 판정하지 않고, excess_sign 은 개시 대비이므로 그대로 판정한다.
  값이 일부만 빈 행은 결측 행이 아니라 손상된 행이다. 판정하지 않고 종료코드 1 로 멈춘다.

  같은 규칙을 매번 전체 행에 다시 적용하므로 실행 순서·횟수와 무관하게 결과가 같다.
  한 번 실패한 실행이 있어도 다음 실행이 빠진 이벤트를 채운다.
  규칙(임계값·벤치마크)을 바꾸면 과거 행에 새 이벤트가 생길 수 있다.
  그것은 규칙 변경이므로 새 인스턴스로 시작해야 한다 (절대 규칙 2).

이 스크립트가 감지하지 않는 것
  수집 실패        daily_nav.py 종료코드 1 → Actions 실패 → GitHub 알림 메일
  상장폐지·코드변경  daily_nav.py 의 하드 스톱(보유 종목 종가 없음) → 종료코드 1
  거래정지·관리종목  KRX 응답에서 빠지지 않아 신호가 없다 (2026-09-19 실측). 사람이 본다
  유상증자 등 권리락  신호 없음. 사람이 본다
  이런 사건은 사람이 events.csv 끝에 한 줄 덧붙인다. type 은 아래 중 하나
    delist · code_change · corp_action · missing · note
  이 스크립트는 그 행을 읽기만 하고 건드리지 않는다.

events.csv 열
  date         사건이 속한 NAV 기록일 (YYYY-MM-DD)
  type         nav_move · excess_sign · (수동) delist · code_change · corp_action · missing · note
  value        nav_move 는 등락률(%), excess_sign 은 바뀐 뒤 부호. 수동 행은 자유
  detail       사람이 읽는 한 줄 설명
  detected_at  이 행을 적은 시각 (KST, ISO 8601). 소급 기록이 아님을 증명한다

종료코드
  0  이벤트를 1건 이상 덧붙였다
  2  덧붙일 것이 없다 (정상)
  1  오류. events.csv 를 건드리지 않았다

실행
  python scripts/detect_events.py             저장소 경로는 GILOGBOOK_REPO 또는 기본값
  python scripts/detect_events.py --dry-run   판정만 출력하고 파일에 쓰지 않는다

환경변수
  GILOGBOOK_REPO   저장소 경로 (기본값 C:\\gilogbook-repo\\gilogbook.github.io)
  GITHUB_OUTPUT / GITHUB_STEP_SUMMARY   Actions 가 주면 요약을 적는다. 없으면 무시
"""

import csv
import os
import sys
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------- 설정

KST = timezone(timedelta(hours=9))

REPO = os.environ.get("GILOGBOOK_REPO", r"C:\gilogbook-repo\gilogbook.github.io")
NAV_CSV = os.path.join(REPO, "nav.csv")
EV_CSV = os.path.join(REPO, "events.csv")

MOVE_PCT = 5.0          # nav_move 임계값. 전 기록일 대비 NAV 등락률 절대값 (%)
BENCH_COL = "kospi"     # excess_sign 벤치마크 열. 주 벤치마크 = 코스피 (방법론 §5)
BENCH_NM = "코스피"
ZERO_DIGITS = 6         # 초과수익(%p)을 이 자리수로 반올림해 0 을 판정한다

VALUE_COLS = ("nav", "holdings_value", "cash", "kospi", "kosdaq")   # 전부 비면 결측 확정 행

EV_HEADER = ["date", "type", "value", "detail", "detected_at"]
AUTO_TYPES = ("nav_move", "excess_sign")


# ---------------------------------------------------------------- 유틸

def die(msg):
    print("[오류] " + msg)
    sys.exit(1)


def num(s, what, date):
    t = str(s if s is not None else "").replace(",", "").strip()
    if t == "":
        die("%s 행의 %s 값이 비어 있습니다. 결측 행은 판정하지 않고 중단합니다." % (date, what))
    try:
        return float(t)
    except ValueError:
        die("%s 행의 %s 값 %r 을 숫자로 읽지 못했습니다." % (date, what, s))


def is_placeholder(r):
    """결측 확정 행인가 — 값 열이 전부 비어 있으면 True. 일부만 비면 손상된 행으로 보고 멈춘다."""
    empty = [c for c in VALUE_COLS if str(r.get(c) or "").strip() == ""]
    if not empty:
        return False
    if len(empty) == len(VALUE_COLS):
        return True
    die("%s 행은 값 열 %d개 중 %d개만 비어 있습니다 (%s). 결측 행이면 값 열이 전부 비어야 합니다. "
        "손상된 행으로 보고 판정하지 않습니다." % (r.get("date"), len(VALUE_COLS), len(empty), ", ".join(empty)))


def sign_of(x):
    """+ / - / '' (0)"""
    r = round(x, ZERO_DIGITS)
    if r > 0:
        return "+"
    if r < 0:
        return "-"
    return ""


def fmt_won(v):
    return format(int(round(v)), ",")


# ---------------------------------------------------------------- 읽기

def load_nav():
    if not os.path.exists(NAV_CSV):
        die("nav.csv 가 없습니다: " + NAV_CSV)
    with open(NAV_CSV, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    if len(rows) < 1:
        die("nav.csv 에 행이 없습니다")
    rows.sort(key=lambda r: r["date"])
    seen = set()
    for r in rows:
        if r["date"] in seen:
            die("nav.csv 에 같은 날짜가 두 번 있습니다: " + r["date"])
        seen.add(r["date"])
    return rows


def load_events():
    """(기존 행 목록, (date,type) 집합). 파일이 없으면 빈 값."""
    if not os.path.exists(EV_CSV):
        return [], set()
    with open(EV_CSV, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    keys = set()
    for r in rows:
        if not r.get("date") or not r.get("type"):
            die("events.csv 에 date 또는 type 이 빈 행이 있습니다. 손으로 고친 뒤 다시 실행하세요.")
        keys.add((r["date"], r["type"]))
    return rows, keys


# ---------------------------------------------------------------- 판정

def evaluate(nav_rows):
    """nav.csv 전체에 규칙을 적용해 이벤트 후보 목록을 돌려준다. (date 순)"""
    base = nav_rows[0]
    if is_placeholder(base):
        die("개시 행(%s)이 결측 행입니다. 개시 행은 값이 있어야 합니다." % base["date"])
    nav0 = num(base["nav"], "nav", base["date"])
    b0 = num(base[BENCH_COL], BENCH_COL, base["date"])
    if nav0 <= 0 or b0 <= 0:
        die("개시 행의 nav 또는 %s 가 0 이하입니다" % BENCH_COL)

    found = []
    prev_nav = None
    last_sign = ""      # 마지막으로 부호가 있었던 날의 부호
    skipped = []
    for r in nav_rows:
        d = r["date"]
        if is_placeholder(r):
            skipped.append(d)
            prev_nav = None     # 다음 기록일은 직전 값이 없으므로 nav_move 를 판정하지 않는다
            continue
        nav = num(r["nav"], "nav", d)
        b = num(r[BENCH_COL], BENCH_COL, d)

        # 규칙 1 — 전 기록일 대비 NAV 등락률
        if prev_nav is not None:
            ret = (nav / prev_nav - 1) * 100
            if abs(ret) >= MOVE_PCT:
                found.append({
                    "date": d, "type": "nav_move", "value": "%.2f" % ret,
                    "detail": "NAV %s원 → %s원 (%+.2f%%) · 전 기록일 대비 · 기준 %s%%"
                              % (fmt_won(prev_nav), fmt_won(nav), ret, MOVE_PCT)})
        prev_nav = nav

        # 규칙 2 — 개시 대비 누적 초과수익 부호 전환
        nav_ret = (nav / nav0 - 1) * 100
        b_ret = (b / b0 - 1) * 100
        excess = nav_ret - b_ret
        s = sign_of(excess)
        if s and last_sign and s != last_sign:
            found.append({
                "date": d, "type": "excess_sign", "value": s,
                "detail": "개시 대비 초과수익 %+.2f%%p (NAV %+.2f%% · %s %+.2f%%) · 직전 부호 %s"
                          % (excess, nav_ret, BENCH_NM, b_ret, last_sign)})
        if s:
            last_sign = s
    if skipped:
        print("      결측 확정 행 %d건 건너뜀 — %s" % (len(skipped), ", ".join(skipped)))
    return found


# ---------------------------------------------------------------- 쓰기

def append_events(existing_rows, new_rows):
    """기존 행은 그대로 두고 새 행만 뒤에 덧붙인 파일을 통째로 다시 쓴다 (원자적 교체)."""
    tmp = EV_CSV + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=EV_HEADER, lineterminator="\n")
        w.writeheader()
        for r in existing_rows:
            w.writerow({k: r.get(k, "") for k in EV_HEADER})
        for r in new_rows:
            w.writerow({k: r.get(k, "") for k in EV_HEADER})
    os.replace(tmp, EV_CSV)


def write_actions_summary(new_rows, total_rows):
    """GitHub Actions 가 주는 파일이 있을 때만 요약을 적는다."""
    gho = os.environ.get("GITHUB_OUTPUT")
    if gho:
        msg = " / ".join("%s %s (%s)" % (r["type"], r["value"], r["date"]) for r in new_rows)
        with open(gho, "a", encoding="utf-8") as f:
            f.write("events=%d\n" % len(new_rows))
            f.write("event_msg=%s\n" % msg)
    ghs = os.environ.get("GITHUB_STEP_SUMMARY")
    if ghs:
        with open(ghs, "a", encoding="utf-8") as f:
            if new_rows:
                f.write("## 이벤트 %d건 — 글 발행 기준은 방법론 §8 「이벤트 기록」\n\n" % len(new_rows))
                f.write("| date | type | value | detail |\n|---|---|---|---|\n")
                for r in new_rows:
                    f.write("| %s | %s | %s | %s |\n" % (r["date"], r["type"], r["value"], r["detail"]))
            else:
                f.write("이벤트 없음 (events.csv 누적 %d행)\n" % total_rows)
    for r in new_rows:
        print("::notice title=이벤트 %s::%s %s — %s" % (r["date"], r["type"], r["value"], r["detail"]))


# ---------------------------------------------------------------- 본체

def main():
    dry = "--dry-run" in sys.argv[1:]

    nav_rows = load_nav()
    ev_rows, ev_keys = load_events()
    found = evaluate(nav_rows)

    now = datetime.now(KST).replace(microsecond=0).isoformat()
    new_rows = []
    for r in found:
        key = (r["date"], r["type"])
        if key in ev_keys:
            continue
        r["detected_at"] = now
        new_rows.append(r)

    print("[1/2] nav.csv %d행 · 개시 %s · 마지막 %s" % (len(nav_rows), nav_rows[0]["date"], nav_rows[-1]["date"]))
    print("      판정 결과 %d건 · 이미 기록됨 %d건 · 새 이벤트 %d건"
          % (len(found), len(found) - len(new_rows), len(new_rows)))
    for r in new_rows:
        print("    %s  %-12s %-6s %s" % (r["date"], r["type"], r["value"], r["detail"]))

    if dry:
        print("[2/2] --dry-run · 파일에 쓰지 않습니다")
        return 0 if new_rows else 2

    if new_rows or not os.path.exists(EV_CSV):
        append_events(ev_rows, new_rows)
        print("[2/2] events.csv 기록 · 누적 %d행" % (len(ev_rows) + len(new_rows)))
    else:
        print("[2/2] events.csv 변경 없음 · 누적 %d행" % len(ev_rows))

    write_actions_summary(new_rows, len(ev_rows) + len(new_rows))
    return 0 if new_rows else 2


if __name__ == "__main__":
    sys.exit(main())
