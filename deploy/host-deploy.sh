#!/usr/bin/env bash
# Pull code, strategies, and images, then restart the stack. Idempotent.
# The deploy workflow runs this over SSH. Usage: host-deploy.sh [image-tag]
set -euo pipefail

TAG=${1:-latest}
REPO_DIR=/opt/kaupo
STRATEGIES_DIR=/opt/kaupo-strategies
ENV_FILE=/etc/kaupo/kaupo.env

if [[ ! -f "$ENV_FILE" ]]; then
  echo "$ENV_FILE is missing. The deploy workflow writes it from GitHub secrets."
  exit 1
fi

cd "$REPO_DIR"
git pull --ff-only

# Private strategies. Works once the host deploy key is added to the repository.
# The tree hash of strategies/ changes exactly when strategy code changes.
# Memory and docs commits must not restart the shadow run.
strategies_tree() { git -C "$STRATEGIES_DIR" rev-parse HEAD:strategies 2>/dev/null || echo none; }
before=$(strategies_tree)
if git ls-remote git@github.com:nemecec/kaupo-strategies.git > /dev/null 2>&1; then
  if [[ -d "$STRATEGIES_DIR/.git" ]]; then
    git -C "$STRATEGIES_DIR" pull --ff-only
  else
    git clone git@github.com:nemecec/kaupo-strategies.git "$STRATEGIES_DIR"
  fi
else
  echo "strategies repository not reachable; keeping the current strategies"
fi
after=$(strategies_tree)

current_tag=$(grep '^KAUPO_TAG=' "$ENV_FILE" | cut -d= -f2- || true)

set_env() { # key value — replace the line or append it
  if grep -q "^$1=" "$ENV_FILE"; then
    sed -i "s|^$1=.*|$1=$2|" "$ENV_FILE"
  else
    echo "$1=$2" >> "$ENV_FILE"
  fi
}
set_env KAUPO_TAG "$TAG"
if [[ -d "$STRATEGIES_DIR/.git" ]]; then
  # strategies live in the strategies/ subdirectory of the repository
  set_env KAUPO_STRATEGIES_HOST_DIR "$STRATEGIES_DIR/strategies"
fi

compose() {
  docker compose --env-file "$ENV_FILE" -f deploy/compose.prod.yml --profile trading "$@"
}

# A rebuild re-pushes the same tag with a fresh digest. Pulling it makes
# compose recreate every container for no content change. Pull only on a
# real tag change; `up -d` is a no-op when nothing changed.
if [[ "$TAG" != "$current_tag" ]]; then
  compose pull
else
  echo "image tag unchanged ($TAG); skipping pull"
fi
compose up -d --remove-orphans
if [[ "$before" != "none" && "$before" != "$after" ]]; then
  echo "strategy code changed ($before -> $after); restarting shadow"
  compose restart shadow
fi
systemctl enable kaupo.service
docker image prune -f
compose ps
