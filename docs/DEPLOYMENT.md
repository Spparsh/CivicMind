# Deploying CivicMind

CivicMind is a single FastAPI process that serves the API and the static frontend, with SQLite and a
private file store for photos, videos and voice notes. Run **one instance** (throttles and caches are
in-process) behind HTTPS.

## 1. Production settings

| Variable | Required | Notes |
|---|---|---|
| `CIVICMIND_ENV=production` | yes | Strict start-up: refuses to start without a proper secret, with demo mode, on the demo database or with TinyFish mock mode. Set by the Docker image. |
| `CIVICMIND_SECRET` | yes | Random, 32+ characters (`python -c "import secrets;print(secrets.token_urlsafe(48))"`). Changing it signs everyone out. |
| `CIVICMIND_DB`, `CIVICMIND_EVIDENCE_DIR` | yes | On a **persistent** volume (the image uses `/data`). |
| `CIVICMIND_ADMIN_EMAIL`, `CIVICMIND_ADMIN_PASSWORD` | first start | Creates the first administrator (password ≥ 12 chars). Remove the password variable afterwards. Or run `python manage.py create-user --role admin …`. |
| `SARVAM_API_KEY` | optional | AI classification, translation, chatbot phrasing, speech-to-text. Without it everything runs rule-based. |
| `TINYFISH_API_KEY` | optional | Admin public data-source discovery. Keep `TINYFISH_MOCK_MODE=false`. |
| `CIVICMIND_REVERSE_GEOCODER` | optional | `none` (default) or `nominatim` — only within the [Nominatim usage policy](https://operations.osmfoundation.org/policies/nominatim/), with an identifying `CIVICMIND_REVERSE_GEOCODER_USER_AGENT`; for real traffic use your own geocoder URL. |
| `CIVICMIND_CORS_ORIGINS` | optional | Leave empty (same-origin). |

Secrets go in the platform's secret store or an env file readable only by the service — never in the
image, the repository or the frontend.

## 2. Options

**Docker** (any VPS or container host)

```bash
docker build -t civicmind .
docker run -d --name civicmind -p 127.0.0.1:8000:8000 -v civicmind-data:/data --env-file /etc/civicmind.env civicmind
```

Put Nginx (or the platform's proxy) in front for TLS: `deploy/nginx-civicmind.conf` sets the 32 MB body
limit needed for video uploads, forwards `X-Forwarded-Proto` (so HSTS is sent) and disables buffering for
live updates. The image has a health check on `/api/health`.

**Render** — `render.yaml` (Docker runtime, persistent disk at `/data`, generated secret, health check).
A paid plan is needed for the disk; without a disk all data is lost on every deploy.

**VPS without Docker** — `deploy/civicmind.service` (systemd, env file `/etc/civicmind.env`, data in
`/var/lib/civicmind`) + `deploy/nginx-civicmind.conf`.

## 3. Before going live

- [ ] HTTPS only; the proxy forwards `X-Forwarded-For`/`X-Forwarded-Proto`; port 8000 not public.
- [ ] `CIVICMIND_ENV=production`, strong `CIVICMIND_SECRET`, database and evidence on a backed-up volume.
- [ ] A **fresh** database — never deploy `data/demo/civicmind_demo.db` or the demo accounts.
- [ ] First admin created; the bootstrap password removed from the environment; officers created in Admin → Users.
- [ ] Backups: copy the SQLite file (use `sqlite3 civicmind.db ".backup backup.db"` while running) **and** the evidence directory.
- [ ] Data-protection review: complaints contain personal data (names, e-mail, location, photos, voice).
  Voice audio is deleted 90 days after resolution; decide retention for photos/videos.
- [ ] Optional providers configured or left off deliberately; the landing page and logs state which are active.

## 4. What is not included

No horizontal scaling (SQLite, in-process throttles), no e-mail/SMS delivery, no SSO, no malware scanning
of uploads (files are type-checked; photos are re-encoded; videos are stored as uploaded with location tags blanked).
The bundled street extract and synthetic utility network are for demos only (`seed_demo.py`).
