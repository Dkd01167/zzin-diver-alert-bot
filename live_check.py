"""실계좌 API 키 점검 (읽기 전용 — 주문 없음).
사용: cd ~/zzin-alert && set -a && . ./.env && . ./.env.live && set +a && python3 live_check.py
"""
import os
import sys

import zzin_trader as zt

for k in ("BITGET_API_KEY", "BITGET_API_SECRET", "BITGET_PASSPHRASE"):
    if not os.environ.get(k):
        print(f"{k} 가 설정되어 있지 않습니다. .env.live 를 확인하세요.")
        sys.exit(1)

c = zt.Client(os.environ["BITGET_API_KEY"], os.environ["BITGET_API_SECRET"], os.environ["BITGET_PASSPHRASE"])
ex = zt.LiveEx(c)
ex.load_contracts()
print(f"계약 {len(ex.contracts)}개 로드 (공개 API OK)")
try:
    print(f"선물 지갑 사용 가능 잔고: {ex.balance():.2f} USDT   <- 서명/키/IP 허용이 정상이면 여기까지 나옵니다")
except zt.BitgetError as e:
    print("잔고 조회 실패:", e)
    print("  -> 키/시크릿/패스프레이즈 오타, 권한, IP 허용 목록(서버 IP)을 확인하세요.")
    sys.exit(2)
pos = ex.positions()
print(f"현재 열린 포지션: {len(pos)}개", pos if pos else "")
for sym in ("BTCUSDT", "XRPUSDT"):
    try:
        a = c.call("GET", "/api/v2/mix/account/account",
                   {"symbol": sym, "productType": "USDT-FUTURES", "marginCoin": "USDT"})
        print(f"{sym}: 포지션 모드={a.get('posMode')} 마진 모드={a.get('marginMode')} 자산 모드={a.get('assetMode')}")
    except zt.BitgetError as e:
        print(f"{sym} 계정 조회 실패:", e)
print("점검 완료. 주문은 하지 않았습니다.")
