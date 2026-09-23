# Burst mode: more workers, on the cheapest cloud, when paid jobs wait

Burst mode is **off**. Nothing here runs, and nothing here costs money, until
the owner turns it on with the procedure below.

## What it does

Today the web server does each server job itself, one at a time. When many
paid jobs wait (a 500-book pack, an unlimited plan), buyers wait for hours.

In burst mode:

- The web server keeps the data: Postgres and Redis on its own 127.0.0.1, and
  its own worker, which takes paid jobs first and then free ones.
- The files go to a Cloudflare R2 bucket. R2 charges no transfer fee, to any
  cloud or to a customer who downloads a result.
- Once a minute the burst controller (`python -m tools.burst`) reads the paid
  queue. When paid jobs wait, it reads the live offers of each cloud that has
  keys: the newest AWS spot prices, and the Hetzner prices of the server types
  that are in stock. A new worker goes to the offer with the lowest cost per
  book, inside the spend cap.
- A worker reaches Postgres and Redis through an SSH tunnel to the web server.
  The tunnel user can forward those two ports and do nothing else. A worker
  takes no inbound connection.
- A worker with no job for 10 minutes stops after its job, and Terraform
  destroys its machine. A scale-in never kills a job. When a cloud takes a spot
  machine back during a job, the job runs once more on another worker.
- Free credits never start a machine: burst workers take paid jobs only.

```
                  web server (RackNerd, US west)
          Postgres + Redis on 127.0.0.1, own worker, controller
                 ^                                  |
     SSH tunnel  |                                  | terraform apply
     (2 ports)   |                                  v
        burst workers on AWS spot (us-west-2) or Hetzner, 0..N
                 |
                 v
        Cloudflare R2 bucket (inputs, results, app releases, backups)
```

## Money

**Spend cap.** The controller keeps the cost of the burst machines of the last
30 days under a cap:

    cap = min($100, max($5, 30% of the net server revenue of the last 30 days))

The net revenue counts each pack for 30 days after its purchase, and each
active subscription, after the Stripe fee (2.9% + 30 cents, and 0.7% more for
a subscription). Free credits count for nothing. The $5 floor lets the first
buyer have a burst worker. The controller also keeps 2.5 hours of each running
worker in reserve. At the cap it starts no new machine, and running jobs
finish. Change the numbers with `SUBPLZ_BURST_CAP_SHARE`,
`SUBPLZ_BURST_CAP_FLOOR_USD` and `SUBPLZ_BURST_CAP_CEILING_USD`.

**Other limits.** `SUBPLZ_BURST_MAX_WORKERS` (default 1). The AWS spot price
limit (`max_price`, $0.0625 an hour). The AWS spot limit of a new account (5
vCPU: one worker). The Hetzner server limit. A machine powers itself off after
90 idle minutes, if the controller is gone.

**Prices** (financial advocate, 2026-09-22; the controller reads the live ones):

| Offer | Per hour | Compute per 10-hour book | Note |
|---|---|---|---|
| AWS spot, 4-vCPU x86 (c6a, c7a, m6a) | about $0.06, + $0.007 IPv4 and disk | about $0.14 | billed per second |
| AWS spot, 4-vCPU Graviton (c7g) | $0.041-0.056 | $0.10-0.14 | off until one test book: add to `SUBPLZ_BURST_AWS_TYPES` |
| Hetzner EU cx33 | about $0.016 | about $0.05 | often sold out; billed per started hour |
| Hetzner EU cpx32 | about $0.065 | $0.14-0.20 | 155 ms from the web server |
| Hetzner US cpx31 | about $0.115 | $0.25-0.35 | |
| R2 bucket | $0.015 per GB-month | | no transfer fee; 10 GB free |

While burst mode is on and idle, only the R2 storage costs money. AWS charges
$0.09/GB for the results that its workers send to R2, above 100 GB a month:
when you pass that, set `SUBPLZ_BURST_AWS_EGRESS_USD_PER_BOOK` (about 0.09 x
the GB of a result), so that the price comparison includes it.

## Parts

| Path | What |
|---|---|
| `state/` | The web server's Postgres, Redis and tunnel user (`setup-web-server.sh`), the R2 bucket and its token. Apply once, on the web server. |
| `workers/aws/` | One spot instance for each AWS worker. Only the controller applies it. `iam-policy.json` is the policy of the controller's AWS key. |
| `workers/hetzner/` | One server for each Hetzner worker. Only the controller applies it. |
| `cloud-init.yaml.tftpl` | What a new machine does at first boot, on either cloud. |
| `worker-constraints.txt` | The package versions of the web server, so that a worker computes the same result. |
| `systemd/` | Units for the web server: the controller timer and the web server's own worker. |
| `../../tools/burst/` | The controller: `core.py` decides, `offers.py` reads the prices, `revenue.py` reads the revenue. |
| `../../tools/copy_database.py` | Copies SQLite to Postgres (once, when burst mode goes on) and back. |

## Accounts (owner steps, once)

Only the owner does these steps. Claude never types a key.

1. **Cloudflare**: an account with R2 turned on (done on 2026-09-22). At
   activation, make an API token for Terraform (My Profile, API Tokens, Create
   Custom Token) with two permissions: Account, Workers R2 Storage, Edit; and
   User, API Tokens, Edit. Write down the account ID (R2 page, right side).
2. **AWS**, for spot workers: an account on the Paid plan (the Free plan blocks
   xlarge machines), with MFA on the root user. Make an IAM user
   `subplz-burst` with the policy in `workers/aws/iam-policy.json`, and an
   access key for it.
3. **Hetzner**, for Hetzner workers: an account, a project `subplz-burst` with
   nothing else in it, and an API token (Read & Write) in that project. Check
   the server limit of the project (Limits).

The controller uses each cloud that has keys. One cloud is enough; two let it
switch to the cheaper one.

## Turn burst mode on

Do these steps as root on the web server. The site is down from step 8 to
step 12, about 10 minutes.

1. Install Terraform:

   ```bash
   wget -qO- https://apt.releases.hashicorp.com/gpg | gpg --dearmor -o /usr/share/keyrings/hashicorp.gpg
   echo "deb [signed-by=/usr/share/keyrings/hashicorp.gpg] https://apt.releases.hashicorp.com bookworm main" > /etc/apt/sources.list.d/hashicorp.list
   apt-get update && apt-get install -y terraform
   ```

2. Put the keys in `/etc/subplz-burst.env`. Each command asks for the value
   with the input hidden. Leave out the lines of a cloud that you do not use:

   ```bash
   cd /opt/subplz-web
   tools/set-secret.sh CLOUDFLARE_API_TOKEN /etc/subplz-burst.env
   tools/set-secret.sh SUBPLZ_BURST_AWS_ACCESS_KEY_ID /etc/subplz-burst.env
   tools/set-secret.sh SUBPLZ_BURST_AWS_SECRET_ACCESS_KEY /etc/subplz-burst.env
   tools/set-secret.sh HCLOUD_TOKEN /etc/subplz-burst.env
   echo "SUBPLZ_BURST_MAX_WORKERS=1" >> /etc/subplz-burst.env
   ```

3. Install the queue, storage and Postgres clients into the app, and make the
   folder of the Terraform state (it holds passwords):

   ```bash
   .venv/bin/pip install -c infra/burst/worker-constraints.txt redis rq boto3 "psycopg[binary]"
   install -d -m 700 /var/lib/subplz-burst
   ```

4. Make the shared state. This installs Postgres, Redis and the tunnel user on
   this machine, and makes the bucket and its token. Read the plan before you
   type `yes`:

   ```bash
   set -a; . /etc/subplz-burst.env; set +a
   S=/opt/subplz-web/infra/burst/state
   terraform -chdir=$S init -backend-config=path=/var/lib/subplz-burst/state.tfstate
   terraform -chdir=$S apply -var cloudflare_account_id=YOUR_ACCOUNT_ID
   ```

5. Write the storage keys and the backup settings:

   ```bash
   (umask 077
    terraform -chdir=$S output -raw storage_env > /etc/subplz-storage.env
    terraform -chdir=$S output -raw rclone_env > /etc/subplz-rclone.env
    echo "BUCKET=$(terraform -chdir=$S output -raw bucket)" > /etc/subplz-backup.env)
   ```

6. Make the backups:

   ```bash
   cp .env .env.bak-before-burst
   cp data/subplz.db data/subplz.db.bak-before-burst
   ```

7. Read the new database address:

   ```bash
   DB=$(terraform -chdir=$S output -raw web_env | sed -n 's/^SUBPLZ_WEB_DATABASE_URL=//p')
   ```

8. Stop the site:

   ```bash
   systemctl stop subplz-web
   ```

9. Copy the database to Postgres, and the files to the bucket:

   ```bash
   .venv/bin/python -m tools.copy_database sqlite:////opt/subplz-web/data/subplz.db "$DB"
   env $(cat /etc/subplz-rclone.env) rclone copy --progress data/artifacts "r2:$(terraform -chdir=$S output -raw bucket)/jobs"
   ```

10. Tell the app to use the queue, Postgres and the bucket:

    ```bash
    terraform -chdir=$S output -raw web_env >> .env
    install -d /etc/systemd/system/subplz-web.service.d
    printf '[Service]\nEnvironmentFile=/etc/subplz-storage.env\n' > /etc/systemd/system/subplz-web.service.d/storage.conf
    cp infra/burst/systemd/subplz-worker.service infra/burst/systemd/subplz-burst.* /etc/systemd/system/
    systemctl daemon-reload
    ```

11. Start the site, the web server's own worker, and the nightly backup:

    ```bash
    systemctl start subplz-web
    systemctl enable --now subplz-worker subplz-pgdump.timer
    ```

12. Do a test job on the site with "Convert in this browser" off. Make sure
    that it succeeds and that its files download. If it fails, turn burst mode
    off (below) before you look for the cause.

13. Prepare the worker modules of each cloud that has keys:

    ```bash
    for c in aws hetzner; do
      terraform -chdir=infra/burst/workers/$c init -backend-config=path=/var/lib/subplz-burst/workers-$c.tfstate
    done
    ```

14. Start the controller. Do a dry run first: it logs its decision, the offers
    and the spend cap, and changes nothing:

    ```bash
    set -a; . ./.env; . /etc/subplz-storage.env; . /etc/subplz-burst.env; set +a
    .venv/bin/python -m tools.burst --dry-run
    systemctl enable --now subplz-burst.timer
    ```

15. Watch it: `journalctl -u subplz-burst -f`. A new worker needs about 10
    minutes to install before it takes a job.

16. Measure one book on each cloud that the controller used. If a 10-hour book
    takes much more or less than 2 hours on 4 vCPU, the price comparison is
    off: tell Claude the times.

## While burst mode is on

- **Deploy**: after `git pull`, restart `subplz-web` and `subplz-worker`. The
  controller sees the new commit. It stops the workers of the old commit after
  their jobs and starts new ones. You do nothing else.
- **Workers**: change `SUBPLZ_BURST_MAX_WORKERS` in `/etc/subplz-burst.env`.
  The next run uses it. With `0`, each burst worker stops after its job and its
  machine goes.
- **AWS spot limit**: when paid jobs often wait for more than an hour, ask AWS
  for more spot vCPU (Service Quotas, "All Standard Spot Instance Requests"),
  then raise `SUBPLZ_BURST_AWS_VCPU_LIMIT` and `SUBPLZ_BURST_MAX_WORKERS`.
- **Keys**: do not change the passwords, the tunnel key or the bucket token
  while a worker runs. Set the workers to 0 first, and wait until
  `terraform -chdir=infra/burst/workers/<cloud> state list` shows nothing.
- **State**: `/var/lib/subplz-burst` is the only record of what Terraform made.
  If it is lost, the machines still run and cost money: find them in the AWS
  and Hetzner consoles (tag or label `subplz-burst-worker`) and delete them.
- **Storage**: nothing deletes files. R2 costs $0.015 per GB-month; watch the
  bucket size in the Cloudflare dashboard.

## Turn burst mode off

1. Set `SUBPLZ_BURST_MAX_WORKERS=0`. Wait until the `state list` of each worker
   module shows nothing. Then:
   `systemctl disable --now subplz-burst.timer subplz-worker subplz-pgdump.timer`.
2. Stop the site. Copy Postgres to a new SQLite file, and the bucket to the
   disk:

   ```bash
   systemctl stop subplz-web
   .venv/bin/python -m tools.copy_database "$DB" sqlite:////opt/subplz-web/data/subplz-from-postgres.db
   env $(cat /etc/subplz-rclone.env) rclone copy --progress "r2:$(terraform -chdir=$S output -raw bucket)/jobs" data/artifacts
   mv data/subplz.db data/subplz.db.bak-burst && mv data/subplz-from-postgres.db data/subplz.db
   ```

3. Remove the burst lines from `.env` (the six lines that step 10 added), and
   remove `/etc/systemd/system/subplz-web.service.d/storage.conf`. Then
   `systemctl daemon-reload && systemctl start subplz-web`, and do a test job.
4. Only after the test job succeeds: remove the bucket and its token with
   `terraform -chdir=$S destroy`. It deletes the bucket with its files.
   Postgres and Redis stay installed on the web server, unused.

## Security

- Postgres and Redis listen on 127.0.0.1 only. A worker reaches them through
  an SSH tunnel as `subplz-tunnel`, a user with no shell whose key can forward
  those two ports and nothing else. The worker checks the web server's host key.
- A worker accepts no inbound connection (AWS security group and Hetzner
  firewall without inbound rules).
- The bucket token can read and write that one bucket only. The AWS key can end
  only instances tagged `subplz-burst-worker`.
- The state files and `/etc/subplz-*.env` hold secrets: root only, mode 0600.
  A machine's user data holds the tunnel key and the database passwords; only
  the cloud account and the machine itself can read it.
