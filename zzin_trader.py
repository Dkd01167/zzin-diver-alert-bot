"""
찐다이버전스 자동매매 봇 (Bitget USDT-M 선물).

신호 소스: zzin_diver_alert.py가 기록하는 signals.jsonl (알림봇과 같은 폴더).
LIVE=0(기본)이면 모의 모드 — 실제 주문 없이 시세로 가상 체결/청산만 기록.
LIVE=1이면 실주문 (Bitget API 키 필요: BITGET_API_KEY / BITGET_API_SECRET / BITGET_PASSPHRASE).
BITGET_DEMO=1이면 Bitget 데모 계정(SBTC/SETH/SXRP만 가능)으로 주문.

규칙:
- 신호: 찐다이버전스 확정 시 진입. 종목당 포지션 1개. 자금이 모자라거나 상한이면 건너뜀.
- 진입: 시장가, 증거금 MARGIN_USDT x LEVERAGE배.
- 손절: 지표 피벗 캔들 고가(숏)/저가(롱). 진입가 대비 거리가 (MAX_LOSS_PCT_OF_MARGIN / LEVERAGE)%를
  넘으면 그 거리로 당김(증거금의 30% 손실 상한). 현재가가 이미 손절선을 넘었으면 진입 안 함.
- 익절 사다리: 1R 본절 이동 / 2R 잔량 30% 익절+손절 1R / 3R부터 정수 R마다 잔량 10% 익절+손절 직전 R.
"""

import os
import sys
import json
import time
import hmac
import base64
import hashlib
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone
from math import floor

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
SIGNALS_PATH = os.path.join(HERE, "signals.jsonl")
STATE_PATH = os.path.join(HERE, "trader_state.json")
TRADES_LOG = os.path.join(HERE, "trader_trades.jsonl")
STOP_FILE = os.path.join(HERE, "STOP_ENTRIES")


def _f(name, default):
    return float(os.getenv(name, default))


LIVE = os.getenv("LIVE", "0") == "1"
DEMO = os.getenv("BITGET_DEMO", "0") == "1"
MARGIN = _f("MARGIN_USDT", "10")
LEV = int(_f("LEVERAGE", "10"))
MAX_POS = int(_f("MAX_POSITIONS", "200"))
MAX_LOSS_PCT_MARGIN = _f("MAX_LOSS_PCT_OF_MARGIN", "30")
TFS = set(t.strip() for t in os.getenv("TIMEFRAMES", "30m,1H,4H,6H,1D,1W").split(","))
POLL = int(_f("POLL_SECONDS", "15"))
DRY_BALANCE = _f("DRY_BALANCE", "300")
MIN_FREE = _f("MIN_FREE_MARGIN_BUFFER", "3")
DAILY_LOSS_LIMIT = _f("DAILY_LOSS_LIMIT_USDT", "0")  # 0 = 사용 안 함
FEE = 0.0006
ALLOW = set(x.strip() for x in os.getenv("ALLOW_SYMBOLS", "").split(",") if x.strip())  # 비우면 전 종목

TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")
MODE_TAG = "실전" if (LIVE and not DEMO) else ("데모" if LIVE and DEMO else "모의")

DEMO_MAP = {"BTCUSDT": "SBTCSUSDT", "ETHUSDT": "SETHSUSDT", "XRPUSDT": "SXRPSUSDT"}


def log(msg):
    print(f"[{datetime.now(timezone.utc).strftime('%m-%d %H:%M:%S')}] {msg}", flush=True)


def tg(text):
    if not TG_TOKEN or not TG_CHAT:
        return
    data = urllib.parse.urlencode({"chat_id": TG_CHAT, "text": text}).encode("utf-8")
    for _ in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(
                    f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data=data), timeout=10) as r:
                r.read()
            time.sleep(1.1)
            return
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(6)
                continue
            return
        except Exception:
            return


# ---------------------------------------------------------------- 순수 계산 함수 (테스트 대상)
def round_down(v, step):
    return floor(v / step + 1e-9) * step


def fmt(v, places):
    return f"{v:.{places}f}"


def price_step(c):
    return float(c["priceEndStep"]) / (10 ** int(c["pricePlace"]))


def round_price(c, p):
    step = price_step(c)
    return round(round(p / step) * step, int(c["pricePlace"]))


def decide_stop(direction, price, pivot_stop, max_dist):
    """현재가와 피벗 손절선으로 실제 손절가를 정한다. 진입 불가면 None."""
    if direction == "short":
        if price >= pivot_stop:
            return None
        dist = (pivot_stop - price) / price
        return pivot_stop if dist <= max_dist else price * (1 + max_dist)
    if price <= pivot_stop:
        return None
    dist = (price - pivot_stop) / price
    return pivot_stop if dist <= max_dist else price * (1 - max_dist)


def level_price(pos, k):
    sgn = 1 if pos["dir"] == "long" else -1
    return pos["entry"] + sgn * k * pos["R"]


def advance(pos, price):
    """가격이 다음 R 레벨에 닿았는지 보고 (레벨, 익절비율, 새 손절가) 목록을 돌려준다. pos['next_level']만 갱신."""
    actions = []
    while True:
        k = pos["next_level"]
        lvl = level_price(pos, k)
        hit = price >= lvl if pos["dir"] == "long" else price <= lvl
        if not hit:
            break
        if k == 1:
            frac, new_stop = 0.0, pos["entry"]
        elif k == 2:
            frac, new_stop = 0.30, level_price(pos, 1)
        else:
            frac, new_stop = 0.10, level_price(pos, k - 1)
        actions.append((k, frac, new_stop))
        pos["next_level"] = k + 1
    return actions


# ---------------------------------------------------------------- 거래소 인터페이스
class BitgetError(Exception):
    pass


class Client:
    BASE = "https://api.bitget.com"

    def __init__(self, key="", secret="", passphrase=""):
        self.key, self.secret, self.passphrase = key, secret, passphrase

    def call(self, method, path, params=None, body=None, signed=True):
        qs = urllib.parse.urlencode(params) if params else ""
        req_path = path + ("?" + qs if qs else "")
        body_s = json.dumps(body, separators=(",", ":")) if body else ""
        headers = {"Content-Type": "application/json", "locale": "en-US", "User-Agent": "zzin-trader"}
        if signed:
            ts = str(int(time.time() * 1000))
            pre = ts + method.upper() + req_path + body_s
            sig = base64.b64encode(hmac.new(self.secret.encode(), pre.encode(), hashlib.sha256).digest()).decode()
            headers.update({"ACCESS-KEY": self.key, "ACCESS-SIGN": sig,
                            "ACCESS-TIMESTAMP": ts, "ACCESS-PASSPHRASE": self.passphrase})
        if DEMO:
            headers["paptrading"] = "1"
        req = urllib.request.Request(self.BASE + req_path, data=body_s.encode() if body_s else None,
                                     headers=headers, method=method.upper())
        last = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=15) as r:
                    d = json.loads(r.read().decode())
                break
            except urllib.error.HTTPError as e:
                txt = e.read().decode()[:300]
                if e.code == 429 or e.code >= 500:
                    last = f"HTTP {e.code} {txt}"
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise BitgetError(f"HTTP {e.code} {txt}")
            except Exception as e:
                last = str(e)
                time.sleep(1.5 * (attempt + 1))
        else:
            raise BitgetError(last or "network error")
        if d.get("code") != "00000":
            raise BitgetError(f"{d.get('code')} {d.get('msg')}")
        return d.get("data")


class BaseEx:
    """공통: 계약 정보 / 시세."""

    def __init__(self, client):
        self.c = client
        self.contracts = {}
        self.product = "SUSDT-FUTURES" if DEMO else "USDT-FUTURES"
        self.coin = "SUSDT" if DEMO else "USDT"

    def load_contracts(self):
        data = self.c.call("GET", "/api/v2/mix/market/contracts",
                           {"productType": self.product.lower() if DEMO else self.product}, signed=False)
        self.contracts = {x["symbol"]: x for x in data or []}

    def api_sym(self, sym):
        return DEMO_MAP.get(sym) if DEMO else sym

    def tradable(self, sym):
        s = self.api_sym(sym)
        return bool(s) and s in self.contracts and self.contracts[s].get("symbolStatus") == "normal"

    def info(self, sym):
        return self.contracts[self.api_sym(sym)]

    def prices(self):
        data = self.c.call("GET", "/api/v2/mix/market/tickers",
                           {"productType": self.product.lower() if DEMO else self.product}, signed=False)
        rev = {v: k for k, v in DEMO_MAP.items()} if DEMO else {}
        out = {}
        for t in data or []:
            try:
                out[rev.get(t["symbol"], t["symbol"])] = float(t.get("markPrice") or t["lastPr"])
            except Exception:
                pass
        return out


class LiveEx(BaseEx):
    def __init__(self, client):
        super().__init__(client)
        self.posmode = {}
        self.prepared = set()

    def balance(self):
        data = self.c.call("GET", "/api/v2/mix/account/accounts", {"productType": self.product})
        for a in data or []:
            if a.get("marginCoin") == self.coin:
                return float(a.get("available", 0))
        return 0.0

    def positions(self):
        data = self.c.call("GET", "/api/v2/mix/position/all-position",
                           {"productType": self.product, "marginCoin": self.coin})
        rev = {v: k for k, v in DEMO_MAP.items()} if DEMO else {}
        out = {}
        for p in data or []:
            size = float(p.get("total") or 0)
            if size > 0:
                out[rev.get(p["symbol"], p["symbol"])] = {
                    "side": p["holdSide"], "size": size, "avg": float(p.get("openPriceAvg") or 0)}
        return out

    def prep(self, sym):
        s = self.api_sym(sym)
        if s in self.prepared:
            return
        acct = self.c.call("GET", "/api/v2/mix/account/account",
                           {"symbol": s, "productType": self.product, "marginCoin": self.coin})
        self.posmode[s] = (acct or {}).get("posMode", "one_way_mode")
        if (acct or {}).get("marginMode") != "isolated":
            self.c.call("POST", "/api/v2/mix/account/set-margin-mode",
                        body={"symbol": s, "productType": self.product, "marginCoin": self.coin,
                              "marginMode": "isolated"})
        self.c.call("POST", "/api/v2/mix/account/set-leverage",
                    body={"symbol": s, "productType": self.product, "marginCoin": self.coin,
                          "leverage": str(LEV)})
        self.prepared.add(s)

    def _order_params(self, s, side, size, opening):
        info = self.contracts[s]
        p = {"symbol": s, "productType": self.product, "marginMode": "isolated", "marginCoin": self.coin,
             "size": fmt(size, int(info["volumePlace"])), "orderType": "market",
             "clientOid": f"zz{int(time.time() * 1000)}{abs(hash((s, side, size))) % 1000}"}
        hedge = self.posmode.get(s) == "hedge_mode"
        if hedge:
            p["side"] = "buy" if side == "long" else "sell"
            p["tradeSide"] = "open" if opening else "close"
        else:
            if opening:
                p["side"] = "buy" if side == "long" else "sell"
            else:
                p["side"] = "sell" if side == "long" else "buy"
                p["reduceOnly"] = "YES"
        return p

    def open_market(self, sym, side, size, stop_price):
        s = self.api_sym(sym)
        info = self.contracts[s]
        p = self._order_params(s, side, size, True)
        p["presetStopLossPrice"] = fmt(stop_price, int(info["pricePlace"]))
        res = self.c.call("POST", "/api/v2/mix/order/place-order", body=p)
        oid = (res or {}).get("orderId")
        time.sleep(0.7)
        fill = None
        try:
            det = self.c.call("GET", "/api/v2/mix/order/detail",
                              {"symbol": s, "productType": self.product, "orderId": oid})
            fill = float((det or {}).get("priceAvg") or 0) or None
        except Exception:
            pass
        if not fill:
            pos = self.positions().get(sym)
            fill = pos["avg"] if pos else None
        return fill

    def close_market(self, sym, side, size):
        s = self.api_sym(sym)
        self.c.call("POST", "/api/v2/mix/order/place-order", body=self._order_params(s, side, size, False))

    def _pending_stops(self, s):
        data = self.c.call("GET", "/api/v2/mix/order/orders-plan-pending",
                           {"productType": self.product, "planType": "profit_loss", "symbol": s})
        lst = (data or {}).get("entrustedList") or []
        return [o for o in lst if o.get("planType") in ("loss_plan", "pos_loss")]

    def has_stop(self, sym):
        return len(self._pending_stops(self.api_sym(sym))) > 0

    def set_stop(self, sym, side, price):
        s = self.api_sym(sym)
        info = self.contracts[s]
        old = self._pending_stops(s)
        hedge = self.posmode.get(s) == "hedge_mode"
        hold = side if hedge else ("buy" if side == "long" else "sell")
        self.c.call("POST", "/api/v2/mix/order/place-tpsl-order",
                    body={"marginCoin": self.coin, "productType": self.product, "symbol": s,
                          "planType": "pos_loss", "triggerPrice": fmt(price, int(info["pricePlace"])),
                          "triggerType": "mark_price", "executePrice": "0", "holdSide": hold})
        if old:
            try:
                self.c.call("POST", "/api/v2/mix/order/cancel-plan-order",
                            body={"orderIdList": [{"orderId": o["orderId"]} for o in old], "symbol": s,
                                  "productType": self.product, "marginCoin": self.coin,
                                  "planType": "profit_loss"})
            except BitgetError as e:
                log(f"[경고] 이전 손절 주문 취소 실패({sym}): {e}")

    def tick(self, prices):
        return []


class DryEx(BaseEx):
    """모의 거래소: 실제 시세로 가상 체결. 손절은 15초 폴링 가격으로 판정."""

    def __init__(self, client, saved=None):
        super().__init__(client)
        saved = saved or {}
        self.pos = saved.get("pos", {})
        self.realized = saved.get("realized", 0.0)

    def dump(self):
        return {"pos": self.pos, "realized": self.realized}

    def balance(self):
        used = sum(p["size"] * p["avg"] / LEV for p in self.pos.values())
        return DRY_BALANCE + self.realized - used

    def positions(self):
        return {s: dict(p) for s, p in self.pos.items()}

    def prep(self, sym):
        pass

    def open_market(self, sym, side, size, stop_price):
        px = self.last_prices.get(sym)
        self.pos[sym] = {"side": side, "size": size, "avg": px, "stop": stop_price}
        self.realized -= size * px * FEE
        return px

    def close_market(self, sym, side, size):
        p = self.pos[sym]
        px = self.last_prices[sym]
        pnl = (px - p["avg"]) * size * (1 if side == "long" else -1) - size * px * FEE
        self.realized += pnl
        p["size"] -= size
        if p["size"] <= 1e-12:
            del self.pos[sym]

    def has_stop(self, sym):
        return sym in self.pos

    def set_stop(self, sym, side, price):
        if sym in self.pos:
            self.pos[sym]["stop"] = price

    def tick(self, prices):
        """손절 도달한 포지션을 청산하고 [(sym, 청산가)] 반환."""
        self.last_prices = prices
        hit = []
        for sym, p in list(self.pos.items()):
            px = prices.get(sym)
            if px is None:
                continue
            if (p["side"] == "long" and px <= p["stop"]) or (p["side"] == "short" and px >= p["stop"]):
                self.close_market(sym, p["side"], p["size"])
                hit.append((sym, px))
        return hit


# ---------------------------------------------------------------- 트레이더
class Trader:
    def __init__(self, ex):
        self.ex = ex
        self.state = {"positions": {}, "seen": [], "sig_offset": None, "stats": {"closed": 0, "R": 0.0},
                      "day": "", "day_pnl_R": 0.0}
        if os.path.exists(STATE_PATH):
            self.state.update(json.load(open(STATE_PATH, encoding="utf-8")))
        if isinstance(ex, DryEx):
            ex.pos = self.state.get("dry", {}).get("pos", {})
            ex.realized = self.state.get("dry", {}).get("realized", 0.0)
        if self.state["sig_offset"] is None:
            self.state["sig_offset"] = os.path.getsize(SIGNALS_PATH) if os.path.exists(SIGNALS_PATH) else 0

    def save(self):
        if isinstance(self.ex, DryEx):
            self.state["dry"] = self.ex.dump()
        self.state["seen"] = self.state["seen"][-5000:]
        tmp = STATE_PATH + ".tmp"
        json.dump(self.state, open(tmp, "w", encoding="utf-8"), ensure_ascii=False)
        os.replace(tmp, STATE_PATH)

    def new_signals(self):
        if not os.path.exists(SIGNALS_PATH):
            return []
        out = []
        with open(SIGNALS_PATH, "rb") as f:
            f.seek(self.state["sig_offset"])
            data = f.read()
        cut = data.rfind(b"\n") + 1
        for line in data[:cut].splitlines():
            try:
                out.append(json.loads(line.decode("utf-8")))
            except Exception:
                pass
        self.state["sig_offset"] += cut
        return out

    # ---- 진입
    def try_enter(self, s, prices):
        sym = s["symbol"]
        if s["id"] in self.state["seen"]:
            return
        self.state["seen"].append(s["id"])
        if s["gran"] not in TFS:
            return
        if ALLOW and sym not in ALLOW:
            return
        if os.path.exists(STOP_FILE):
            log(f"신규 진입 중지 파일 있음 — 건너뜀 {sym}")
            return
        if sym in self.state["positions"]:
            log(f"이미 포지션 있음 — 건너뜀 {sym} {s['tf_name']}")
            return
        if len(self.state["positions"]) >= MAX_POS:
            log(f"동시 포지션 상한 — 건너뜀 {sym}")
            return
        if not self.ex.tradable(sym):
            log(f"거래 불가 종목 — 건너뜀 {sym}")
            return
        if DAILY_LOSS_LIMIT > 0 and self.state["day_pnl_usdt"] <= -DAILY_LOSS_LIMIT:
            log("일일 손실 한도 — 건너뜀")
            return
        px = prices.get(sym)
        if not px:
            return
        if self.ex.balance() < MARGIN + MIN_FREE:
            log(f"증거금 부족 — 건너뜀 {sym} (잔고 {self.ex.balance():.1f})")
            return
        info = self.ex.info(sym)
        max_dist = MAX_LOSS_PCT_MARGIN / 100.0 / LEV
        stop = decide_stop(s["dir"], px, float(s["stop"]), max_dist)
        if stop is None:
            log(f"현재가가 이미 손절선을 넘음 — 건너뜀 {sym} px={px} stop={s['stop']}")
            return
        stop = round_price(info, stop)
        if abs(px - stop) < 2 * price_step(info):
            log(f"손절선이 현재가와 너무 가까움 — 건너뜀 {sym}")
            return
        step = float(info["sizeMultiplier"])
        size = round_down(MARGIN * LEV / px, step)
        if size < float(info["minTradeNum"]) or size * px < float(info.get("minTradeUSDT") or 5):
            log(f"최소 주문 수량 미달 — 건너뜀 {sym} size={size}")
            return
        try:
            self.ex.prep(sym)
            fill = self.ex.open_market(sym, s["dir"], size, stop)
        except BitgetError as e:
            log(f"[주문 실패] {sym}: {e}")
            tg(f"[{MODE_TAG}] 주문 실패 {sym}: {e}")
            return
        if not fill:
            tg(f"[{MODE_TAG}] 진입 체결가 확인 실패 {sym} — 직접 확인 필요")
            return
        R = abs(fill - stop)
        pos = {"sym": sym, "dir": s["dir"], "gran": s["gran"], "tf": s["tf_name"], "entry": fill, "stop0": stop,
               "stop": stop, "R": R, "size0": size, "rem": 1.0, "next_level": 1, "realized_R": 0.0,
               "opened_ms": int(time.time() * 1000), "capped": abs(float(s["stop"]) - stop) > 2 * price_step(info)}
        self.state["positions"][sym] = pos
        risk = R / fill * MARGIN * LEV
        tg(f"[진입|{MODE_TAG}] {s['label']} · {sym} {'롱 🔺' if s['dir'] == 'long' else '숏 🔻'} {s['tf_name']}\n"
           f"체결가 {fill:.6g} / 손절가 {stop:.6g}{' (3% 상한으로 당김)' if pos['capped'] else ''}\n"
           f"수량 {size:.8g} (포지션 {size * fill:.1f} USDT) / 손절 시 손실 약 {risk:.2f} USDT")
        log(f"진입 {sym} {s['dir']} {s['tf_name']} fill={fill} stop={stop} R={R}")

    # ---- 관리
    def manage(self, prices):
        exch = self.ex.positions()
        for sym, pos in list(self.state["positions"].items()):
            if sym not in exch:
                self.finalize(sym, pos, prices)
                continue
            px = prices.get(sym)
            if px is None:
                continue
            actions = advance(pos, px)
            if not actions:
                continue
            info = self.ex.info(sym)
            step = float(info["sizeMultiplier"])
            new_stop = pos["stop"]
            lines = []
            for k, frac, ns in actions:
                if frac > 0:
                    take = pos["rem"] * frac
                    size = round_down(take * pos["size0"], step)
                    left = exch[sym]["size"]
                    if size >= float(info["minTradeNum"]) and size <= left:
                        try:
                            self.ex.close_market(sym, pos["dir"], size)
                            exch[sym]["size"] = left - size
                            pos["realized_R"] += take * k
                            pos["rem"] -= take
                            lines.append(f"{k}R 도달: {int(frac * 100)}% 익절 (수량 {size:.8g})")
                        except BitgetError as e:
                            log(f"[익절 실패] {sym} {k}R: {e}")
                            tg(f"[{MODE_TAG}] 익절 주문 실패 {sym} {k}R: {e}")
                    else:
                        lines.append(f"{k}R 도달: 최소 수량 때문에 익절 건너뜀")
                else:
                    lines.append(f"{k}R 도달: 손절선을 진입가로 이동")
                new_stop = ns
            new_stop = round_price(info, new_stop)
            if new_stop != pos["stop"]:
                try:
                    self.ex.set_stop(sym, pos["dir"], new_stop)
                    pos["stop"] = new_stop
                except BitgetError as e:
                    log(f"[손절 이동 실패] {sym}: {e}")
                    tg(f"[{MODE_TAG}] 손절선 이동 실패 {sym} → {new_stop}: {e} (이전 손절은 유지됨)")
            tg(f"[{MODE_TAG}] {sym} " + " / ".join(lines) + f"\n현재 손절가 {pos['stop']:.6g}")

    def finalize(self, sym, pos, prices):
        sgn = 1 if pos["dir"] == "long" else -1
        exit_px = pos["stop"]
        rR = pos["realized_R"] + pos["rem"] * (exit_px - pos["entry"]) * sgn / pos["R"]
        pnl = rR * pos["R"] / pos["entry"] * MARGIN * LEV
        del self.state["positions"][sym]
        self.state["stats"]["closed"] += 1
        self.state["stats"]["R"] += rR
        self.state["day_pnl_usdt"] = self.state.get("day_pnl_usdt", 0.0) + pnl
        rec = {"sym": sym, "dir": pos["dir"], "tf": pos["tf"], "entry": pos["entry"], "stop0": pos["stop0"],
               "exit_est": exit_px, "R": round(rR, 3), "pnl_est_usdt": round(pnl, 2),
               "opened_ms": pos["opened_ms"], "closed_ms": int(time.time() * 1000)}
        with open(TRADES_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        tg(f"[종료|{MODE_TAG}] {sym} {pos['tf']} {'롱' if pos['dir'] == 'long' else '숏'}\n"
           f"손절선({exit_px:.6g}) 도달로 종료 / 결과 약 {rR:+.2f}R ≈ {pnl:+.2f} USDT (추정)\n"
           f"누적 {self.state['stats']['closed']}건 {self.state['stats']['R']:+.2f}R")
        log(f"종료 {sym} {rR:+.2f}R")

    def protect(self):
        """실전: 포지션마다 손절 주문이 실제로 걸려 있는지 확인. 없으면 다시 걸고, 실패하면 시장가 청산."""
        for sym, pos in list(self.state["positions"].items()):
            try:
                if self.ex.has_stop(sym):
                    continue
                log(f"손절 주문 없음 — 재설정 {sym}")
                try:
                    self.ex.set_stop(sym, pos["dir"], pos["stop"])
                    tg(f"[{MODE_TAG}] {sym} 손절 주문이 없어서 다시 걸었습니다.")
                except BitgetError as e:
                    size = self.ex.positions().get(sym, {}).get("size")
                    if size:
                        self.ex.close_market(sym, pos["dir"], size)
                    tg(f"[{MODE_TAG}] {sym} 손절 주문 설정 실패로 시장가 청산했습니다: {e}")
            except BitgetError as e:
                log(f"[보호 점검 실패] {sym}: {e}")

    def step(self, n):
        prices = self.ex.prices()
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self.state.get("day") != day:
            self.state["day"] = day
            self.state["day_pnl_usdt"] = 0.0
        if isinstance(self.ex, DryEx):
            for sym, px in self.ex.tick(prices):
                log(f"[모의] 손절 체결 {sym} @ {px}")
        else:
            self.ex.last_prices = prices
        self.manage(prices)
        for s in self.new_signals():
            self.try_enter(s, prices)
        if n % 4 == 0 and LIVE:
            self.protect()
        self.save()


def main():
    key = os.getenv("BITGET_API_KEY", "")
    if LIVE and not (key and os.getenv("BITGET_API_SECRET") and os.getenv("BITGET_PASSPHRASE")):
        log("LIVE=1인데 API 키가 없습니다. 종료합니다.")
        return
    client = Client(key, os.getenv("BITGET_API_SECRET", ""), os.getenv("BITGET_PASSPHRASE", ""))
    ex = LiveEx(client) if LIVE else DryEx(client)
    ex.load_contracts()
    trader = Trader(ex)
    log(f"시작 모드={MODE_TAG} 증거금={MARGIN} 레버리지={LEV} 시간대={sorted(TFS)} 상한={MAX_POS} 계약 {len(ex.contracts)}개")
    tg(f"[{MODE_TAG}] 자동매매 봇 시작 (증거금 {MARGIN} USDT x {LEV}배, 시간대 {','.join(sorted(TFS))})")
    n = 0
    last_err = 0
    while True:
        try:
            trader.step(n)
        except Exception as e:
            log(f"[에러] {type(e).__name__}: {e}")
            if time.time() - last_err > 600:
                tg(f"[{MODE_TAG}] 봇 오류: {type(e).__name__}: {e}")
                last_err = time.time()
        n += 1
        time.sleep(POLL)


if __name__ == "__main__":
    main()
