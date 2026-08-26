# Deploy to production (Hetzner)

The production stack runs on one Hetzner CX23 server. Caddy serves `https://kaupo.trade` and manages the certificates. Images come from GHCR. GitHub Actions deploys over SSH after a green CI run on `main`. A nightly `pg_dump` goes to AWS S3 as the cross-provider copy.

## Monthly cost (ex-VAT)

- CX23 with IPv4: 5.99 EUR
- Automated whole-disk backups: 1.10 EUR (20 percent of the server price)
- S3 storage for the dumps: under 0.10 EUR
- Total: about 7.20 EUR

## One-time setup

### 1. Server

The CX23 with Ubuntu 24.04 exists. Do two more things in the Hetzner cloud console:

1. Enable backups: open the server, then Backups, then Enable. This gives seven daily whole-disk slots.
2. Add a firewall: Firewalls, then Create. Allow inbound TCP on ports 22, 80, and 443 from `0.0.0.0/0` and `::/0`. Apply the firewall to the server.

### 2. DNS

At the `kaupo.trade` registrar, create two records:

- `A` record: `kaupo.trade` points to the server IPv4
- `A` record: `www` points to the server IPv4

### 3. Deploy key for GitHub Actions

On your machine, create a key pair and authorize it on the server:

```bash
ssh-keygen -t ed25519 -N '' -f ~/.ssh/kaupo-hetzner-deploy
ssh-copy-id -i ~/.ssh/kaupo-hetzner-deploy.pub root@<server-ip>
```

`ssh-copy-id` uses your existing key. The deploy key is for the GitHub Actions runner only.

### 4. AWS backup bucket

Create the bucket for the nightly dumps:

```bash
aws s3 mb s3://kaupo-backups-<suffix> --region eu-north-1
```

New buckets block public access by default. Enable versioning so a leaked backup key cannot destroy old dumps. Then add a 30-day expiry:

```bash
aws s3api put-bucket-versioning --bucket kaupo-backups-<suffix> \
  --versioning-configuration Status=Enabled
cat > /tmp/lifecycle.json <<'EOF'
{
  "Rules": [
    {
      "ID": "expire-pgdumps",
      "Status": "Enabled",
      "Prefix": "pgdump/",
      "Expiration": {"Days": 30},
      "NoncurrentVersionExpiration": {"NoncurrentDays": 30},
      "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7}
    }
  ]
}
EOF
aws s3api put-bucket-lifecycle --bucket kaupo-backups-<suffix> \
  --lifecycle-configuration file:///tmp/lifecycle.json
```

Create an IAM user with an access key, scoped to this bucket only:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:PutObject", "s3:GetObject"],
      "Resource": "arn:aws:s3:::kaupo-backups-<suffix>/pgdump/*"
    },
    {
      "Effect": "Allow",
      "Action": ["s3:ListBucket"],
      "Resource": "arn:aws:s3:::kaupo-backups-<suffix>"
    }
  ]
}
```

### 5. GitHub secrets and variables

Generate two API tokens. Save them in your password manager. You need the read-only token to open the dashboard:

```bash
openssl rand -hex 32
```

Then set the secrets and variables. Run these in the `kaupo` repository:

```bash
gh secret set HETZNER_SSH_PRIVATE_KEY < ~/.ssh/kaupo-hetzner-deploy
gh secret set POSTGRES_PASSWORD -b "$(openssl rand -hex 32)"
gh secret set KAUPO_ADMIN_TOKEN -b "<admin-token>"
gh secret set KAUPO_READONLY_TOKEN -b "<readonly-token>"
gh secret set AWS_ACCESS_KEY_ID -b "<access-key-id>"
gh secret set AWS_SECRET_ACCESS_KEY -b "<secret-access-key>"
gh variable set HETZNER_HOST -b "<server-ip>"
gh variable set AWS_REGION -b "eu-north-1"
gh variable set KAUPO_BACKUP_BUCKET -b "kaupo-backups-<suffix>"
gh secret set KAUPO_NTFY_TOPIC -b "kaupo-$(openssl rand -hex 6)"
```

The database password never leaves this chain. You never type it yourself.

### 6. First deploy

```bash
gh workflow run deploy.yml
```

The workflow builds the images, bootstraps the host, writes the secrets, and starts the stack. Later deploys run automatically after each green CI run on `main`.

### 7. Make the GHCR packages public

The host pulls images without credentials. After the first deploy:

1. Open `github.com/nemecec?tab=packages`.
2. Open `kaupo`, then Package settings, then Change visibility, then Public.
3. Do the same for `kaupo-ui`.

### 8. Private strategies

The bootstrap step printed a public key for the host. Find it in the workflow log of the first deploy. Add it in the `kaupo-strategies` repository under Settings, then Deploy keys.

The next deploy clones the repository to `/opt/kaupo-strategies` and mounts it into the containers. Until then the stack uses the bundled example strategies.

### 9. Verify

1. Open `https://kaupo.trade`. Enter the read-only token.
2. Check the containers on the host:

```bash
ssh root@<server-ip> 'docker compose --env-file /etc/kaupo/kaupo.env -f /opt/kaupo/deploy/compose.prod.yml ps'
```

## Operations

Set a shell shortcut for the compose commands below:

```bash
COMPOSE="docker compose --env-file /etc/kaupo/kaupo.env -f /opt/kaupo/deploy/compose.prod.yml"
```

- Deploy: automatic after a green CI run on `main`. Manual: `gh workflow run deploy.yml`.
- Run assignments: the `run_assignments` table is the desired set of shadow runs. The `supervisor` service starts, stops, and restarts runs to match the enabled rows. Manage the rows through the API with the admin token: `GET` and `POST /api/v1/assignments`, `PUT` and `DELETE /api/v1/assignments/{id}` (DELETE disables the row). The migration seeds the two runs the old stack ran: `primary` (strategy, pair, and timeframe from the settings table) and `sol-4h` (`sma-cross` on SOL/EUR 4h). `PUT /api/v1/settings` still works and updates the `primary` row. A run stopped with the kill switch stays down until a `resume` control command or an assignment update.
- Update strategies: push to the `kaupo-strategies` main branch. The next deploy pulls them and restarts the supervisor container only when strategy code changed. Memory and docs commits trigger nothing. To apply changes now, run `/opt/kaupo/deploy/host-deploy.sh` on the host.
- Logs: `$COMPOSE logs -f supervisor` on the host. Replace `supervisor` with `api` or `db`.
- Backup log: `/var/log/kaupo-backup.log` on the host.
- Alerts (ntfy): the topic name is in `/etc/kaupo/kaupo.env` on the host. Subscribe to it in the ntfy app. A daily summary posts at 06:47 UTC. Halts, kill-switch use, strategy switches, and agent events post immediately.
- Reboots: `kaupo.service` starts the stack on boot.

## Restore from a backup

1. Download and unpack a dump on your machine:

   ```bash
   aws s3 cp s3://kaupo-backups-<suffix>/pgdump/kaupo-<stamp>.sql.gz - | gunzip > restore.sql
   ```

2. Copy it to the host:

   ```bash
   scp restore.sql root@<server-ip>:/tmp/
   ```

3. On the host, stop the writers and load the dump into a fresh database:

   ```bash
   $COMPOSE stop api supervisor backtest-worker
   $COMPOSE exec db psql -U kaupo -d postgres -c 'DROP DATABASE kaupo;'
   $COMPOSE exec db psql -U kaupo -d postgres -c 'CREATE DATABASE kaupo;'
   $COMPOSE exec -T db psql -U kaupo -d kaupo < /tmp/restore.sql
   $COMPOSE up -d
   ```

## Moving to ECS later

Every service is already a container, and the images already live in a registry. The migration is: push the images to ECR, restore the latest dump into RDS, and write ECS task definitions for `api`, `supervisor`, `backtest-worker`, `migrate`, and the UI. The Caddy and host-specific parts do not transfer.
