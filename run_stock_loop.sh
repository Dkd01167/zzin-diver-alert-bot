#!/bin/bash
# 본주 차트 기준 찐다이버 알림 루프: 매시 7/22/37/52분에 스캔
cd /home/ubuntu/zzin-alert || exit 1
while true; do
  python3 zzin_stock_alert.py >> zzin_stock.log 2>&1 || echo "[$(date -u)] stock scanner error" >> zzin_stock.log
  if [ "$(stat -c%s zzin_stock.log 2>/dev/null || echo 0)" -gt 5000000 ]; then
    tail -c 1000000 zzin_stock.log > zzin_stock.log.tmp && mv zzin_stock.log.tmp zzin_stock.log
  fi
  s=$(date +%s); m=$(( (s/60) % 60 )); sec=$(( s % 60 )); wait=0
  for slot in 7 22 37 52 67; do
    if [ "$m" -lt "$slot" ]; then wait=$(( (slot - m) * 60 - sec )); break; fi
  done
  sleep "$wait"
done
