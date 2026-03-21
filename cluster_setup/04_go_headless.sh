#!/bin/bash
# SmolClaw Cluster — Go Headless (NUCs only!)
# Disables the GUI on a NUC to save ~800MB RAM and CPU cycles.
# DO NOT run this on the MacBook (nizbot0) — that's your GUI terminal!
#
# Usage: sudo ./04_go_headless.sh
# Revert: sudo systemctl set-default graphical.target && sudo reboot

set -e

HOSTNAME=$(hostname)

# Safety check — don't headless the MacBook
if [[ "$HOSTNAME" == "nizbot0" ]] || [[ "$HOSTNAME" == *"macbook"* ]]; then
    echo "ERROR: Don't run this on the MacBook! It's your GUI terminal."
    exit 1
fi

echo "=== Going Headless: $HOSTNAME ==="
echo "This will disable the desktop GUI on next reboot."
echo "You'll manage this node via SSH from nizbot0."
echo ""
read -p "Continue? (y/n) " -n 1 -r
echo ""
[[ ! $REPLY =~ ^[Yy]$ ]] && exit 0

# Switch to multi-user (text-only) target
sudo systemctl set-default multi-user.target

echo ""
echo "Done! On next reboot, $HOSTNAME will boot to text-only mode."
echo "To revert: sudo systemctl set-default graphical.target"
echo ""
echo "Reboot now? (y/n)"
read -p "" -n 1 -r
echo ""
[[ $REPLY =~ ^[Yy]$ ]] && sudo reboot
