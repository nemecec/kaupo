#!/usr/bin/env bash
# One-time host setup for kaupo on Hetzner CX23 (Ubuntu 24.04). Idempotent.
# The deploy workflow runs this automatically on the first deploy.
# Manual use: scp to the server, then run as root: bash bootstrap.sh
set -euxo pipefail

REPO=https://github.com/nemecec/kaupo.git
REPO_DIR=/opt/kaupo

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y docker.io docker-compose-v2 git jq curl ca-certificates awscli cron
systemctl enable --now docker
systemctl enable --now cron

# SSH: key authentication only. Ubuntu cloud images can set
# PasswordAuthentication in sshd_config.d, which wins over sshd_config.
shopt -s nullglob
configs=(/etc/ssh/sshd_config /etc/ssh/sshd_config.d/*.conf)
if [[ ${#configs[@]} -gt 0 ]]; then
  sed -i -e 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' "${configs[@]}"
fi
systemctl reload ssh

# Application source (public repository)
[[ -d "$REPO_DIR/.git" ]] || git clone "$REPO" "$REPO_DIR"

install -d -m 755 /etc/kaupo
install -d -m 700 /root/.ssh

# Key for the private strategies repository. Add the printed public key as a
# deploy key in kaupo-strategies. host-deploy.sh clones it once the key works.
[[ -f /root/.ssh/kaupo-strategies ]] \
  || ssh-keygen -t ed25519 -N '' -C kaupo-strategies-deploy -f /root/.ssh/kaupo-strategies
cat > /root/.ssh/config <<'EOF'
Host github.com
  IdentityFile /root/.ssh/kaupo-strategies
  StrictHostKeyChecking accept-new
EOF
chmod 600 /root/.ssh/config

# Start the stack on every boot. host-deploy.sh enables it after the first deploy.
cat > /etc/systemd/system/kaupo.service <<'EOF'
[Unit]
Description=kaupo trading stack
Requires=docker.service
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/opt/kaupo
ExecStart=/usr/bin/docker compose --env-file /etc/kaupo/kaupo.env -f deploy/compose.prod.yml --profile trading up -d
ExecStop=/usr/bin/docker compose --env-file /etc/kaupo/kaupo.env -f deploy/compose.prod.yml down
TimeoutStartSec=300

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload

# Nightly backup at 03:17 UTC, daily summary at 06:47 UTC
cat > /etc/cron.d/kaupo-backup <<'EOF'
17 3 * * * root /opt/kaupo/deploy/backup.sh >> /var/log/kaupo-backup.log 2>&1
47 6 * * * root /opt/kaupo/deploy/notify-daily.sh >> /var/log/kaupo-notify.log 2>&1
EOF

# Kraken rolling-window refresh (1d + 4h candles, trade ticks) at 04:41 UTC (venue-check data stays fresh)
cat > /etc/cron.d/kaupo-refresh <<'EOF'
41 4 * * * root /opt/kaupo/deploy/refresh-kraken.sh >> /var/log/kaupo-refresh.log 2>&1
EOF

echo
echo "Bootstrap done. Add this deploy key to the kaupo-strategies repository:"
cat /root/.ssh/kaupo-strategies.pub
