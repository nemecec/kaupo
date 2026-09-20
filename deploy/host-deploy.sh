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

# Only the explicit platform pin reaches trading containers. Agent commits on
# strategies/main cannot change production through an unrelated engine deploy.
STRATEGY_REF=$(tr -d '\r\n' < deploy/strategies-ref)
if [[ ! "$STRATEGY_REF" =~ ^[0-9a-f]{40}$ ]]; then
  echo "deploy/strategies-ref must contain a full commit SHA" >&2
  exit 1
fi
if [[ ! -d "$STRATEGIES_DIR/.git" ]]; then
  git clone --no-checkout git@github.com:nemecec/kaupo-strategies.git "$STRATEGIES_DIR"
fi
git -C "$STRATEGIES_DIR" fetch origin "$STRATEGY_REF"
release_dir="/opt/kaupo-strategy-releases/$STRATEGY_REF"
if [[ ! -d "$release_dir/strategies" ]]; then
  mkdir -p /opt/kaupo-strategy-releases
  stage_dir=$(mktemp -d /opt/kaupo-strategy-releases/.stage.XXXXXX)
  git -C "$STRATEGIES_DIR" archive "$STRATEGY_REF" strategies | tar -x -C "$stage_dir"
  mv "$stage_dir" "$release_dir"
fi

TAG_FILE=/etc/kaupo/deployed-tag
# The workflow rewrites the env file on every deploy, so the tag of the last
# successful deploy lives in its own file, not in the env file.
current_tag=$(cat "$TAG_FILE" 2>/dev/null || true)

set_env() { # key value — replace the line or append it
  if grep -q "^$1=" "$ENV_FILE"; then
    sed -i "s|^$1=.*|$1=$2|" "$ENV_FILE"
  else
    echo "$1=$2" >> "$ENV_FILE"
  fi
}
TRADING_REF=$(tr -d '\r\n' < deploy/trading-ref)
if [[ ! "$TRADING_REF" =~ ^[0-9a-f]{40}$ ]]; then
  echo "deploy/trading-ref must contain a full commit SHA" >&2
  exit 1
fi
set_env KAUPO_TRADING_TAG "$TRADING_REF"
set_env KAUPO_TAG "$TAG"
set_env KAUPO_STRATEGIES_HOST_DIR "$release_dir/strategies"

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
# Dead one-off containers (manual `compose run` diagnostics) hold service names
# hostage and break `up -d` with a docker name conflict (kaupo#32)
docker container prune -f --filter "label=com.docker.compose.project=kaupo"
compose up -d --remove-orphans
echo "$TAG" > "$TAG_FILE"
systemctl enable kaupo.service
# On the containerd image store a plain `prune -f` misses the old tagged
# deploy digests and the disk fills over weeks (the 2026-09-11 outage);
# `-a` keeps only what running containers use
docker image prune -a -f
compose ps
