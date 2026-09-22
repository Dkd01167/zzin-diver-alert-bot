"""주식 토큰 종목의 찐다이버전스를 '본주 차트'(Yahoo Finance 데이터) 기준으로 감지해서 텔레그램으로 보낸다.
- 대상: stock_map.json 에 매핑된 Bitget 주식/ETF 토큰 (본주 없는 종목은 기존 Bitget 기준 알림 유지)
- 타임프레임: 15분, 30분, 1시간, 일봉, 주봉 (본주 데이터에 4시간/6시간봉 없음)
- 정규장 시간만 사용한다(프리/애프터마켓 제외). Yahoo가 확장시간 거래량을 항상 0으로 주는 등
  정규장 밖 데이터 신뢰도가 낮아, 트레이딩뷰(정규장)와 어긋나는 신호가 나온 사례(2026-09-22, DDOG)가
  확인돼서 뺐다. 정규장이 아닌 시간의 신호는 zzin_diver_alert.py 가 Bitget 자체 가격으로 대신 보낸다
  (in_regular_hours 로 두 알림봇이 겹치지 않게 시간대를 나눔).
- 판정 로직(RSI 14 Wilder, 70/30, 50선 초기화, 에피소드 극값)은 zzin_diver_alert.py 와 동일
"""
import json
import os
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import zzin_diver_alert as za

HERE = os.path.dirname(os.path.abspath(__file__))
MAP_PATH = os.path.join(HERE, "stock_map.json")
STATE_PATH = os.path.join(HERE, "zzin_stock_state.json")

STOCK_TFS = [("15분봉", "15m"), ("30분봉", "30m"), ("1시간봉", "1H"), ("일봉", "1D"), ("주봉", "1W")]
YF_INTERVAL = {"15m": "15m", "30m": "30m", "1H": "60m", "1D": "1d", "1W": "1wk"}
BAR_MS = {"15m": 900_000, "30m": 1_800_000, "1H": 3_600_000, "1D": 86_400_000, "1W": 604_800_000}
SEED_RANGE = {"15m": "30d", "30m": "60d", "1H": "180d", "1D": "2y", "1W": "10y"}
INC_RANGE = {"15m": "5d", "30m": "5d", "1H": "5d", "1D": "3mo", "1W": "1y"}
BACKOFF_MS = {"15m": 10 * 60_000, "30m": 20 * 60_000, "1H": 40 * 60_000, "1D": 3 * 3_600_000, "1W": 6 * 3_600_000}
US_EXCH = {"NMS", "NYQ", "NGM", "PCX", "NCM", "BTS", "ASE", "NYS"}
HEADERS = {"User-Agent": "Mozilla/5.0"}

# 거래소별 정규장 시간(현지시간, 월~금). 공휴일은 반영 안 함(그날은 Bitget 쪽이 대신 알림을 보냄 -> 무해).
EXCH_SESSION = {
    "US": (ZoneInfo("America/New_York"), (9, 30), (16, 0)),
    "KSC": (ZoneInfo("Asia/Seoul"), (9, 0), (15, 30)),
    "JPX": (ZoneInfo("Asia/Tokyo"), (9, 0), (15, 0)),
    "HKG": (ZoneInfo("Asia/Hong_Kong"), (9, 30), (16, 0)),
    "SHH": (ZoneInfo("Asia/Shanghai"), (9, 30), (15, 0)),
}


def in_regular_hours(exch, now_ms=None):
    """이 거래소가 지금 정규장 시간(월~금, 장중)인지. 모르는 거래소는 보수적으로 False(정규장 아님)."""
    key = "US" if exch in US_EXCH else exch
    sess = EXCH_SESSION.get(key)
    if not sess:
        return False
    tz, (sh, sm), (eh, em) = sess
    now = datetime.fromtimestamp((now_ms or int(time.time() * 1000)) / 1000, tz)
    if now.weekday() >= 5:
        return False
    t = now.hour * 60 + now.minute
    return sh * 60 + sm <= t < eh * 60 + em


def load_map():
    try:
        with open(MAP_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def mapped_symbols():
    return set(load_map().keys())


def load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_state(state):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    os.replace(tmp, STATE_PATH)


def yahoo_get(ticker, gran, rng, ext):
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/%s?range=%s&interval=%s&includePrePost=%s"
           % (urllib.parse.quote(ticker), rng, YF_INTERVAL[gran], "true" if ext else "false"))
    for attempt in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=25) as r:
                d = json.loads(r.read().decode())
            res = d["chart"]["result"]
            return res[0] if res else None
        except Exception as e:
            if "404" in str(e):
                return None
            time.sleep(2 * (attempt + 1) if "429" in str(e) else 1)
    return None


def merge_closing_auction(bars, meta):
    """정규장 종료 시각 이후에 시작하는 봉(동시호가 단일 체결 봉)은 앞 봉에 합친다(트레이딩뷰와 같은 방식)."""
    try:
        reg = meta["currentTradingPeriod"]["regular"]
        off = int(meta.get("gmtoffset") or 0)
        close_tod = (int(reg["end"]) + off) % 86400
    except Exception:
        return bars
    out = []
    for b in bars:
        tod = ((b[0] // 1000) + off) % 86400
        single_print = b[1] == b[2] == b[3] == b[4]   # 동시호가는 가격 하나로만 체결되는 봉
        # Yahoo 의 regular 종료 시각에는 동시호가 구간(약 10분)이 들어 있어서 15분 여유를 둔다
        if out and single_print and tod >= close_tod - 900 and (b[0] - out[-1][0]) < 86_400_000:
            p = out[-1]
            out[-1] = [p[0], p[1], max(p[2], b[2]), min(p[3], b[3]), b[4]]
        else:
            out.append(list(b))
    return out


def fetch_bars(ticker, gran, exch, seed, now_ms):
    """완성된 봉만 [시작ms, o, h, l, c] 로 돌려준다. 정규장만 쓴다(프리/애프터마켓 제외 — 위 모듈 설명 참고)."""
    res = yahoo_get(ticker, gran, (SEED_RANGE if seed else INC_RANGE)[gran], False)
    if not res or not res.get("timestamp"):
        return []
    q = res["indicators"]["quote"][0]
    bars = [[int(t) * 1000, o, h, l, c] for t, o, h, l, c in zip(res["timestamp"], q["open"], q["high"], q["low"], q["close"])
            if None not in (o, h, l, c)]
    meta = res.get("meta", {})
    if gran in ("15m", "30m", "1H"):
        bars = merge_closing_auction(bars, meta)
    # 진행 중인 마지막 봉 제외
    done = []
    reg_end = None
    try:
        reg_end = int(meta["currentTradingPeriod"]["regular"]["end"]) * 1000
    except Exception:
        pass
    for i, b in enumerate(bars):
        last = i == len(bars) - 1
        if not last:
            done.append(b)
            continue
        if gran in ("15m", "30m", "1H"):
            complete = now_ms >= b[0] + BAR_MS[gran]
        elif gran == "1D":
            complete = now_ms >= b[0] + 7 * 3_600_000   # 일봉 시작(장 시작) + 7시간이면 어느 거래소든 장 마감 후
        else:
            complete = now_ms >= b[0] + 5 * 86_400_000
        if complete:
            done.append(b)
    return done


def run_key(sym, info, tf_name, gran, state, now_ms, alerts):
    key = "%s|%s" % (sym, gran)
    st_entry = state.get(key)
    if st_entry and now_ms < st_entry.get("next_ms", 0):
        return
    ctx = {"symbol": sym, "base_coin": info["base"], "label": info["label"], "tf_name": tf_name, "gran": gran,
           "yahoo": info["yahoo"], "exch": info["exch"], "cur": info["cur"]}
    bars = fetch_bars(info["yahoo"], gran, info["exch"], st_entry is None, now_ms)
    if st_entry is None:
        if len(bars) <= za.PERIOD + 1:
            state[key] = {"next_ms": now_ms + 6 * 3_600_000, "empty": True}
            return
        hist = za.seed_rsi([b[4] for b in bars])
        st = za.fresh_ep_state()
        pc = bars[hist[0][0] - 1][4]
        pag = pal = None
        for idx, ag, al, rsi in hist:
            b = bars[idx]
            za.process_bar(st, b[0], b[2], b[3], b[4], rsi, pc, pag, pal, False, [], ctx)
            pc = b[4]
            pag, pal = ag, al
        li, lag, lal, _ = hist[-1]
        state[key] = {**st, "last_ts": bars[li][0], "avg_gain": lag, "avg_loss": lal, "last_close": bars[li][4],
                      "next_ms": now_ms + BACKOFF_MS[gran]}
        return
    if st_entry.get("empty"):
        del state[key]
        return
    new = [b for b in bars if b[0] > st_entry["last_ts"]]
    if not new:
        st_entry["next_ms"] = now_ms + BACKOFF_MS[gran]
        return
    st = {k: v for k, v in st_entry.items() if k not in ("last_ts", "avg_gain", "avg_loss", "last_close", "next_ms")}
    ag, al, pc = st_entry["avg_gain"], st_entry["avg_loss"], st_entry["last_close"]
    found = []
    for b in new:
        ag, al, rsi = za.rsi_step(ag, al, pc, b[4])
        if rsi is None:
            pc = b[4]
            continue
        fresh = (now_ms - b[0]) <= max(3 * BAR_MS[gran], 1_800_000)
        za.process_bar(st, b[0], b[2], b[3], b[4], rsi, pc, ag, al, fresh, found, ctx)
        pc = b[4]
    state[key] = {**st, "last_ts": new[-1][0], "avg_gain": ag, "avg_loss": al, "last_close": pc,
                  "next_ms": now_ms + BACKOFF_MS[gran]}
    alerts.extend(found)


def format_message(a):
    arrow = "숏 🔻" if a["dir"] == "short" else "롱 🔺"
    kst = datetime.fromtimestamp(a["ts"] / 1000, timezone.utc) + timedelta(hours=9)
    return (
        f"[찐다이버전스] {a['label']} · {a['symbol']}\n"
        f"기준: 본주 {a['yahoo']} ({a['exch']})\n"
        f"방향: {arrow}\n"
        f"타임프레임: {a['tf_name']}\n"
        f"신호 캔들(UTC+9): {kst.strftime('%m-%d %H:%M')}\n"
        f"지표가 뜬 시점 가격: {a['close']:.6g} {a['cur']}"
    )


def run(now_ms=None, send=True):
    """한 번 스캔. 새 알림 목록을 돌려준다."""
    now_ms = now_ms or int(time.time() * 1000)
    smap = load_map()
    if not smap:
        return []
    state = load_state()
    alerts = []
    items = [(sym, info, tf_name, gran) for sym, info in smap.items() for tf_name, gran in STOCK_TFS]

    def work(it):
        sym, info, tf_name, gran = it
        try:
            run_key(sym, info, tf_name, gran, state, now_ms, alerts)
        except Exception as e:
            print(f"[본주 에러] {sym} {gran}: {type(e).__name__}: {e}")
        time.sleep(0.1)

    with ThreadPoolExecutor(max_workers=3) as ex:
        list(ex.map(work, items))
    save_state(state)
    alerts.sort(key=lambda a: a["ts"])
    for a in alerts:
        msg = format_message(a)
        print(msg)
        if send:
            za.send_telegram(msg)
    print(f"[본주] 스캔 완료. 신규 알림 {len(alerts)}건, 상태 키 {len(state)}개")
    return alerts


if __name__ == "__main__":
    run()
