# Going Live — Enterprise IT Automator

Deployment reference reflecting the state of `main` at commit `c0b066a`. The container image is built and published to GHCR; nothing is running yet. This is the free-tier stack to run it on, and the exact manual steps to actually go live.

**Everything that can be prepared in advance already has been** — `render.yaml` (Render Blueprint) and `.env.production` (generated secrets, gitignored) are both in this repo. §6 below is the actual runbook; §1–5 are the reasoning behind each choice.

Free-tier terms shift often — re-check provider pricing pages before relying on the limits below.

---

## 1. Compute — where the container runs

Every serverless free tier sleeps on idle — Cloud Run's own docs confirm scale-to-zero interrupts background tasks, which matters because this app runs a periodic SLA-sweep loop in-process. That rules out most "easy" options unless you accept the sweep going quiet between requests.

### Render (recommended)

Native GHCR pulls, free TLS subdomain, no card on file.

Point a Web Service at `ghcr.io/jitendraprabhu-l/enterprise-it-automator:latest` directly — no rebuild, no git-push workflow needed. Gets you `https://<name>.onrender.com` with TLS already handled.

**Trade-off:** sleeps after 15 minutes idle, ~30–60s cold start on the next request. The SLA-sweep `asyncio` task pauses along with everything else while asleep — pair with the uptime monitor in §4 to keep it warm if that matters to you.

- 750 hrs/mo free compute
- No card required
- ~30–60s cold start

*Alt: Koyeb — same idea, scale-to-zero can't be disabled on free tier.*

### Oracle Cloud Always Free (true always-on)

A real, persistent Arm VM — the only free option that won't interrupt the background sweep.

You install Docker yourself and run `docker compose up -d` on the box. Genuinely always-on, so the SLA-sweep task runs continuously exactly as it does in local testing.

**Trade-off:** you own the ops — firewall, TLS (nothing bundled; add Caddy or put Cloudflare in front), systemd/restart policy. Oracle also quietly halved the free allocation (4→2 OCPU) in mid-2026 with no announcement, and some regions report capacity/suspension issues — worth a look before committing.

- 2 OCPU / 12 GB Ampere A1 (Arm)
- Card required at signup, not billed
- Self-managed TLS & ops

---

## 2. Database — managed Postgres

The app already speaks Postgres correctly for both the app DB and the LangGraph checkpointer — the deciding factor is just how the free tier goes idle.

### Neon (recommended)

Auto-wakes on connection — the only option that won't strand an unattended background task.

Suspends after 5 minutes idle, but reconnects itself in roughly half a second to a second and a half when the app makes its next query — no dashboard click required. Supabase's free Postgres, by contrast, pauses after 7 days and needs a *human* to resume it in the dashboard, which would silently break the app if nobody notices.

- 0.5 GB storage
- 100 hrs/mo compute
- Auto wake on connect

*Avoid: Supabase — manual resume after 7-day pause. Avoid: Render Postgres — hard-deletes after 30 days. Gone: ElephantSQL shut down Jan 2025.*

---

## 3. TLS & public URL

Not a separate line item — bundled free with whichever compute option you pick above.

- **Render / Koyeb:** a free `*.onrender.com` / `*.koyeb.app` subdomain with auto-renewed TLS, zero configuration.
- **Oracle VM:** nothing bundled. Cheapest fix is putting Cloudflare in front (free plan, handles TLS + a subdomain) rather than managing certificates on the box yourself.

---

## 4. Uptime monitoring *(optional)*

### Better Stack (recommended)

30-second checks, unlimited free alert channels — meaningfully ahead of the usual default pick.

Point it at `/health`. If you're on Render's free tier, this doubles as a keep-warm ping that reduces how often the sweep task actually goes to sleep.

- 10 monitors free
- 30s check interval
- Unlimited alert channels

*Avoid: UptimeRobot — free tier restricted to non-commercial use since Dec 2024. Gone: Freshping shut down Mar 2026.*

---

## 5. Secrets management *(optional)*

**Don't add one yet.** At this scale, the hosting platform's built-in encrypted environment variables (which Render already gives you free) do the job — an extra secrets manager doesn't buy real rotation automation to justify the added moving part.

If that changes later: **GCP Secret Manager's** always-free tier (6 active secret versions, 10,000 accesses/month) covers this app's handful of keys at zero cost. None of the free tiers — GCP included — send you an actual "rotate this now" reminder; treat rotation as a manual, calendar-based habit regardless of what you pick.

---

## 6. What's already prepared vs. what needs you

Two files in this repo are ready to use — nothing left to write, only accounts left to create:

- **`render.yaml`** — a Render Blueprint. Render reads this automatically and provisions the web service from the already-published image, with every secret correctly marked so it's never committed to this public repo.
- **`.env.production`** *(gitignored, local only — not in this repo's git history)* — contains freshly generated values for `API_KEY` and `POSTGRES_PASSWORD`, ready to paste in. `MCP_SERVER_TOKEN` is also there, pre-generated, in case you ever switch `MCP_TRANSPORT` to `http`; the default Blueprint doesn't need it.

None of the three signups below can happen on your behalf — each needs your email/identity, not mine.

### Step 1 — Neon (Postgres)

1. Sign up at [neon.tech](https://neon.tech) (no card required) and create a project.
2. From the project dashboard, copy the connection string it gives you (starts `postgresql://`).
3. You need it in **two forms**, for two different env vars:
   - `DATABASE_URL` — take the copied string and change its scheme to `postgresql+asyncpg://` (the app DB uses the asyncpg driver).
   - `CHECKPOINT_DB_PATH` — use the copied string **as-is**, plain `postgresql://` (the LangGraph checkpointer uses psycopg, not asyncpg — see the comment in `app/config.py` for why these can't share one value even though they point at the same database).

### Step 2 — Groq (LLM)

1. Sign up at [console.groq.com](https://console.groq.com/keys) (free tier) and create an API key.
2. That's your `GROQ_API_KEY`. If you already have one from local testing, **generate a new one instead of reusing it** — the original was pasted into a chat session earlier in this project and should be treated as burned.

### Step 3 — Render (compute)

1. Sign up at [render.com](https://render.com) (no card required).
2. **New → Blueprint**, connect your GitHub account, point it at the `enterprise-it-automator` repo. Render will detect `render.yaml` automatically.
3. When it asks for the `sync: false` variables, paste in:

   | Variable | Value |
   |---|---|
   | `API_KEY` | from `.env.production` |
   | `DATABASE_URL` | from Neon, Step 1 (the `+asyncpg` form) |
   | `CHECKPOINT_DB_PATH` | from Neon, Step 1 (the plain form) |
   | `GROQ_API_KEY` | from Groq, Step 2 |

4. Deploy. Render builds nothing — it just pulls `ghcr.io/jitendraprabhu-l/enterprise-it-automator:latest` and starts it.

### Step 4 — Seed the reviewer accounts and example API client

Render's free tier has no shell access, so run this from your own machine instead, pointed at the same Neon database (export `DATABASE_URL` locally first, same value as Step 3):

```
python -m app.db.seed
```

This creates the `mchen` (manager) and `admin` (it_admin) reviewer tokens — approvals have no one able to decide them without this — plus one example `STANDARD`-role API client (`hr@example.com`). Save every printed token/key; they're shown only once.

Note: the `API_KEY` value you set in Render is automatically promoted to a real `ADMIN`-role API client on first app startup (see `_ensure_bootstrap_admin_client` in `app/api/main.py`) — you don't need to run the seed script just to make that key work. The seed script's `STANDARD` client is only needed if you want to demonstrate/test the per-caller scoping (a `STANDARD` key only sees tickets it filed itself; `ADMIN` sees everything).

### Step 5 — Confirm it's actually live

```
curl https://<your-app>.onrender.com/ready
```

Expect `{"ready":true,"checks":{"database":true,"checkpointer":true}}` — the same signal used to verify the local docker-compose run.

### Step 6 (optional) — Better Stack monitoring

1. Sign up at [betterstack.com](https://betterstack.com) (free, no card).
2. Add an HTTP monitor against `https://<your-app>.onrender.com/health`, 30s interval.
3. This doubles as a keep-warm ping — Render's free tier sleeps after 15 minutes idle, and a monitor hitting it regularly reduces how often that happens.

### Step 7 (optional) — Let strangers try it without a real key

If you're sharing this link publicly (e.g. in a portfolio or with recruiters), set
`DEMO_API_KEY` to a value **different from `API_KEY`** — generate one the same way:

```
python -c "import secrets; print(secrets.token_urlsafe(24))"
```

The dashboard then auto-fills this key for any visitor with no key of their own. It's
seeded as a low-privilege client (can submit tickets and see only tickets it filed
itself, capped at 10 requests/day) — never your real admin key. Leave `DEMO_API_KEY`
unset to keep the dashboard requiring a real key from every visitor, as before.

### Full environment variable list

| Variable | Required | Where it comes from |
|---|---|---|
| `DATABASE_URL` | **required** | Neon connection string, `postgresql+asyncpg://` |
| `CHECKPOINT_DB_PATH` | **required** | same Neon instance, plain `postgresql://` (psycopg driver) |
| `API_KEY` | **required** | pre-generated in `.env.production` |
| `GROQ_API_KEY` | **required** | console.groq.com — a fresh key, not the one pasted earlier |
| `LLM_PROVIDER` | defaulted in `render.yaml` | `groq` |
| `MCP_SERVER_TOKEN` | not needed by default | only if `MCP_TRANSPORT` is switched to `http`; pre-generated in `.env.production` regardless |
| `SENSITIVE_ACTIONS` | defaulted in `render.yaml` | `disable_user,revoke_access,create_user,grant_access` |
| `DEMO_API_KEY` | optional | a fresh, separate value — see Step 7 above. Leave unset to disable public demo access |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | optional | leave blank — tracing stays a no-op |
