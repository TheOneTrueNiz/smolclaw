#!/bin/bash
# SmolClaw Cluster — MacBook Head Node Setup
# Run this on nizbot0 (MacBook Air) after 01_network_setup.sh
# Sets up SSH keys, SmolClaw agent, and cluster tools
#
# Usage: ./03_macbook_head_setup.sh

set -e

echo "=== SmolClaw MacBook Head Node Setup ==="
echo ""

# 1. Generate SSH key if needed
echo "[1/5] SSH key setup..."
if [ ! -f ~/.ssh/id_ed25519 ]; then
    ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519 -N "" -C "nizbot0-cluster"
    echo "  Generated new SSH key"
else
    echo "  SSH key already exists"
fi

# 2. Copy SSH key to all NUCs
echo "[2/5] Distributing SSH key to NUCs..."
for node in nizbot1@10.0.0.1 nizbot2@10.0.0.2 nizbot3@10.0.0.3; do
    echo "  Copying to $node..."
    ssh-copy-id -o ConnectTimeout=5 "$node" 2>/dev/null && echo "    OK" || echo "    SKIP (not reachable yet)"
done

# 3. Install SmolClaw agent
echo "[3/5] Installing SmolClaw..."
mkdir -p ~/smolclaw
cd ~/smolclaw

# Copy agent.py from nizbot1
scp nizbot1@10.0.0.1:~/smolclaw/agent.py ./agent.py
scp nizbot1@10.0.0.1:~/smolclaw/test_harness.py ./test_harness.py 2>/dev/null || true
scp nizbot1@10.0.0.1:~/smolclaw/flight_analysis.py ./flight_analysis.py 2>/dev/null || true

echo "  Copied SmolClaw files"

# 4. Configure agent for head node
# MacBook doesn't run llama-server — it talks to NUCs
echo "[4/5] Configuring for head node..."
mkdir -p ~/.config/smolclaw

# Brave API key — copy from nizbot1 or prompt
if [ ! -f ~/.config/smolclaw/brave.key ]; then
    scp nizbot1@10.0.0.1:~/.config/smolclaw/brave.key ~/.config/smolclaw/brave.key 2>/dev/null || {
        echo "  Enter your Brave API key (or press Enter to skip):"
        read -r key
        if [ -n "$key" ]; then
            echo "$key" > ~/.config/smolclaw/brave.key
        fi
    }
    chmod 600 ~/.config/smolclaw/brave.key 2>/dev/null || true
fi

# 5. Create cluster management script
echo "[5/5] Creating cluster tools..."
cat > ~/smolclaw/cluster_status.sh << 'STATUSEOF'
#!/bin/bash
# SmolClaw Cluster Status
echo "╔══════════════════════════════════════╗"
echo "║  SmolClaw Cluster Status             ║"
echo "╚══════════════════════════════════════╝"
echo ""

for node in "nizbot1:10.0.0.1:nizbot1" "nizbot2:10.0.0.2:nizbot2" "nizbot3:10.0.0.3:nizbot3"; do
    IFS=':' read -r name ip user <<< "$node"
    printf "  %-10s " "$name"
    if ping -c 1 -W 1 "$ip" &>/dev/null; then
        printf "UP   "
        # Check llama-server
        health=$(curl -s --max-time 3 "http://$ip:8090/health" 2>/dev/null)
        if echo "$health" | grep -q '"ok"'; then
            printf "llama-server: ONLINE"
        else
            printf "llama-server: OFFLINE"
        fi
        # Get load
        load=$(ssh -o ConnectTimeout=2 "$user@$ip" "uptime | sed 's/.*load average: //' | cut -d, -f1" 2>/dev/null)
        [ -n "$load" ] && printf "  load: $load"
    else
        printf "DOWN"
    fi
    echo ""
done
echo ""
STATUSEOF
chmod +x ~/smolclaw/cluster_status.sh

echo ""
echo "=== Done! ==="
echo ""
echo "Usage:"
echo "  python3 ~/smolclaw/agent.py              # Run SmolClaw"
echo "  ~/smolclaw/cluster_status.sh              # Check cluster health"
echo "  ssh nizbot1@10.0.0.1                      # SSH to NUC1"
echo "  ssh nizbot2@10.0.0.2                      # SSH to NUC2"
echo "  ssh nizbot3@10.0.0.3                      # SSH to NUC3"
