#!/usr/bin/env bash
# Logical backup: pg_dump | gzip | S3. Runs from cron on the EC2 host.
# Exits quietly when no backup bucket is configured.
set -euo pipefail

ENV_FILE=/etc/kaupo/kaupo.env
set -a
# shellcheck source=/dev/null
. "$ENV_FILE"
set +a

if [[ -z "${KAUPO_BACKUP_BUCKET:-}" ]]; then
  echo "KAUPO_BACKUP_BUCKET is not set; skipping backup"
  exit 0
fi

stamp=$(date -u +%Y%m%dT%H%M%SZ)
cd /opt/kaupo
docker compose --env-file "$ENV_FILE" -f deploy/compose.prod.yml exec -T db \
  pg_dump -U kaupo -d kaupo --no-owner --no-privileges \
  | gzip \
  | aws s3 cp - "s3://$KAUPO_BACKUP_BUCKET/pgdump/kaupo-$stamp.sql.gz"
echo "uploaded pgdump/kaupo-$stamp.sql.gz"
