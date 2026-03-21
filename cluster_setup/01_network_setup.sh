#!/bin/bash
# SmolClaw Cluster — Network Setup
# Run this ON each new node (nizbot3, nizbot0) with a keyboard+monitor attached.
#
# Usage:
#   On nizbot3:  sudo ./01_network_setup.sh nizbot3 10.0.0.3
#   On MacBook:  sudo ./01_network_setup.sh nizbot0 10.0.0.10

set -e

NODE_NAME="${1:?Usage: $0 <hostname> <ip_address>}"
NODE_IP="${2:?Usage: $0 <hostname> <ip_address>}"

echo "=== SmolClaw Cluster Network Setup ==="
echo "  Hostname: $NODE_NAME"
echo "  Static IP: $NODE_IP/24"
echo ""

# 1. Set hostname
echo "[1/4] Setting hostname to $NODE_NAME..."
hostnamectl set-hostname "$NODE_NAME"

# 2. Find the ethernet interface
ETH_IF=$(ip -o link show | awk -F': ' '{print $2}' | grep -E '^en|^eth' | head -1)
if [ -z "$ETH_IF" ]; then
    echo "ERROR: No ethernet interface found!"
    ip link show
    exit 1
fi
echo "[2/4] Found ethernet interface: $ETH_IF"

# 3. Create NetworkManager connection with static IP
echo "[3/4] Creating 'nuc-cluster' connection on $ETH_IF..."
# Remove existing if any
nmcli con delete nuc-cluster 2>/dev/null || true
nmcli con add \
    con-name "nuc-cluster" \
    type ethernet \
    ifname "$ETH_IF" \
    ipv4.method manual \
    ipv4.addresses "$NODE_IP/24" \
    ipv6.method disabled

# Bring it up
nmcli con up nuc-cluster

# 4. Add cluster hosts to /etc/hosts
echo "[4/4] Adding cluster hosts to /etc/hosts..."
grep -q "10.0.0.1.*nizbot1" /etc/hosts || echo "10.0.0.1  nizbot1" >> /etc/hosts
grep -q "10.0.0.2.*nizbot2" /etc/hosts || echo "10.0.0.2  nizbot2" >> /etc/hosts
grep -q "10.0.0.3.*nizbot3" /etc/hosts || echo "10.0.0.3  nizbot3" >> /etc/hosts
grep -q "10.0.0.10.*nizbot0" /etc/hosts || echo "10.0.0.10 nizbot0" >> /etc/hosts

echo ""
echo "=== Done! ==="
ip addr show "$ETH_IF" | grep "inet "
echo ""
echo "Test: ping 10.0.0.1 (should reach nizbot1)"
ping -c 2 10.0.0.1 || echo "Can't reach nizbot1 — check cable"
