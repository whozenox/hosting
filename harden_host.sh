#!/usr/bin/env bash
# Made by @Dev_Null_X — host firewall for hosted-bot containers (v5.2)
# Run ONCE as root on the Docker host (VPS) and again after each reboot
# (systemd unit / rc.local). Needs: iptables. Safe to re-run (idempotent).
#
# Hosted bots keep normal internet access (Telegram API etc.) but can NOT:
#   - reach the cloud metadata service (169.254.169.254 -> steals cloud keys)
#   - scan/attack your LAN, the host or other private ranges
#   - send spam via SMTP (25/465/587)
#   - open hundreds of connections (port-scan / DDoS tools) or flood UDP
# SUBNET must equal config.CONTAINER_SUBNET.
set -euo pipefail
SUBNET="${1:-172.29.0.0/16}"
CHAIN=HOSTEDBOT-FW

iptables -N "$CHAIN" 2>/dev/null || true
iptables -F "$CHAIN"                       # only OUR chain is flushed

# 1) traffic inside the bot subnet (icc is disabled by Docker anyway)
iptables -A "$CHAIN" -d "$SUBNET" -j RETURN
# 2) cloud metadata: always blocked
iptables -A "$CHAIN" -d 169.254.169.254 -j DROP
# 3) DNS must keep working (resolvers are often on private IPs)
iptables -A "$CHAIN" -p udp --dport 53 -j RETURN
iptables -A "$CHAIN" -p tcp --dport 53 -j RETURN
# 4) no access to private / link-local / CGNAT ranges
for NET in 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 169.254.0.0/16 100.64.0.0/10; do
  iptables -A "$CHAIN" -d "$NET" -j DROP
done
# 5) no outbound mail (spam)
iptables -A "$CHAIN" -p tcp -m multiport --dports 25,465,587 -j DROP
# 6) connection / flood limits per container
iptables -A "$CHAIN" -p tcp --syn -m connlimit --connlimit-above 150 --connlimit-mask 32 -j DROP
iptables -A "$CHAIN" -p udp -m hashlimit --hashlimit-above 400/sec \
         --hashlimit-mode srcip --hashlimit-name botudp -j DROP
iptables -A "$CHAIN" -j RETURN

# hook into DOCKER-USER (evaluated before Docker's own forward rules)
iptables -N DOCKER-USER 2>/dev/null || true
iptables -C DOCKER-USER -s "$SUBNET" -j "$CHAIN" 2>/dev/null || iptables -I DOCKER-USER 1 -s "$SUBNET" -j "$CHAIN"

echo "OK — firewall active for $SUBNET:"; iptables -S "$CHAIN"
