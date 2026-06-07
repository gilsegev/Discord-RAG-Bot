#!/usr/bin/env bash
set -euo pipefail

if [[ $EUID -eq 0 ]]; then
  echo "Run this as the normal opc/ubuntu user, not root."
  exit 1
fi

echo "== Updating packages =="
sudo apt-get update
sudo apt-get install -y ca-certificates curl gnupg git openssl

if ! command -v docker >/dev/null 2>&1; then
  echo "== Installing Docker =="
  sudo install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
    | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  sudo chmod a+r /etc/apt/keyrings/docker.gpg
  . /etc/os-release
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
    | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
  sudo apt-get update
  sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
else
  echo "== Docker already installed =="
fi

sudo usermod -aG docker "$USER"

echo "== Creating .env if missing =="
if [[ ! -f .env ]]; then
  cp .env.example .env
  POSTGRES_PASSWORD="$(openssl rand -hex 32)"
  N8N_ENCRYPTION_KEY="$(openssl rand -hex 32)"
  N8N_BASIC_AUTH_PASSWORD="$(openssl rand -base64 32 | tr -d '\n')"
  PHOENIX_ADMIN_PASSWORD="$(openssl rand -base64 32 | tr -d '\n')"
  sed -i "s|replace_with_a_long_random_url_safe_password|${POSTGRES_PASSWORD}|1" .env
  sed -i "s|replace_with_32_plus_random_chars|${N8N_ENCRYPTION_KEY}|1" .env
  sed -i "s|replace_with_a_long_random_password|${N8N_BASIC_AUTH_PASSWORD}|1" .env
  sed -i "s|replace_with_a_long_random_password|${PHOENIX_ADMIN_PASSWORD}|1" .env
  echo "Created deploy/phase0/.env with generated passwords."
else
  echo ".env already exists; leaving it unchanged."
fi

echo "== Starting Phase 0 services =="
docker compose pull
docker compose up -d

echo "== Service status =="
docker compose ps

echo
echo "Phase 0 started. If this is your first Docker install, log out and back in"
echo "so your shell gets Docker group membership, then rerun:"
echo "  cd ~/Discord-RAG-Bot/deploy/phase0 && docker compose ps"
