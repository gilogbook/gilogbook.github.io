# -*- coding: utf-8 -*-
"""
daily_nav.py — 일별 NAV 수집 (v2)

원본 위치는 저장소다.  C:\\gilogbook-repo\\gilogbook.github.io\\scripts\\daily_nav.py
GitHub Actions 가 같은 파일을 실행하므로 OneDrive 에 사본을 두지 않는다.

v1 과 달라진 점
  1. 기준일이 '전일 고정'이 아니라 '마지막 기록일+1 ~ 어제' 범위다.
     실행을 며칠 건너뛰어도 다음 실행이 빠진 날을 전부 채운다.
  2. 휴장일을 달력으로 판정하지 않는다. KRX 응답이 있으면 거래일, 없으면 아니다.
     응답이 없고 GRACE_DAYS 가 지나면 비거래일로 확정해 nontrading.json 에 적는다.
     법정공휴일·임시공휴일·대체공휴일·주말이 며칠 겹치든 판정 방식이 같다.
     임시공휴일이 갑자기 지정돼도 코드를 고칠 일이 없다.
  3. '데이터가 없다'와 '못 받았다'를 구분한다.
     HTTP 오류·타임아웃은 비거래일 판정으로 이어지지 않고 그 자리에서 중단한다.
  4. 빈 응답은 캐시에 저장하지 않는다. 저장하면 나중에 영원히 빈 값을 읽는다.

거래정지 종목에 대한 실측 (2026-09-19 · 20260907 응답 원본 대조)
  KRX 일별매매정보와 종목기본정보의 종목 집합이 완전히 같다.
    KOSPI  base 943  = quote 943   (차집합 양방향 0건)
    KOSDAQ base 1822 = quote 1822  (차집합 양방향 0건)
  종가가 없거나 0인 종목은 양 시장 모두 0건이다.
  거래량 0 종목이 KOSPI 32건 · KOSDAQ 87건 있는데 전부 종가가 있고 전일대비가 0이다.
  관리종목(KOSDAQ 129건)도 종가와 함께 들어 있다.
  → 거래정지·거래중지·관리종목·정리매매는 상장이 유지되는 동안 응답에서 빠지지 않는다.
     따라서 아래 compute() 의 하드 스톱은 상장폐지와 종목코드 변경에서만 발생한다.

종료코드
  0  1건 이상 NAV 를 기록했다
  2  기록할 것이 없다 (정상. 휴장 구간이거나 이미 다 기록됐다)
  1  오류. NAV 를 기록하지 않았다

실행
  단독      python daily_nav.py
  특정일    python daily_nav.py 20260918
  구간      python daily_nav.py 20260915 20260918
  API 점검  python daily_nav.py --probe 20260918
            조회만 하고 저장소에 아무것도 쓰지 않는다.
            인증키가 유효한지, 이 실행 환경에서 KRX API 에 닿는지만 본다.

환경변수
  KRX_API_KEY        필수
  GILOGBOOK_REPO     저장소 경로 (기본값 C:\\gilogbook-repo\\gilogbook.github.io)
  GILOGBOOK_CACHE    KRX 원본 캐시 경로 (기본값 C:\\gilogbook-cache\\krx)
"""

import csv
import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------- 설정

KST = timezone(timedelta(hours=9))

REPO = os.environ.get("GILOGBOOK_REPO", r"C:\gilogbook-repo\gilogbook.github.io")
CACHE = os.environ.get("GILOGBOOK_CACHE", r"C:\gilogbook-cache\krx")

NAV_CSV = os.path.join(REPO, "nav.csv")
NT_JSON = os.path.join(REPO, "nontrading.json")
HOLD_DIR = os.path.join(REPO, "holdings")

BASE = "https://data-dbg.krx.co.kr/svc/apis/"
QUOTE = {"KOSPI": "sto/stk_bydd_trd", "KOSDAQ": "sto/ksq_bydd_trd"}
#         캐시키          엔드포인트                응답의 IDX_NM 정확일치 값
INDEX = {"KOSPI": ("idx/kospi_dd_trd", "코스피"),
         "KOSDAQ": ("idx/kosdaq_dd_trd", "코스닥")}

GRACE_DAYS = 2      # 응답 없음 + 이 일수 경과 → 비거래일 확정
MAX_LOOKBACK = 40   # 한 번의 실행에서 훑는 최대 일수. 남으면 다음 실행이 이어간다
TIMEOUT = 60

NAV_HEADER = ["date", "nav", "holdings_value", "cash", "kospi", "kosdaq", "note"]


# ---------------------------------------------------------------- 유틸

def today_kst():
    return datetime.now(KST).date()


def d2s(d):
    return d.strftime("%Y%m%d")


def d2iso(d):
    return d.strftime("%Y-%m-%d")


def s2d(s):
    s = str(s).strip()
    if "-" in s:
        return datetime.strptime(s, "%Y-%m-%d").date()
    return datetime.strptime(s, "%Y%m%d").date()


def clean_num(s):
    """'7,190' -> '7190' / '6,627.26' -> '6627.26'. 값이 없으면 None"""
    if s is None:
        return None
    t = str(s).replace(",", "").strip()
    if t in ("", "-"):
        return None
    return t


def pick(obj, names, what):
    for n in names:
        if isinstance(obj, dict) and n in obj and obj[n] not in (None, ""):
            return obj[n]
    keys = list(obj.keys()) if isinstance(obj, dict) else type(obj).__name__
    raise KeyError("%s 에 해당하는 키를 찾지 못했습니다. 있는 키: %s" % (what, keys))


def die(msg):
    print("[오류] " + msg)
    sys.exit(1)


# ---------------------------------------------------------------- KRX

def krx_get(path, bas, cache_name):
    """(rows, 출처) 반환. HTTP·네트워크 오류는 예외로 올린다."""
    cpath = os.path.join(CACHE, cache_name)
    if os.path.exists(cpath):
        try:
            with open(cpath, encoding="utf-8") as f:
                data = json.load(f)
            return (data.get("OutBlock_1") or []), "cache"
        except Exception:
            os.remove(cpath)   # 깨진 캐시는 버리고 다시 받는다

    key = os.environ.get("KRX_API_KEY")
    if not key:
        raise RuntimeError("KRX_API_KEY 환경변수가 없습니다")

    url = BASE + path + "?basDd=" + bas
    req = urllib.request.Request(url, headers={"AUTH_KEY": key})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        body = r.read().decode("utf-8")
    data = json.loads(body)
    rows = data.get("OutBlock_1") or []

    # 빈 응답은 저장하지 않는다. 휴장 판정이 뒤집힐 수 있어야 한다
    if rows:
        os.makedirs(CACHE, exist_ok=True)
        tmp = cpath + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, cpath)
    return rows, "api"


def fetch_day(d):
    """
    (거래일인가, payload) 반환.
    4개 엔드포인트가 전부 비면 (False, 건수표).
    일부만 비면 이상 상황이므로 예외로 올린다 — 비거래일로 판정하지 않는다.
    """
    bas = d2s(d)
    payload, counts = {}, {}

    for mkt, path in QUOTE.items():
        rows, src = krx_get(path, bas, "%s_%s_quote.json" % (bas, mkt))
        payload["Q_" + mkt] = rows
        counts["Q_" + mkt] = len(rows)

    for mkt, (path, idxnm) in INDEX.items():
        rows, src = krx_get(path, bas, "%s_%s_index.json" % (bas, mkt))
        payload["I_" + mkt] = rows
        counts["I_" + mkt] = len(rows)

    total = sum(counts.values())
    if total == 0:
        return False, counts

    empty = sorted(k for k, v in counts.items() if v == 0)
    if empty:
        raise RuntimeError(
            "%s 일부 엔드포인트만 비어 있습니다 %s. 비거래일 판정을 하지 않고 중단합니다."
            % (bas, counts))
    return True, payload


# ---------------------------------------------------------------- 보유

def load_holdings(target):
    """target 이전(또는 같은 날)의 가장 최근 holdings/*.json 을 읽는다."""
    if not os.path.isdir(HOLD_DIR):
        die("holdings 폴더가 없습니다: " + HOLD_DIR)
    cands = []
    for fn in os.listdir(HOLD_DIR):
        if not fn.endswith(".json"):
            continue
        stem = fn[:-5]
        try:
            hd = s2d(stem)
        except ValueError:
            continue
        if hd <= target:
            cands.append((hd, fn))
    if not cands:
        die("%s 이전의 holdings 파일이 없습니다" % d2iso(target))
    cands.sort()
    hd, fn = cands[-1]
    with open(os.path.join(HOLD_DIR, fn), encoding="utf-8") as f:
        raw = json.load(f)

    items = pick(raw, ["holdings", "items", "stocks", "positions", "종목"], "보유 목록")
    holds = []
    for it in items:
        code = str(pick(it, ["code", "종목코드", "isu_cd", "ISU_CD", "ticker"], "종목코드")).strip()
        shares = int(float(str(pick(it, ["shares", "주수", "qty", "quantity", "수량"], "주수")).replace(",", "")))
        nm = ""
        for k in ("name", "종목명", "ISU_NM"):
            if isinstance(it, dict) and it.get(k):
                nm = str(it[k])
                break
        holds.append({"code": code.zfill(6), "name": nm, "shares": shares})

    cash = None
    for k in ("cash", "현금", "잔액", "cash_won"):
        if k in raw and raw[k] not in (None, ""):
            cash = int(float(str(raw[k]).replace(",", "")))
            break
    return fn, holds, cash


# ---------------------------------------------------------------- nav.csv

def load_nav():
    if not os.path.exists(NAV_CSV):
        die("nav.csv 가 없습니다: " + NAV_CSV)
    with open(NAV_CSV, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        die("nav.csv 에 행이 없습니다. 개시일 확정(16_fill_holdings.py)이 먼저입니다.")
    return rows


def save_nav(rows):
    rows.sort(key=lambda r: r["date"])
    tmp = NAV_CSV + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=NAV_HEADER, lineterminator="\n")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in NAV_HEADER})
    os.replace(tmp, NAV_CSV)


# ---------------------------------------------------------------- nontrading.json

NT_NOTE = ("KRX Open API 응답이 없어 거래일이 아닌 것으로 판정한 날짜다. "
           "공식 휴장일 목록이 아니라 응답 부재로 내린 판정이므로, "
           "주말·법정공휴일·임시공휴일·대체공휴일이 구분 없이 함께 들어간다. "
           "이 파일에 있는 날짜는 다시 조회하지 않는다.")


def load_nt():
    if not os.path.exists(NT_JSON):
        return {"_주석": NT_NOTE,
                "criterion": "KRX 4개 엔드포인트 전부 빈 응답 + %d일 이상 경과" % GRACE_DAYS,
                "days": {}}
    with open(NT_JSON, encoding="utf-8") as f:
        d = json.load(f)
    d.setdefault("days", {})
    return d


def save_nt(d):
    d["_주석"] = NT_NOTE
    d["criterion"] = "KRX 4개 엔드포인트 전부 빈 응답 + %d일 이상 경과" % GRACE_DAYS
    d["days"] = dict(sorted(d["days"].items()))
    tmp = NT_JSON + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, NT_JSON)


# ---------------------------------------------------------------- 계산

def compute(d, payload, holds, cash):
    px = {}
    for mkt in ("KOSPI", "KOSDAQ"):
        for r in payload["Q_" + mkt]:
            code = str(r.get("ISU_CD") or r.get("ISU_SRT_CD") or "").strip().zfill(6)
            v = clean_num(r.get("TDD_CLSPRC"))
            if code and v is not None:
                px[code] = int(float(v))

    missing = [h for h in holds if h["code"] not in px]
    if missing:
        raise RuntimeError(
            "%s 종가를 찾지 못한 보유 종목 %d개 — %s\n"
            "  거래정지·관리종목·정리매매는 응답에서 빠지지 않는다(실측 확인). "
            "따라서 상장폐지 또는 종목코드 변경일 가능성이 높다.\n"
            "  확인 방법 — KRX 종목기본정보(stk_isu_base_info / ksq_isu_base_info)에 "
            "해당 코드가 남아 있는지 본다. 없으면 상장폐지, 다른 코드로 있으면 코드 변경이다.\n"
            "  포트폴리오 구성이 바뀌는 사건이므로 사람이 판단해야 한다. 아무것도 기록하지 않는다."
            % (d2iso(d), len(missing),
               ", ".join("%s %s" % (h["code"], h["name"]) for h in missing[:10])))

    hv = sum(h["shares"] * px[h["code"]] for h in holds)

    idx = {}
    for mkt, (path, idxnm) in INDEX.items():
        hit = None
        for r in payload["I_" + mkt]:
            if str(r.get("IDX_NM", "")).strip() == idxnm:
                hit = clean_num(r.get("CLSPRC_IDX"))
                break
        if hit is None:
            raise RuntimeError("%s 지수 '%s' 를 응답에서 찾지 못했습니다" % (d2iso(d), idxnm))
        idx[mkt] = hit

    return {"date": d2iso(d),
            "nav": str(hv + cash),
            "holdings_value": str(hv),
            "cash": str(cash),
            "kospi": idx["KOSPI"],
            "kosdaq": idx["KOSDAQ"],
            "note": ""}


# ---------------------------------------------------------------- 점검

def probe(bas):
    """
    API 점검 전용. 조회만 하고 저장소에 아무것도 쓰지 않는다.
    nav.csv·holdings·nontrading.json 을 읽지도 않는다.
    """
    print("[점검] %s — KRX 4개 엔드포인트를 조회만 한다. 기록하지 않는다." % bas)
    total = 0
    try:
        for mkt, path in QUOTE.items():
            rows, src = krx_get(path, bas, "%s_%s_quote.json" % (bas, mkt))
            print("   %-6s 시세 %5d건   출처 %s" % (mkt, len(rows), src))
            total += len(rows)
        for mkt, (path, idxnm) in INDEX.items():
            rows, src = krx_get(path, bas, "%s_%s_index.json" % (bas, mkt))
            hit = [r for r in rows if str(r.get("IDX_NM", "")).strip() == idxnm]
            val = clean_num(hit[0].get("CLSPRC_IDX")) if hit else None
            print("   %-6s 지수 %5d건   '%s' %s   출처 %s"
                  % (mkt, len(rows), idxnm, val if val else "찾지 못함", src))
            total += len(rows)
    except urllib.error.HTTPError as e:
        die("KRX HTTP %s. 인증키가 틀렸거나 만료됐거나, 이 IP 에서 차단됐을 수 있습니다." % e.code)
    except urllib.error.URLError as e:
        die("KRX 연결 실패 (%s). 이 실행 환경에서 KRX API 에 닿지 못합니다." % e.reason)
    except RuntimeError as e:
        die(str(e))

    if total == 0:
        print("[점검] 응답이 전부 비었습니다. 이 날짜는 거래일이 아니거나 아직 게시되지 않았습니다.")
        print("       인증키 문제는 아닙니다. 키가 틀리면 HTTP 오류가 납니다.")
        return 2
    print("[점검] 정상. 인증키가 유효하고 이 실행 환경에서 KRX API 에 닿습니다.")
    return 0


# ---------------------------------------------------------------- 본체

def main():
    args = [a for a in sys.argv[1:] if a.strip()]
    today = today_kst()

    if args and args[0] == "--probe":
        if len(args) != 2:
            die("--probe 는 날짜 하나를 받습니다. 예: python daily_nav.py --probe 20260918")
        return probe(d2s(s2d(args[1])))

    nav_rows = load_nav()
    have = set(r["date"] for r in nav_rows)
    nt = load_nt()

    if len(args) == 1:
        start = end = s2d(args[0])
    elif len(args) == 2:
        start, end = s2d(args[0]), s2d(args[1])
    elif len(args) == 0:
        last = max(s2d(x) for x in have)
        start = last + timedelta(days=1)
        end = today - timedelta(days=1)
    else:
        die("인자는 0개·1개·2개만 받습니다")

    if start > end:
        print("[알림] 기록할 구간이 없습니다. 마지막 기록일 %s · 오늘 %s"
              % (max(sorted(have)), d2iso(today)))
        return 2

    span = (end - start).days + 1
    if span > MAX_LOOKBACK:
        end = start + timedelta(days=MAX_LOOKBACK - 1)
        print("[주의] 빈 구간이 %d일입니다. 이번 실행은 %s~%s 만 처리하고 나머지는 다음 실행이 이어갑니다."
              % (span, d2iso(start), d2iso(end)))

    fn, holds, cash_h = load_holdings(end)
    cash = cash_h if cash_h is not None else int(float(nav_rows[-1]["cash"]))
    print("[1/3] 보유 %s — %d종목 · 현금 %s원" % (fn, len(holds), format(cash, ",")))
    print("[2/3] 훑는 구간 %s ~ %s (%d일)" % (d2iso(start), d2iso(end), (end - start).days + 1))

    recorded, confirmed, pending, already = [], [], [], []
    d = start
    while d <= end:
        ds, iso = d2s(d), d2iso(d)
        if iso in have:
            already.append(iso)
            d += timedelta(days=1)
            continue
        if ds in nt["days"]:
            d += timedelta(days=1)
            continue

        try:
            ok, payload = fetch_day(d)
        except urllib.error.HTTPError as e:
            die("%s KRX HTTP %s — 못 받은 것과 없는 것은 다릅니다. 판정하지 않고 중단합니다." % (iso, e.code))
        except urllib.error.URLError as e:
            die("%s KRX 연결 실패 (%s). 판정하지 않고 중단합니다." % (iso, e.reason))
        except RuntimeError as e:
            die(str(e))

        if not ok:
            age = (today - d).days
            if age >= GRACE_DAYS:
                nt["days"][ds] = {"confirmed": d2s(today)}
                confirmed.append(iso)
                print("    %s  비거래일 확정 — 응답 없음 · %d일 경과" % (iso, age))
            else:
                pending.append(iso)
                print("    %s  응답 없음 — %d일 더 기다립니다 (게시 지연과 구분)"
                      % (iso, GRACE_DAYS - age))
            d += timedelta(days=1)
            continue

        try:
            row = compute(d, payload, holds, cash)
        except RuntimeError as e:
            die(str(e))

        nav_rows.append(row)
        have.add(iso)
        recorded.append(row)
        print("    %s  NAV %s  (코스피 %s · 코스닥 %s)"
              % (iso, format(int(row["nav"]), ","), row["kospi"], row["kosdaq"]))
        d += timedelta(days=1)

    if recorded:
        save_nav(nav_rows)
    if confirmed:
        save_nt(nt)

    print("[3/3] 기록 %d건 · 비거래일 확정 %d건 · 대기 %d건 · 이미 있음 %d건"
          % (len(recorded), len(confirmed), len(pending), len(already)))

    if recorded:
        base = int(nav_rows[0]["nav"])
        last = recorded[-1]
        print("")
        print("=" * 62)
        print(" %s  NAV %s원   개시 대비 %+.2f%%"
              % (last["date"], format(int(last["nav"]), ","),
                 (int(last["nav"]) / base - 1) * 100))
        print("=" * 62)

    gho = os.environ.get("GITHUB_OUTPUT")
    if gho:
        with open(gho, "a", encoding="utf-8") as f:
            f.write("recorded=%s\n" % ",".join(r["date"] for r in recorded))
            f.write("nontrading=%s\n" % ",".join(confirmed))
            f.write("count=%d\n" % len(recorded))

    return 0 if recorded else 2


if __name__ == "__main__":
    sys.exit(main())
