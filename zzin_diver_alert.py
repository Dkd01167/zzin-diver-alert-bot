"""
찐다이버전스(RSI 고신뢰 다이버전스) 알림 봇 — Bitget 선물시장 전체 감시.

감시 대상:
1. Bitget USDT-M 선물 중 isRwa=YES (주식/ETF/원자재/지수/외환 토큰) 전체
2. Bitget 시가총액 상위 20위 코인(CoinGecko 기준) 중 Bitget 선물에 있는 것
3. BTC/ETH/SOL/XRP/DOGE (명시적으로 항상 포함)

타임프레임: 15m, 30m, 1H, 4H, 6H, 1D, 1W (Bitget에 8H 없어서 6H로 대체)

로직(찐다이버전스, TradingView 인디케이터 "찐다이버 Donggun Ver"와 동일):
- RSI(14) 기준 과매수(>=70)/과매도(<=30) 구간(에피소드)의 극값(고가/저가+RSI)을 추적
- 직전 같은 방향 에피소드 대비 "가격은 신고점/신저점, RSI는 덜 극단적" -> 다이버전스 확정
- RSI가 50선을 터치하면 그 방향 기준 초기화
- 확정 시점(RSI가 70/30 재진입하는 순간)에 진입가를 Wilder RSI 역산으로 근사(그 캔들 안에서
  RSI가 정확히 70/30이 되는 가격), 손절은 피벗 캔들의 고가(숏)/저가(롱)
- 진입가가 피벗가보다 불리하면 스킵(대기)

상태(zzin_alert_state.json)에 "심볼|타임프레임"별 Wilder 평균/에피소드 추적값을 저장해서,
매 실행마다 신규 캔들만 증분 처리 -> 첫 실행(백필)만 무겁고 이후엔 가볍게 실행됨.
백필 구간에서 발견되는 과거 신호는 알림 보내지 않고 상태만 세팅함(신규 실시간 신호만 알림).

GitHub Actions에서 15분마다 실행 (.github/workflows/scan.yml).
읽기 전용 — 주문 없음, 공개 시세 데이터만 사용.
"""

import os
import sys
import json
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

sys.stdout.reconfigure(encoding="utf-8")

STATE_PATH = os.path.join(os.path.dirname(__file__), "zzin_alert_state.json")
SIGNALS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "signals.jsonl")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

PERIOD = 14
OB, OS = 70.0, 30.0

# tf_name(표시용), bitget granularity, 백필 기간(일)
TIMEFRAMES = [
    ("15분봉", "15m", 5),
    ("30분봉", "30m", 7),
    ("1시간봉", "1H", 10),
    ("4시간봉", "4H", 20),
    ("6시간봉", "6H", 30),
    ("일봉", "1D", 120),
    ("주봉", "1W", 730),
]
GRAN_MS = {
    "15m": 900_000, "30m": 1_800_000, "1H": 3_600_000, "4H": 14_400_000,
    "6H": 21_600_000, "1D": 86_400_000, "1W": 604_800_000,
}

EXPLICIT_MAJORS = {"BTC", "ETH", "SOL", "XRP", "DOGE"}

COMMODITY_SET = {"XAU", "XAG", "XPT", "XPD", "PAXG", "XAUT", "COPPER", "NATGAS", "CL"}
FOREX_SET = {"EURUSD", "USDJPY", "GBPUSD"}
INDEX_SET = {"SP500", "NDX100", "HSI", "JP225", "KR200"}
ETF_SET = {
    "QQQ", "TQQQ", "SQQQ", "SPY", "VOO", "IWM", "SOXL", "SOXS", "SOXX", "SMH",
    "XLU", "XLK", "XLV", "XLE", "GDX", "KWEB", "EWJ", "EWY", "EWT", "EWH", "EWZ",
    "INDA", "BOTZ", "UVXY", "TZA", "TMF", "TBT", "BITO", "IBB", "XBI", "QLD",
    "NVDL", "TSLL", "GGLL", "AMZU", "METU", "AAPU", "MSFU", "SGOV", "DFEN",
}


def classify(base_coin, is_rwa):
    b = base_coin.upper()
    if not is_rwa:
        return "알트코인" if b not in {"BTC", "ETH"} else "코인(메이저)"
    if b in COMMODITY_SET:
        return "원자재"
    if b in FOREX_SET:
        return "외환"
    if b in INDEX_SET:
        return "지수"
    if b in ETF_SET:
        return "ETF"
    return "주식"


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[경고] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 미설정 — 전송 생략")
        return
    import urllib.parse as _up
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = _up.urlencode({"chat_id": TELEGRAM_CHAT_ID, "text": text}).encode("utf-8")
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, data=data)
            with urllib.request.urlopen(req, timeout=10) as r:
                r.read()
            time.sleep(1.1)
            return
        except urllib.error.HTTPError as e:
            if e.code == 429:
                try:
                    wait = json.loads(e.read().decode()).get("parameters", {}).get("retry_after", 5)
                except Exception:
                    wait = 5
                time.sleep(wait + 1)
                continue
            print(f"[실패] 텔레그램 전송: HTTP {e.code}")
            return
        except Exception as e:
            print(f"[실패] 텔레그램 전송: {type(e).__name__}: {e}")
            return


def api_get(url, retries=5):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                d = json.loads(r.read().decode())
            if d.get("code") != "00000":
                if d.get("code") == "429" or "Too Many" in str(d.get("msg", "")):
                    time.sleep(1.5 * (attempt + 1))
                    continue
                return None
            return d.get("data", [])
        except urllib.error.HTTPError as e:
            if e.code != 429 and e.code < 500:
                return None  # 400 등 요청 자체가 잘못된 경우는 재시도해도 소용없음
            time.sleep(1.5 * (attempt + 1))
        except Exception:
            time.sleep(1.0 * (attempt + 1))
    return None


def fetch_bitget(symbol, granularity, start_ms, end_ms):
    out = []
    cur = start_ms
    # Bitget은 한 요청의 시작~종료 간격이 90일을 넘으면 거부함(일봉/주봉)
    step_ms = min(GRAN_MS[granularity] * 200, 89 * 86_400_000)
    base = "https://api.bitget.com/api/v2/mix/market/history-candles"
    while cur < end_ms:
        win_end = min(end_ms, cur + step_ms - 1)
        url = f"{base}?symbol={symbol}&granularity={granularity}&startTime={cur}&endTime={win_end}&limit=200&productType=USDT-FUTURES"
        data = api_get(url)
        time.sleep(0.25)
        if not data:
            cur = win_end + 1
            continue
        data_sorted = sorted(data, key=lambda k: int(k[0]))
        out.extend(data_sorted)
        cur = win_end + 1
    byts = {int(k[0]): k for k in out}
    return [byts[t] for t in sorted(byts.keys())]


def rsi_step(prev_avg_gain, prev_avg_loss, prev_close, close):
    diff = close - prev_close
    gain = max(diff, 0.0)
    loss = max(-diff, 0.0)
    if prev_avg_gain is None:
        return None, None, None
    ag = (prev_avg_gain * (PERIOD - 1) + gain) / PERIOD
    al = (prev_avg_loss * (PERIOD - 1) + loss) / PERIOD
    rsi = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return ag, al, rsi


def seed_rsi(closes):
    """closes: list of float, at least PERIOD+1 long. Returns (avg_gain, avg_loss, rsi, idx) for last bar,
    or None if not enough data."""
    n = len(closes)
    if n <= PERIOD:
        return None
    gains = [0.0] * n
    losses = [0.0] * n
    for i in range(1, n):
        d = closes[i] - closes[i - 1]
        gains[i] = max(d, 0.0)
        losses[i] = max(-d, 0.0)
    ag = sum(gains[1:PERIOD + 1]) / PERIOD
    al = sum(losses[1:PERIOD + 1]) / PERIOD
    rsi = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    hist = [(PERIOD, ag, al, rsi)]
    for i in range(PERIOD + 1, n):
        ag = (ag * (PERIOD - 1) + gains[i]) / PERIOD
        al = (al * (PERIOD - 1) + losses[i]) / PERIOD
        rsi = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
        hist.append((i, ag, al, rsi))
    return hist


def crossing_price(prev_close, prev_ag, prev_al, level, expect):
    RS_target = level / (100 - level)
    if prev_ag is None or prev_al is None:
        return None
    if expect == "loss":
        curr_loss = PERIOD * (prev_ag / RS_target - prev_al)
        if curr_loss < 0:
            return None
        return prev_close - curr_loss
    else:
        curr_gain = PERIOD * (RS_target * prev_al - prev_ag)
        if curr_gain < 0:
            return None
        return prev_close + curr_gain


def fresh_ep_state():
    return {
        "inOB": False, "epHiPrice": None, "epHiRSI": None, "epHiTs": None,
        "inOS": False, "epLoPrice": None, "epLoRSI": None, "epLoTs": None,
        "lastHiRSI": None, "lastHiPrice": None, "lastHiTs": None,
        "lastLoRSI": None, "lastLoPrice": None, "lastLoTs": None,
        "prevRsi": None,
    }


def process_bar(st, ts, high, low, close, rsi, prev_close, prev_ag, prev_al, emit_alerts, alerts, ctx):
    prevRsi = st["prevRsi"]
    if prevRsi is not None:
        if prevRsi < 50 <= rsi:
            st["lastLoRSI"] = st["lastLoPrice"] = st["lastLoTs"] = None
        if prevRsi > 50 >= rsi:
            st["lastHiRSI"] = st["lastHiPrice"] = st["lastHiTs"] = None

    # overbought side
    if rsi >= OB:
        if not st["inOB"]:
            st["inOB"] = True
            st["epHiPrice"], st["epHiRSI"], st["epHiTs"] = high, rsi, ts
        elif rsi > st["epHiRSI"]:
            st["epHiPrice"], st["epHiRSI"], st["epHiTs"] = high, rsi, ts
    else:
        if st["inOB"]:
            st["inOB"] = False
            if st["lastHiRSI"] is not None and st["epHiPrice"] > st["lastHiPrice"] and st["epHiRSI"] < st["lastHiRSI"]:
                cp = crossing_price(prev_close, prev_ag, prev_al, OB, "loss")
                if cp is None or not (low <= cp <= high):
                    cp = close
                if cp <= st["epHiPrice"]:
                    if emit_alerts:
                        alerts.append({
                            "dir": "short", "ts": ts, "close": close, "entry": cp, "stop": st["epHiPrice"],
                            "pivot_rsi": st["epHiRSI"], "prev_pivot_rsi": st["lastHiRSI"], **ctx,
                        })
            st["lastHiRSI"], st["lastHiPrice"], st["lastHiTs"] = st["epHiRSI"], st["epHiPrice"], st["epHiTs"]

    # oversold side
    if rsi <= OS:
        if not st["inOS"]:
            st["inOS"] = True
            st["epLoPrice"], st["epLoRSI"], st["epLoTs"] = low, rsi, ts
        elif rsi < st["epLoRSI"]:
            st["epLoPrice"], st["epLoRSI"], st["epLoTs"] = low, rsi, ts
    else:
        if st["inOS"]:
            st["inOS"] = False
            if st["lastLoRSI"] is not None and st["epLoPrice"] < st["lastLoPrice"] and st["epLoRSI"] > st["lastLoRSI"]:
                cp = crossing_price(prev_close, prev_ag, prev_al, OS, "gain")
                if cp is None or not (low <= cp <= high):
                    cp = close
                if cp >= st["epLoPrice"]:
                    if emit_alerts:
                        alerts.append({
                            "dir": "long", "ts": ts, "close": close, "entry": cp, "stop": st["epLoPrice"],
                            "pivot_rsi": st["epLoRSI"], "prev_pivot_rsi": st["lastLoRSI"], **ctx,
                        })
            st["lastLoRSI"], st["lastLoPrice"], st["lastLoTs"] = st["epLoRSI"], st["epLoPrice"], st["epLoTs"]

    st["prevRsi"] = rsi


def is_due(tf, now):
    """해당 타임프레임의 새 봉이 방금 마감됐을 시점인지(15분 실행 주기 기준 여유 포함)."""
    m, h, wd = now.minute, now.hour, now.weekday()
    if tf == "15m":
        return True
    if tf == "30m":
        return (m % 30) < 15
    if tf == "1H":
        return m < 15
    if tf == "4H":
        return (h % 4 == 0) and m < 15
    if tf == "6H":
        return (h % 6 == 0) and m < 15
    if tf == "1D":
        return h == 0 and m < 15
    if tf == "1W":
        return wd == 0 and h == 0 and m < 15
    return False


def run_symbol_tf(symbol, tf_gran, warm_days, state, key, now_ms, emit_alerts, ctx_base):
    st_entry = state.get(key)
    alerts = []

    if st_entry is None:
        # 최초 백필
        start_ms = now_ms - warm_days * 86_400_000
        raw = fetch_bitget(symbol, tf_gran, start_ms, now_ms)
        if len(raw) <= PERIOD + 1:
            return alerts
        closes = [float(k[4]) for k in raw]
        hist = seed_rsi(closes)
        if hist is None:
            return alerts
        st = fresh_ep_state()
        prev_close = closes[hist[0][0] - 1]
        prev_ag, prev_al = None, None
        for idx, ag, al, rsi in hist:
            k = raw[idx]
            ts, high, low, close = int(k[0]), float(k[2]), float(k[3]), float(k[4])
            process_bar(st, ts, high, low, close, rsi, prev_close, prev_ag, prev_al,
                        emit_alerts=False, alerts=alerts, ctx=ctx_base)
            prev_close = close
            prev_ag, prev_al = ag, al
        last_idx, last_ag, last_al, last_rsi = hist[-1]
        state[key] = {
            **st, "last_ts": int(raw[last_idx][0]), "avg_gain": last_ag, "avg_loss": last_al,
            "last_close": float(raw[last_idx][4]),
        }
        return []  # 백필 알림은 보내지 않음

    # 증분 처리
    last_ts = st_entry["last_ts"]
    raw = fetch_bitget(symbol, tf_gran, last_ts + 1, now_ms)
    # Bitget은 시작 시각이 캔들 중간이면 그 캔들과 바로 앞 캔들까지 돌려줘서, 이미 처리한 캔들이 섞임
    raw = [k for k in raw if int(k[0]) > last_ts]
    if not raw:
        return alerts
    st = {k: v for k, v in st_entry.items() if k not in ("last_ts", "avg_gain", "avg_loss", "last_close")}
    ag, al = st_entry["avg_gain"], st_entry["avg_loss"]
    prev_close = st_entry["last_close"]
    for k in raw:
        ts, high, low, close = int(k[0]), float(k[2]), float(k[3]), float(k[4])
        ag, al, rsi = rsi_step(ag, al, prev_close, close)
        if rsi is None:
            prev_close = close
            continue
        ctx = dict(ctx_base)
        fresh = (now_ms - ts) <= max(3 * GRAN_MS[tf_gran], 1_800_000)
        process_bar(st, ts, high, low, close, rsi, prev_close, ag, al, emit_alerts and fresh, alerts, ctx)
        prev_close = close
    state[key] = {**st, "last_ts": int(raw[-1][0]), "avg_gain": ag, "avg_loss": al, "last_close": prev_close}
    return alerts


def build_universe():
    contracts_url = "https://api.bitget.com/api/v2/mix/market/contracts?productType=USDT-FUTURES"
    data = api_get(contracts_url) or []
    rwa = [(c["symbol"], c["baseCoin"], True, float(c.get("takerFeeRate", 0.0006)))
           for c in data if c.get("isRwa") == "YES" and c.get("symbolStatus") == "normal"]
    non_rwa = {c["baseCoin"]: (c["symbol"], float(c.get("takerFeeRate", 0.0006)))
               for c in data if c.get("isRwa") != "YES" and c.get("quoteCoin") == "USDT"
               and c.get("symbolStatus") == "normal"}

    crypto = []
    added = set()
    for base in EXPLICIT_MAJORS:
        if base in non_rwa and base not in added:
            sym, fee = non_rwa[base]
            crypto.append((sym, base, False, fee))
            added.add(base)
    try:
        cg_url = "https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&order=market_cap_desc&per_page=20&page=1"
        req = urllib.request.Request(cg_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            top20 = json.loads(r.read().decode())
        for c in top20:
            base = c["symbol"].upper()
            if base in non_rwa and base not in added:
                sym, fee = non_rwa[base]
                crypto.append((sym, base, False, fee))
                added.add(base)
    except Exception as e:
        print(f"[경고] CoinGecko 상위 20 조회 실패, 명시 종목만 사용: {e}")

    return rwa + crypto


def main():
    now = datetime.now(timezone.utc)
    now_ms = int(now.timestamp() * 1000)
    universe = build_universe()
    print(f"감시 대상 종목: {len(universe)}개")

    due_tfs = [(tf_name, gran, warm) for tf_name, gran, warm in TIMEFRAMES if is_due(gran, now)]
    due_grans = {t[1] for t in due_tfs}
    print(f"이번 실행에 해당하는 타임프레임: {[t[0] for t in due_tfs]}")

    state = load_state()
    all_alerts = []

    def work(item):
        symbol, base_coin, is_rwa, fee = item
        label = classify(base_coin, is_rwa)
        found = []
        for tf_name, gran, warm_days in TIMEFRAMES:
            key = f"{symbol}|{gran}"
            if gran not in due_grans and key in state:
                continue
            ctx = {"symbol": symbol, "base_coin": base_coin, "label": label, "tf_name": tf_name, "gran": gran}
            try:
                found.extend(run_symbol_tf(symbol, gran, warm_days, state, key, now_ms,
                                           emit_alerts=True, ctx_base=ctx))
            except Exception as e:
                print(f"[에러] {symbol} {tf_name}: {type(e).__name__}: {e}")
        return found

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=3) as ex:
        for found in ex.map(work, universe):
            all_alerts.extend(found)

    try:  # 본주가 있는 주식/ETF 토큰은 본주 차트 기준 알림(zzin_stock_alert.py)으로 대체 -> 여기서는 텔레그램 안 보냄
        import zzin_stock_alert as _zs
        stock_syms = _zs.mapped_symbols()
    except Exception as e:
        print(f"[경고] 본주 매핑 로드 실패, Bitget 기준 알림 그대로 전송: {e}")
        stock_syms = set()

    for a in all_alerts:
        arrow = "숏 🔻" if a["dir"] == "short" else "롱 🔺"
        kst = datetime.fromtimestamp(a["ts"] / 1000, timezone.utc) + timedelta(hours=9)
        msg = (
            f"[찐다이버전스] {a['label']} · {a['symbol']}\n"
            f"방향: {arrow}\n"
            f"타임프레임: {a['tf_name']}\n"
            f"신호 캔들(UTC+9): {kst.strftime('%m-%d %H:%M')}\n"
            f"지표가 뜬 시점 가격: {a['close']:.6g}"
        )
        if a["symbol"] not in stock_syms:
            print(msg)
            send_telegram(msg)
        try:  # 주문 봇(zzin_trader.py)이 읽는 신호 파일
            with open(SIGNALS_PATH, "a", encoding="utf-8") as sf:
                sf.write(json.dumps({
                    "id": f"{a['symbol']}|{a['gran']}|{a['ts']}", "symbol": a["symbol"], "dir": a["dir"],
                    "gran": a["gran"], "tf_name": a["tf_name"], "label": a["label"], "ts": a["ts"],
                    "close": a["close"], "entry": a["entry"], "stop": a["stop"],
                    "detected_ms": int(time.time() * 1000),
                }, ensure_ascii=False) + "\n")
        except Exception as e:
            print(f"[경고] 신호 파일 기록 실패: {e}")

    save_state(state)
    print(f"\n완료. 신규 알림 {len(all_alerts)}건, 저장된 상태 키 {len(state)}개")


if __name__ == "__main__":
    main()
