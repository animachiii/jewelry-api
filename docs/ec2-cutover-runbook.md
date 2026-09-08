# EC2 Cutover Runbook

Execute this yourself in the AWS console, over SSH, and from your own
machine — this session has no AWS credentials of its own, no SSH access to
any instance, and won't be given the client's secrets to hold. Report back
what you see at each checkpoint (marked **CHECK**) before moving to the
next section.

See `docs/superpowers/specs/2026-09-08-stage-c-render-independence-design.md`
for why each choice below is what it is, including the 2026-09-08 revision
note explaining why RDS and S3 are now part of this cutover instead of
later, separate work.

**Scope, restated:** V2 (`jewelry-api`) moves its database (Supabase →
client RDS) and object storage (already coded, just needs real credentials
— Stage A) as part of this same cutover, alongside compute and Redis. **V1
(`jewellery-gen-backend`) is unaffected by RDS or S3** — it has no Postgres
dependency at all (job state lives in Redis + Google Sheets), and its own
storage backend stays whatever it is today. Sections 6 and 9 are the only
V1-specific ones; everything about RDS/S3 below is V2-only.

---

## 0. Blocked on the client

Do not start Section 1 until at least the first two of these are answered
— everything downstream depends on them:

1. **Whitelist your own machine's public IP** on TCP 5432 in
   `sg-049bade1c2300e471` (Staging-SG). Confirm which IP actually got
   whitelisted — the rule the client already added didn't match connections
   from this session, so don't assume it's right without checking.
2. **The real S3 bucket name** the client's IAM key is scoped to (the key
   can't run `ListBuckets`, so it can't be discovered) — or explicit
   permission to create your own buckets if none exists yet.
3. Whether that key's policy includes **`s3:ListBucket`** on the bucket —
   without it, `exists()` checks return 403 instead of a clean 404, which
   breaks asset-ownership validation on `/generate`, `/recolor`, `/mix`,
   and `/background/*` (see `docs/business-rules.md`'s note on this exact
   failure mode from the original Task 9 plan).
4. **VPC and subnet** for `zivoro-erp-test-db`, so the EC2 instance can
   optionally be placed alongside it for private connectivity. Not
   blocking — Section 1 has a fallback that works regardless (public IP
   whitelisting, same mechanism as your own machine).
5. A **URL-safe RDS password**, if the client can reissue one. Not
   blocking either — Section 4 covers encoding whatever password you
   actually get — but worth asking, since the current one contains
   characters (`%`, `#`, `£`, `:`, `|`, `[`, `{`, `^`) that make every
   connection string a chance to get the encoding wrong.

---

## 1. Verify S3 access

From your own machine, once the bucket name (item 2 above) is known.
Configure a scratch AWS CLI profile with the client's key — **never commit
these values anywhere, and don't paste them back into chat once you've set
them locally**:

```bash
aws configure set aws_access_key_id <the key> --profile zivoro
aws configure set aws_secret_access_key <the secret> --profile zivoro
aws configure set region ap-south-1 --profile zivoro
```

Real round-trip, not just a permissions check:

```bash
BUCKET=<real bucket name from the client>
echo "ec2-cutover-test $(date -u +%FT%TZ)" > /tmp/s3-roundtrip-test.txt
aws s3 cp /tmp/s3-roundtrip-test.txt "s3://$BUCKET/ec2-cutover-test.txt" --profile zivoro
aws s3 cp "s3://$BUCKET/ec2-cutover-test.txt" /tmp/s3-roundtrip-verify.txt --profile zivoro
diff /tmp/s3-roundtrip-test.txt /tmp/s3-roundtrip-verify.txt && echo "ROUND-TRIP OK"
aws s3api head-object --bucket "$BUCKET" --key does-not-exist-12345 --profile zivoro 2>&1
```

**CHECK:** `ROUND-TRIP OK` prints. The last command should fail with a
`404`/`Not Found`-shaped error, **not** `403`/`AccessDenied` — a 403 here
means `s3:ListBucket` is missing from the key's policy (item 3 above), and
the app's own `exists()` will misbehave in exactly the way
`docs/business-rules.md` warns about. Report which error you actually see.

Clean up the test object:

```bash
aws s3 rm "s3://$BUCKET/ec2-cutover-test.txt" --profile zivoro
```

---

## 2. Verify RDS connectivity

From your own machine, once your IP is actually whitelisted (item 1 above
— confirm, don't assume).

```bash
export PGPASSWORD='<the RDS password>'
psql -h zivoro-erp-test-db.clks6mke4e4l.ap-south-1.rds.amazonaws.com \
     -p 5432 -U rakshit_team -d AiImageEnhancement \
     -c "select current_database(), current_user, version();"
```

**CHECK:** returns a row, not a timeout or auth error. A timeout means the
security group rule still doesn't match your IP — re-check item 1, don't
retry blindly.

---

## 3. Run V2's schema migration against RDS

Still from your own machine — cheaper to catch a migration problem here
than after EC2 exists.

**URL-encode the password before building a connection string.** Never
hand-encode special characters yourself — get it right programmatically:

```bash
python3 -c "
from urllib.parse import quote
pw = input('RDS password: ')
print(quote(pw, safe=''))
"
```

Paste the password when prompted (it won't be echoed if you run this
interactively in a normal terminal; if it is echoed, clear your scrollback
after). Use the printed value in place of `<url-encoded-password>` below —
**do not paste the raw password into a URL directly**, even if it looks
like it doesn't need encoding.

```bash
cd ~/jewelry-api
export DATABASE_URL="postgresql+asyncpg://rakshit_team:<url-encoded-password>@zivoro-erp-test-db.clks6mke4e4l.ap-south-1.rds.amazonaws.com:5432/AiImageEnhancement"
uv run alembic upgrade head
```

**CHECK:** completes with no error. Then confirm the tables landed:

```bash
export PGPASSWORD='<the RDS password>'
psql -h zivoro-erp-test-db.clks6mke4e4l.ap-south-1.rds.amazonaws.com \
     -p 5432 -U rakshit_team -d AiImageEnhancement \
     -c "\dt"
```

**CHECK:** lists `api_clients`, `config_versions`, `jobs`, `sub_jobs`,
`assets`, `cost_events`, `job_events` — the full schema from
`docs/schema.md`.

---

## 4. Migrate `api_clients` and `config_versions` from Supabase

**Only these two tables.** Job history stays behind — the existing rows are
seeded demo data and your own test runs, not client data, and every
historical `assets` row points at a Supabase Storage path that won't exist
once storage moves to S3 either. Starting job history fresh on the new
infrastructure is the deliberate choice here, not an oversight. If that's
wrong, say so before running this — carrying job history across would also
mean bulk-copying every historical object from Supabase Storage to S3,
which this runbook does not cover.

Get the current Supabase `DATABASE_URL` from the `jewelry-api` Render
service's Environment tab (same source every other `.env` value in this
runbook comes from).

```bash
SUPABASE_DATABASE_URL="<paste the Supabase connection string, psycopg-shaped: postgresql://...>"

pg_dump "$SUPABASE_DATABASE_URL" \
  --table=public.api_clients --table=public.config_versions \
  --data-only --column-inserts --no-owner --no-privileges \
  -f /tmp/ec2-cutover-data-migration.sql
```

**CHECK:** the file is non-empty and contains `INSERT INTO` statements for
both tables:

```bash
grep -c "INSERT INTO" /tmp/ec2-cutover-data-migration.sql
grep -o "INSERT INTO [a-z_.]*" /tmp/ec2-cutover-data-migration.sql | sort -u
```

Expected: at least one `INSERT INTO public.api_clients` and one
`INSERT INTO public.config_versions`.

Restore into RDS:

```bash
export PGPASSWORD='<the RDS password>'
psql -h zivoro-erp-test-db.clks6mke4e4l.ap-south-1.rds.amazonaws.com \
     -p 5432 -U rakshit_team -d AiImageEnhancement \
     -f /tmp/ec2-cutover-data-migration.sql
```

**CHECK:** no errors. Then confirm the rows are actually there and the
active config version matches what's live on Render right now:

```bash
psql -h zivoro-erp-test-db.clks6mke4e4l.ap-south-1.rds.amazonaws.com \
     -p 5432 -U rakshit_team -d AiImageEnhancement \
     -c "select count(*) from api_clients; select version_number, is_active from config_versions where is_active;"
```

**CHECK:** `api_clients` count matches what you'd expect (at least the
Flutter ERP's key and the `/ui` demo key), and exactly one `config_versions`
row shows `is_active = true`, with a `version_number` matching the active
version `GET /api/v2/config` currently returns on the live Render
deployment.

Clean up the local dump file — it contains real API client data:

```bash
rm -f /tmp/ec2-cutover-data-migration.sql
```

---

## 5. Launch the instance

AWS Console → EC2 → Launch instance, region **ap-south-1**:

| Field | Value |
| :--- | :--- |
| Name | `jewelry-render-independence` |
| AMI | Ubuntu Server 22.04 LTS (64-bit x86) |
| Instance type | `t3.small` |
| Key pair | create new, download the `.pem`, keep it — you'll SSH with it |
| Network settings → Create security group | see table below |
| Storage | 20 GiB gp3 (default is usually fine, just confirm it's gp3) |

If item 4 in Section 0 (RDS's VPC/subnet) is known by now, launch into that
same VPC for private RDS connectivity — otherwise launch normally and use
the public-IP whitelisting fallback in Section 6 instead. Don't block the
launch on waiting for that answer.

Security group rules:

| Type | Port | Source |
| :--- | :--- | :--- |
| SSH | 22 | My IP (not `0.0.0.0/0`) |
| Custom TCP | 8000 | Anywhere (`0.0.0.0/0`) |
| Custom TCP | 8001 | Anywhere (`0.0.0.0/0`) |

Do not add a rule for 6379 — Redis stays unreachable from outside the
instance.

Launch it.

---

## 6. Attach an Elastic IP, and whitelist it for RDS

```
EC2 → Elastic IPs → Allocate Elastic IP address → Allocate.
Then Actions → Associate Elastic IP address → select the instance you just
launched.
```

**CHECK:** note the Elastic IP address here — every step below refers to it
as `<ELASTIC_IP>`.

**This instance needs its own RDS whitelist entry, separate from your
laptop's.** Add `<ELASTIC_IP>/32` on TCP 5432 to `sg-049bade1c2300e471` —
the same security group from Section 0, but a new rule. The app running on
this instance connects to RDS at runtime; your laptop's rule from Sections
2-4 only covered the migration steps you ran yourself. If the instance
later moved into RDS's own VPC (see Section 5's note), a security-group
source instead of a public IP would be the better long-term rule — that's
a follow-up, not required to proceed now.

**CHECK:** the new rule is visible in the security group's inbound rules.

---

## 7. SSH in and bootstrap

```bash
ssh -i /path/to/your-key.pem ubuntu@<ELASTIC_IP>
```

Once connected:

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl gnupg git
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker ubuntu
```

Log out and back in (`exit`, then SSH again) so the `docker` group
membership takes effect without needing `sudo` for every command.

```bash
docker --version
docker compose version
```

**CHECK:** both commands print a version with no error.

---

## 8. Clone both repos

```bash
sudo mkdir -p /opt/jewelry
sudo chown ubuntu:ubuntu /opt/jewelry
cd /opt/jewelry
git clone https://github.com/animachiii/jewelry-api.git
git clone https://github.com/animachiii/jewellery-gen-backend.git
cd jewelry-api && git checkout main && cd ..
cd jewellery-gen-backend && git checkout master && cd ..
```

(If either repo is private, you'll need a GitHub personal access token or an
SSH deploy key set up on this instance first — same as any other private
clone.)

---

## 9. Populate `.env` for jewelry-api

```bash
cd /opt/jewelry/jewelry-api
cp .env.example .env
nano .env   # or your editor of choice
```

Follow `docs/deployment-ec2.md` in this repo for the exact delta table —
**it changed today**: `DATABASE_URL` now points at RDS (URL-encoded, same
as Section 3) and `S3_REGION`/`BUCKET_INPUTS`/`BUCKET_OUTPUTS`/
`AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` now hold the client's real S3
values instead of dormant defaults. Everything else still comes from the
Render dashboard as before.

**Do not copy `SUPABASE_URL`/`SUPABASE_SERVICE_KEY`** from Render even if
they're still sitting in that dashboard — the app doesn't read them
anymore.

**CHECK:** `cat .env` — confirm `DATABASE_URL` points at
`zivoro-erp-test-db...rds.amazonaws.com`, not Supabase; confirm
`AWS_ACCESS_KEY_ID` is set to the real key, not blank; confirm no line
still shows an obvious `.env.example` placeholder.

---

## 10. Populate `.env` for jewellery-gen-backend

**Unaffected by the RDS/S3 changes above** — this repo has no Postgres
dependency, and its storage backend stays whatever it is today.

```bash
cd /opt/jewelry/jewellery-gen-backend
cp .env.example .env
nano .env
```

Follow that repo's own `docs/deployment-ec2.md`. Pay particular attention
to `REDIS_URL`: it carries across unchanged from Render, but confirm in the
Render dashboard that it's actually an Upstash URL before doing so.

Also check `STORAGE_BACKEND` while you're in the Render dashboard —
`docs/deployment-ec2.md` assumes it's whatever this service is on today
(that doc's own `.env.example` default is `local`, but this repo also
supports `drive`/`supabase`/`s3`, and this session never re-confirmed which
one is actually live on Render). Copy across whatever it really is; don't
assume `supabase`.

**CHECK:** `cat .env` shows real values, `REDIS_URL` starts with something
like `redis://default:...@....upstash.io:...` (not `redis://red-...`, a
Render-internal hostname shape, or `localhost`), and `STORAGE_BACKEND`
matches what Render's dashboard actually shows rather than an assumption.

---

## 11. Bring up jewelry-api

```bash
cd /opt/jewelry/jewelry-api
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
docker compose -f docker-compose.yml -f docker-compose.prod.yml ps
```

**CHECK:** `migrate` shows `Exited (0)` — this is the first real proof the
container can actually reach RDS over the network, not just that `psql`
could from your laptop. `api`, `worker`, `beat`, `redis` all show `Up`. If
`migrate` fails, run
`docker compose -f docker-compose.yml -f docker-compose.prod.yml logs migrate`
and check first whether it's a connectivity error (Section 6's whitelist
rule) or a genuine migration error — report the output before continuing.

```bash
curl -s localhost:8000/api/v2/health
```

**CHECK:** JSON response with `"status": "ok"`. Note this checks Postgres
and Redis for real, but hardcodes `storage: "ok"` regardless of S3 state —
Section 13 is the real storage check.

---

## 12. Bring up jewellery-gen-backend

```bash
cd /opt/jewelry/jewellery-gen-backend
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d api worker
docker compose -f docker-compose.yml -f docker-compose.prod.yml ps
```

**CHECK:** only `api` and `worker` are listed and both show `Up` — if a
`redis` container is also listed here, stop and report it; that means the
`depends_on: !reset {}` override in `docker-compose.prod.yml` didn't take
effect and the instance now has an unused local Redis nobody intended to
run.

```bash
curl -s localhost:8001/health
```

**CHECK:** `200 OK` response.

---

## 13. Verify from outside the instance, and prove S3 for real

From your own machine, not the SSH session:

```bash
curl -s http://<ELASTIC_IP>:8000/api/v2/health
curl -s http://<ELASTIC_IP>:8001/health
```

**CHECK:** both reachable and healthy over the public IP.

Then the real proof — this is the actual Task 9-equivalent check, now that
the app is running against real S3:

```bash
curl -s -H "X-API-Key: <a real client or ops key — should already exist, it migrated over in Section 4>" \
  http://<ELASTIC_IP>:8000/api/v2/config
```

**CHECK:** returns the real category/angle config, not a 401/500. A 401
here means the API-client migration in Section 4 didn't actually carry the
key you're testing with — check which client the key belongs to.

Then open `http://<ELASTIC_IP>:8000/ui` in a browser and submit one real
test job. Watch it move off `PENDING` — this exercises Celery consuming
from the new container Redis, RDS for job state, and S3 for both the
upload and the generated output, all for the first time together.

**CHECK:** the submitted job's status changes away from `PENDING` within a
minute or two, and on completion the output image actually opens (the
August base64 bug this project already hit once produced a `COMPLETED`
status and a file of exactly the right length with entirely wrong content —
a green status alone doesn't prove the bytes are right). Report the job's
final status and whether the image opens, either way.

---

## 14. Reboot test

```bash
sudo reboot
```

Wait about a minute, then SSH back in:

```bash
docker ps
```

**CHECK:** all containers you expect (jewelry-api's `api`/`worker`/`beat`/
`redis`, jewellery-gen-backend's `api`/`worker`) are back up with no manual
`docker compose up` needed. `migrate` will not reappear — it's `restart:
"no"` and only runs once per explicit `up`, which is correct.

---

## 15. Suspend (not delete) Render

Only after everything above checks out and you're satisfied. In the Render
dashboard:

- Suspend the `jewelry-api` web service
- Suspend the `jewellery-gen-backend` web service
- **Do not** delete `jewelry-api-redis` yet — wait at least 48 hours of the
  EC2 instance behaving correctly before deleting anything on Render. If
  something's wrong, resume the Render services immediately.

**Read `docs/deployment-ec2.md`'s updated rollback note before doing this**
— unlike the original compute-only plan, resuming Render after this point
is not a clean rollback once real jobs have been created against RDS/S3,
since Render is still pointed at the old Supabase project. Decide your real
rollback window before suspending, not after.

The old Supabase project itself (database and storage) is **not**
decommissioned by this runbook — that's a separate, later, deliberate step
once you're confident RDS/S3 are solid, not something to do today.

---

## Done

Report back: which CHECK points passed, which didn't, and the exact output
for anything that didn't match the expected result. Don't guess past a
failed CHECK — stop there and report it.
