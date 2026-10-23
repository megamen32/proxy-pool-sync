#!/bin/bash
# Локальный stdio-обёртка: запускает proxy5-registry MCP на server-100 по SSH.
# Реестр и кабинет живут там; отсюда — только транспорт.
exec ssh -o BatchMode=yes -o ConnectTimeout=15 -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
    roomhacker@104.bezrabotnyi.com \
    'cd /home/roomhacker/PycharmProjects/proxy-pool-sync && \
     PYTHONPATH=/home/roomhacker/PycharmProjects/proxy-pool-sync/src \
     PROXY5_INVENTORY_DB=/var/lib/proxy5-registry/inventory.db \
     PROXY5_GEOIP_DB=/home/roomhacker/.cloakbrowser/geoip/GeoLite2-City.mmdb \
     PROXY5_HOPS_FILE=/home/roomhacker/PycharmProjects/video_watching/scripts/ops/proxy5_hops.py \
     PROXY5_IPS_EGRESS=socks5h://127.0.0.1:30881 \
     exec /opt/proxy-pool-sync/bin/python -m proxy_pool_sync.proxy5.service'
