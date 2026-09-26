#!/bin/bash
# One-shot setup for a fresh Ubuntu 24.04 or Debian 13 server.
# Installs Docker from Docker's official repo so we get docker-compose-plugin.

set -e

echo "==> Updating apt index"
apt-get update -qq

echo "==> Installing prerequisites"
apt-get install -y -qq ca-certificates curl gnupg ufw

echo "==> Adding Docker's official repo"
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
ARCH=$(dpkg --print-architecture)
CODENAME=$(. /etc/os-release && echo "$VERSION_CODENAME")
DISTRO=$(. /etc/os-release && echo "$ID")
echo "deb [arch=${ARCH} signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/${DISTRO} ${CODENAME} stable" > /etc/apt/sources.list.d/docker.list
apt-get update -qq

echo "==> Installing Docker engine + compose plugin"
apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker

echo "==> Configuring firewall (SSH + panel)"
ufw allow 22/tcp >/dev/null
ufw allow 8080/tcp >/dev/null
ufw --force enable

echo "==> Building and starting the bot"
cd "$(dirname "$0")"
docker compose up -d --build

echo
echo "==> Done. Status:"
docker compose ps
echo
PUBIP=$(curl -s ifconfig.me || echo "<your-server-ip>")
echo "Panel: http://${PUBIP}:8080"
echo "Logs:  docker compose logs -f"
