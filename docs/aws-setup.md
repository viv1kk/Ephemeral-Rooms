# AWS setup, end to end

Everything you need in the cloud to run this, in the order you need it.

Budget about **45 minutes**, most of it waiting for DNS.

> **Read this first.** Deploying restarts the service, and a restart destroys
> every active room and every uploaded file. That is by design, not a bug
> (spec §19). It matters here because continuous deployment means a merge to
> `main` disconnects anyone mid-session with no warning.

---

## What you are building

```
        your domain (DNS A record)
                 │
                 ▼
        Elastic IP ──► EC2 instance (Ubuntu 24.04)
                          │
                          ├── Nginx        :80/:443  TLS, static assets, proxy
                          └── Uvicorn      :8000     loopback only, ONE worker
                                 │
                                 └── /var/lib/ephemeral-rooms   room files
```

One instance. No load balancer, no S3, no RDS, no Redis, no container
registry. The application cannot be scaled horizontally — all room state lives
in one process's memory — so there is nothing here to distribute.

**What you need before starting**

- An AWS account.
- A domain you control, with access to its DNS records.
- A terminal with `ssh` (built into Windows 10+, macOS, and Linux).

**What you do *not* need**

- No AWS credentials in GitHub. Deployment goes over SSH, so GitHub never
  touches your AWS account.
- No Docker, no Kubernetes, no CI runner on AWS.
- Node is never installed on the server. The frontend is built in CI and the
  bundle is copied across.

---

## Cost

Approximate, `us-east-1`, as of early 2026. Check current pricing — rates
change and vary by region.

| Item | Monthly |
|---|---|
| `t4g.small` (2 vCPU, 2 GiB, ARM) | ~$12 |
| `t3.small` (2 vCPU, 2 GiB, x86) | ~$15 |
| 30 GB gp3 root volume | ~$2.40 |
| Public IPv4 address | ~$3.65 |
| Data transfer out (first 100 GB) | free |
| **Total** | **~$18–21** |

Two things people get caught by:

- **AWS charges for every public IPv4 address**, including an Elastic IP
  attached to a running instance. It is not free. You cannot avoid this while
  serving public HTTPS on a single instance.
- **A stopped instance still costs money for its EBS volume and its Elastic
  IP.** Stopping is not the same as not paying. See [Teardown](#teardown).

**ARM is worth taking.** `t4g` is cheaper and `pycrdt` publishes `aarch64`
wheels, which I verified — so there is no compiler needed on ARM either. The
only reason to prefer x86 is if you later add a dependency that ships x86
wheels only.

---

## Step 1 — Create an SSH key pair

Do this locally. You will use one key to log in yourself, and a second,
separate key for GitHub Actions.

```bash
# Your personal key
ssh-keygen -t ed25519 -f ~/.ssh/ephemeral-rooms -C "you@example.com"

# The deploy key GitHub Actions will use
ssh-keygen -t ed25519 -f ~/.ssh/ephemeral-deploy -N "" -C "github-actions"
```

The deploy key has **no passphrase** (`-N ""`) because an automated runner
cannot type one. Keep them separate: if the deploy key ever leaks you can
revoke it without losing your own access.

On Windows, run these in Git Bash or PowerShell — `ssh-keygen` ships with both.

### Import your personal key into AWS

EC2 → **Key Pairs** → **Actions** → **Import key pair**

- Name: `ephemeral-rooms`
- Paste the contents of `~/.ssh/ephemeral-rooms.pub` (the `.pub` file — never
  the private one)

---

## Step 2 — Create the security group

EC2 → **Security Groups** → **Create security group**

- Name: `ephemeral-rooms-sg`
- Description: `Ephemeral rooms: ssh from me, http/https public`
- VPC: the default

**Inbound rules — exactly three:**

| Type | Port | Source | Why |
|---|---|---|---|
| SSH | 22 | **My IP** | Administration. Never `0.0.0.0/0`. |
| HTTP | 80 | `0.0.0.0/0` | The ACME challenge for certbot, and the redirect to HTTPS. |
| HTTPS | 443 | `0.0.0.0/0` | The application. |

**Outbound:** leave the default (all traffic).

Nothing else is needed. Port 8000 stays closed — Uvicorn binds `127.0.0.1`
and is reachable only through Nginx on the same machine.

> If your home IP changes, SSH will start timing out. Edit the rule and set
> **My IP** again. GitHub Actions runners have no fixed IP range you can
> usefully allowlist, so the deploy connects from an address you cannot
> predict. See [If you lock out the deploy](#if-you-lock-out-the-deploy).

---

## Step 3 — Launch the instance

EC2 → **Instances** → **Launch an instance**

| Setting | Value |
|---|---|
| Name | `ephemeral-rooms` |
| AMI | **Ubuntu Server 24.04 LTS**. Pick the `arm64` variant for `t4g`, `x86_64` for `t3`. |
| Instance type | `t4g.small` (ARM) or `t3.small` (x86) |
| Key pair | `ephemeral-rooms` |
| Network → Security group | **Select existing** → `ephemeral-rooms-sg` |
| Storage | **30 GiB gp3** |
| Advanced → Termination protection | **Enable** |

**On the AMI:** make sure it says 24.04, not 22.04. The deployment installs
`python3.12`, which 22.04 does not ship. The bootstrap script checks glibc and
will refuse an older base.

**On storage:** the 8 GiB default is too small. About 6 GiB goes to the OS,
and the application reserves a further 2 GiB of headroom it will never fill
into (`DISK_HEADROOM_BYTES`), so 8 GiB leaves you almost nothing for uploads.
30 GiB gives roughly 20 GiB of usable upload space. You can grow an EBS volume
later, but not shrink it.

**On instance type:** the spec calls for `t3.small` or larger. A `t3.micro`
(1 GiB) will run it for light use, but CRDT documents accumulate tombstones
and rooms hold their files in memory-adjacent state, so 2 GiB is the sensible
floor.

---

## Step 4 — Allocate an Elastic IP

Without this, **the public IP changes every time the instance stops and
starts**, which breaks DNS and invalidates your certificate. This is the step
people skip and regret.

EC2 → **Elastic IPs** → **Allocate Elastic IP address** → Allocate

Then, with it selected: **Actions** → **Associate Elastic IP address**

- Resource type: Instance
- Instance: `ephemeral-rooms`
- Associate

Write the address down. It is referred to below as `<ELASTIC_IP>`.

---

## Step 5 — Point DNS at it

At your domain registrar or DNS host:

For an apex domain (`example.com`):

| Type | Name | Value | TTL |
|---|---|---|---|
| `A` | `@` (apex) | `<ELASTIC_IP>` | 300 |
| `A` | `www` | `<ELASTIC_IP>` | 300 |

For a subdomain (`rooms.example.com`), one record is all you need:

| Type | Name | Value | TTL |
|---|---|---|---|
| `A` | `rooms` | `<ELASTIC_IP>` | 300 |

**Do not add a `www.` record for a subdomain.** `bootstrap.sh` detects which
case it is and only asks certbot for a `www.` alias on an apex domain. That
detail matters: certbot validates every name it is given and fails the whole
request if one of them does not resolve, and Let's Encrypt rate-limits
failures at five per hostname per hour.

A `CNAME` for `www` pointing at the apex works too. A low TTL (300s) is worth
setting now so mistakes are cheap to correct.

**Then wait, and verify before going further:**

```bash
dig +short example.com
dig +short www.example.com
```

Both must return your Elastic IP. This can take from two minutes to an hour.

> **Do not run certbot until these resolve.** It will fail, and Let's Encrypt
> rate-limits failed attempts — five failures per hostname per hour. Waiting
> is faster than being locked out.

---

## Step 6 — First connection

```bash
ssh -i ~/.ssh/ephemeral-rooms ubuntu@<ELASTIC_IP>
```

Accept the host key fingerprint. Then, on the instance:

```bash
# Confirm you are where you think you are
lsb_release -a          # should say 24.04
python3.12 --version    # should say 3.12.x
uname -m                # aarch64 (t4g) or x86_64 (t3)

# Take pending security updates before installing anything
sudo apt update && sudo apt upgrade -y
```

Now install the deploy key so GitHub Actions can get in. **From your own
machine**, not the instance:

```bash
ssh-copy-id -i ~/.ssh/ephemeral-deploy.pub ubuntu@<ELASTIC_IP>
```

No `ssh-copy-id` on Windows? Append it by hand:

```bash
cat ~/.ssh/ephemeral-deploy.pub | ssh -i ~/.ssh/ephemeral-rooms ubuntu@<ELASTIC_IP> \
  "mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
```

Verify it works on its own, before trusting CI with it:

```bash
ssh -i ~/.ssh/ephemeral-deploy -o BatchMode=yes ubuntu@<ELASTIC_IP> "echo deploy key works"
```

`BatchMode=yes` disables password prompts, so this fails rather than hangs if
the key is wrong — which is exactly how it will behave in CI.

---

## Step 7 — Deploy once, by hand

Do the first deployment manually. If something is wrong with the instance you
want to see it directly, not through a CI log.

**On your machine, from the repository root:**

```bash
# Build the frontend. Node is a build-time dependency only; the server
# never has it installed.
cd frontend && npm ci && npm run build && cd ..

# Copy everything across
rsync -az --delete \
  -e "ssh -i ~/.ssh/ephemeral-rooms" \
  --exclude .git --exclude node_modules --exclude .venv \
  --exclude __pycache__ --exclude .pytest_cache --exclude .mypy_cache \
  ./ ubuntu@<ELASTIC_IP>:/tmp/ephemeral-rooms/
```

**On the instance:**

```bash
cd /tmp/ephemeral-rooms
sudo ./deploy/bootstrap.sh example.com
```

The script installs packages, creates the `ephemeral` service user, builds the
virtualenv, writes `/etc/ephemeral-rooms.env`, installs the systemd unit and
the Nginx site, and finishes with a smoke test that creates a room through the
API and fetches the landing page. It is idempotent, so it is safe to re-run.

It **stops before certbot** on purpose and installs an HTTP-only site in the
meantime, because the TLS block cannot load until a certificate exists.

Check it is alive over plain HTTP:

```
http://example.com
```

You should get the landing page. If not, jump to
[Troubleshooting](#troubleshooting).

---

## Step 8 — Turn on HTTPS

On the instance:

```bash
# apex
sudo certbot --nginx -d example.com -d www.example.com

# subdomain - one name only, because www.rooms.example.com does not exist
sudo certbot --nginx -d rooms.example.com
```

`bootstrap.sh` prints the exact command for your domain when it finishes, so
you can copy it rather than deciding which of these applies.

Answer the prompts: an email for expiry notices, agree to the terms, and choose
**redirect** when it offers to redirect HTTP to HTTPS. certbot rewrites the
Nginx site itself and reloads it.

Then verify renewal actually works — this is an acceptance requirement, not a
formality:

```bash
sudo certbot renew --dry-run
sudo systemctl status certbot.timer     # renewal is automatic, twice daily
```

### Check certbot did not break the WebSocket

certbot edits the Nginx site, and its default timeouts would cut idle
WebSockets after 60 seconds:

```bash
grep -A3 'location /ws' /etc/nginx/sites-available/ephemeral-rooms
```

You must still see `proxy_read_timeout 3600s`. If it is gone, restore it from
`deploy/nginx.conf` and `sudo systemctl reload nginx`.

Then confirm a connection genuinely survives past a minute — open the room in
a browser, leave it untouched for 90 seconds, and check the status bar still
says **Connected**.

---

## Step 9 — Wire up continuous deployment

Now that a manual deploy works, let CI do it.

GitHub → your repository → **Settings** → **Secrets and variables** →
**Actions**

**Secrets** tab → *New repository secret*:

| Name | Value |
|---|---|
| `DEPLOY_SSH_KEY` | The **entire** contents of `~/.ssh/ephemeral-deploy` — the private key, including the `-----BEGIN`/`-----END` lines |
| `DEPLOY_HOST` | `<ELASTIC_IP>` |
| `DEPLOY_USER` | `ubuntu` |
| `DEPLOY_KNOWN_HOSTS` | Output of `ssh-keyscan -H <ELASTIC_IP>` (optional, recommended) |

**Variables** tab → *New repository variable*:

| Name | Value |
|---|---|
| `DEPLOY_DOMAIN` | `example.com` |
| `DEPLOY_ENABLED` | `true` |

`DEPLOY_ENABLED` is the on-switch. Until it is `true` the deploy job is
skipped, which is why this could all be merged before the instance existed.

`DEPLOY_DOMAIN` is a *variable*, not a secret — a domain is public by
definition, and GitHub rejects the `secrets` context in an environment URL.

`DEPLOY_KNOWN_HOSTS` is worth setting. Without it the workflow falls back to
`ssh-keyscan` at deploy time, which trusts whatever answers on the day.

### Optional: require approval before deploying

Because a deploy destroys every live room, you may want a human in the loop.

**Settings** → **Environments** → **production** → **Required reviewers** →
add yourself.

Deployment then pauses and waits for one click. No code change needed; the
workflow already names the environment.

### Then trigger it

Push anything to `main`. CI runs; if all four checks pass, the deploy job
follows automatically and finishes by fetching your live site and creating a
room through it.

---

## Security headers

Set in three places so they hold however the app is reached:
`deploy/security-headers.conf` (Nginx), `backend/app/security.py` (the
application itself, which covers proxied responses and the SPA fallback), and
the Vite dev server for the case where it is tunnelled.

What is sent, and the two decisions worth knowing:

| Header | Value |
|---|---|
| `Content-Security-Policy` | `default-src 'self'` with `frame-ancestors 'none'`, `object-src 'none'`, `base-uri 'none'` |
| `Strict-Transport-Security` | `max-age=63072000; includeSubDomains; preload` |
| `Referrer-Policy` | `no-referrer` |
| `X-Content-Type-Options` | `nosniff` |
| `X-Frame-Options` | `DENY` |
| `Cross-Origin-Opener-Policy` | `same-origin` |
| `Cross-Origin-Resource-Policy` | `same-origin` |
| `Permissions-Policy` | every feature denied |

**`script-src` has no `'unsafe-inline'`,** because the built page contains no
inline script at all — that is where most of the value in a CSP is.

**`style-src` does have it, and cannot avoid it.** CodeMirror injects its theme
through a runtime `<style>` element and y-codemirror paints remote selections
with a `style` attribute on each decoration. Tightening it was tested: every
collaborator's highlight renders transparent and the browser blocks seven
inline styles. Mozilla Observatory does not penalise `'unsafe-inline'` when it
is confined to `style-src`.

### An Nginx trap worth knowing

`add_header` in a `server` block is inherited by a `location` **only if that
location declares no `add_header` of its own**. The `/assets/` location sets
`Cache-Control`, which silently discards every inherited security header for
those responses. That is why the headers live in an included snippet that is
pulled into both places, rather than being written once in the server block.

### If the HTTPS redirect is still reported as missing

The Nginx site returns `301 https://$host$request_uri` from port 80, which is
what a scanner wants. If a scan still reports no redirect, the request is not
reaching this Nginx:

- **Behind a Cloudflare tunnel**, Cloudflare terminates TLS and answers port 80
  itself. Turn on **SSL/TLS → Edge Certificates → Always Use HTTPS**; Nginx
  never sees that request.
- **Before certbot has run**, `bootstrap.sh` installs an HTTP-only site on
  purpose, because the TLS block cannot load without a certificate. It cannot
  redirect to HTTPS that does not exist yet. Finish
  [Step 8](#step-8--turn-on-https) and the redirect appears.

### Scanning the right thing

A tunnel pointed at the Vite dev server is not what you ship. Its CSP is
deliberately looser — Vite's React Fast Refresh injects an inline module
script, so dev needs `'unsafe-inline'` for scripts and production does not. To
check the real headers without deploying, serve the production build from
Uvicorn:

```bash
cd frontend && npm run build && cd ../backend
SERVE_STATIC_DIR=../frontend/dist .venv/bin/uvicorn app.main:app --port 8000 --workers 1
```

### HSTS preloading

`max-age` is already two years with `includeSubDomains` and `preload`, so the
domain is eligible. Submitting it at <https://hstspreload.org/> is a one-way
door in practice: every subdomain of it must serve HTTPS, permanently.

---

## Ongoing operations

```bash
# Is it running?
sudo systemctl status ephemeral-rooms

# Follow the logs
journalctl -u ephemeral-rooms -f

# Last 200 lines
journalctl -u ephemeral-rooms -n 200 --no-pager

# Restart (destroys every active room — that is by design)
sudo systemctl restart ephemeral-rooms

# Nginx
sudo nginx -t && sudo systemctl reload nginx

# Disk usage — the thing most likely to bite you
df -h /
du -sh /var/lib/ephemeral-rooms
```

**Changing configuration:** edit `/etc/ephemeral-rooms.env`, then
`sudo systemctl restart ephemeral-rooms`. Note that `bootstrap.sh` never
overwrites this file, so your edits survive redeployment.

**Security updates:**

```bash
sudo apt update && sudo apt upgrade -y
sudo reboot        # only if the kernel was updated
```

---

## Teardown

To stop paying, in this order:

1. **Terminate the instance.** EC2 → Instances → Instance state → Terminate.
   You enabled termination protection in Step 3, so disable that first under
   Actions → Instance settings.
2. **Release the Elastic IP.** EC2 → Elastic IPs → Actions → Release. An
   unattached Elastic IP still costs money — this is the most commonly
   forgotten step.
3. **Check for orphaned EBS volumes.** EC2 → Volumes, filter by state
   `available`. Terminating usually deletes the root volume, but verify.
4. Delete the security group and key pair if you are done for good.

Stopping rather than terminating still charges for the EBS volume and the
Elastic IP — roughly $6/month for a machine doing nothing.

---

## Troubleshooting

**SSH times out.** Your IP changed. Edit the security group's port 22 rule and
set **My IP** again.

**`bootstrap.sh` says `frontend/dist/index.html` is missing.** You did not
build the frontend before copying. Run `npm ci && npm run build` in
`frontend/` locally and rsync again — Node is not on the server.

**certbot fails with "DNS problem" or "unauthorized".** DNS has not propagated,
or port 80 is closed. Check `dig +short example.com` returns your Elastic IP
and that the security group allows 80 from `0.0.0.0/0`. Wait rather than
retrying in a loop: Let's Encrypt rate-limits five failures per hostname per
hour.

**The site loads but the room never connects.** The WebSocket is not getting
through. Check `grep -A3 'location /ws'` in the Nginx site still has the
`Upgrade`/`Connection` headers and `proxy_read_timeout 3600s`, then
`journalctl -u ephemeral-rooms -f` while you reload the page.

**Connection drops after ~60 seconds of inactivity.** `proxy_read_timeout` was
lost, almost certainly when certbot rewrote the config. Restore it from
`deploy/nginx.conf`.

**Uploads fail near a size limit.** Check free space with `df -h /`. The app
refuses to use the last 2 GiB (`DISK_HEADROOM_BYTES`), so a "full" disk still
shows 2 GiB free. Grow the EBS volume, or lower the headroom in
`/etc/ephemeral-rooms.env`.

**Two people in the same room code cannot see each other.** Something is
running more than one worker. Check `ExecStart` in
`/etc/systemd/system/ephemeral-rooms.service` still ends in `--workers 1`, and
that nothing set `WEB_CONCURRENCY` in the environment file. The application
logs a prominent warning at startup if it detects this.

**The service will not start.** `journalctl -u ephemeral-rooms -n 50
--no-pager` names the reason. The usual causes are a malformed value in
`/etc/ephemeral-rooms.env` (configuration is validated at startup and fails
fast, deliberately) or `DATA_ROOT` not being writable by the `ephemeral` user.

### If you lock out the deploy

The GitHub Actions runner connects from an unpredictable IP, so port 22 must
be open to it. If you tighten SSH to your own IP only, **CI deployments will
fail**.

Options, least to most work:

- Leave 22 open to `0.0.0.0/0` and rely on key-only authentication. Ubuntu
  disables password login by default, so this is defensible for a personal
  project, though you will see constant background scanning in the logs.
- Allowlist GitHub's published Actions ranges from
  `https://api.github.com/meta` — they are large and they change, so this
  needs periodic maintenance.
- Keep 22 restricted to your IP and deploy manually with Step 7 whenever you
  want to ship.

---

## What to verify when you are done

The two acceptance items that cannot be checked without a live instance:

- [ ] `sudo certbot renew --dry-run` passes
- [ ] A room stays connected for more than 60 seconds idle, through Nginx

And worth confirming while you are there:

- [ ] `https://example.com` loads with a valid certificate
- [ ] `http://example.com` redirects to HTTPS
- [ ] Two browsers in the same room see each other's edits and cursors
- [ ] A file uploads, downloads, and deletes
- [ ] No mixed-content errors in the browser console
