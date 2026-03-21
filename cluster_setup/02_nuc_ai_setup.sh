#!/bin/bash
# SmolClaw Cluster — NUC AI Server Setup
# Run this on nizbot3 (after 01_network_setup.sh)
# Sets up llama.cpp + SmolLM3 model + systemd service
#
# Usage: ./02_nuc_ai_setup.sh

set -e

HOSTNAME=$(hostname)
USER=$(whoami)
HOME_DIR=$(eval echo ~$USER)

echo "=== SmolClaw AI Server Setup ==="
echo "  Host: $HOSTNAME"
echo "  User: $USER"
echo "  Home: $HOME_DIR"
echo ""

# 1. Install build dependencies
echo "[1/6] Installing build dependencies..."
sudo apt update -qq
sudo apt install -y -qq build-essential cmake git

# 2. Clone and build llama.cpp
echo "[2/6] Building llama.cpp..."
cd "$HOME_DIR"
if [ -d "llama.cpp" ]; then
    echo "  llama.cpp already exists, pulling latest..."
    cd llama.cpp && git pull
else
    git clone https://github.com/ggml-org/llama.cpp.git
    cd llama.cpp
fi
cmake -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build --config Release -j$(nproc)
echo "  Built: $(ls -la build/bin/llama-server)"

# 3. Download model
echo "[3/6] Downloading SmolLM3-3B Q4_K_M..."
mkdir -p "$HOME_DIR/models"
cd "$HOME_DIR/models"
if [ -f "SmolLM3-Q4_K_M.gguf" ]; then
    echo "  Model already exists ($(du -h SmolLM3-Q4_K_M.gguf | cut -f1))"
else
    wget -q --show-progress \
        "https://huggingface.co/bartowski/HuggingFaceTB-SmolLM3-3B-GGUF/resolve/main/HuggingFaceTB-SmolLM3-3B-Q4_K_M.gguf" \
        -O SmolLM3-Q4_K_M.gguf
    echo "  Downloaded: $(du -h SmolLM3-Q4_K_M.gguf | cut -f1)"
fi

# 4. Download chat template
echo "[4/6] Setting up chat template..."
mkdir -p "$HOME_DIR/models/smollm3-config"
cd "$HOME_DIR/models/smollm3-config"
if [ -f "chat_template.jinja" ]; then
    echo "  Chat template already exists"
else
    wget -q --show-progress \
        "https://huggingface.co/HuggingFaceTB/SmolLM3-3B/resolve/main/chat_template.jinja" \
        -O chat_template.jinja
    echo "  Downloaded chat template"
fi

# 5. Create systemd user service
echo "[5/6] Creating systemd service..."
mkdir -p "$HOME_DIR/.config/systemd/user"
cat > "$HOME_DIR/.config/systemd/user/smollm3.service" << EOF
[Unit]
Description=SmolLM3-3B llama-server ($HOSTNAME)
After=network.target

[Service]
Type=simple
ExecStart=$HOME_DIR/llama.cpp/build/bin/llama-server \\
    --model $HOME_DIR/models/SmolLM3-Q4_K_M.gguf \\
    --host 0.0.0.0 --port 8090 --ctx-size 8192 --parallel 1 --threads 4 \\
    --jinja --chat-template-file $HOME_DIR/models/smollm3-config/chat_template.jinja
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable smollm3
systemctl --user start smollm3

# 6. Enable lingering (service runs without login)
echo "[6/6] Enabling user lingering..."
sudo loginctl enable-linger "$USER"

echo ""
echo "=== Done! ==="
echo "Service status:"
systemctl --user status smollm3 --no-pager | head -5
echo ""
echo "Health check:"
sleep 3
curl -s http://localhost:8090/health | python3 -m json.tool 2>/dev/null || echo "Still starting up... wait a few seconds and try: curl http://localhost:8090/health"
