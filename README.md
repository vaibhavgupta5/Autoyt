# AutoYT

Picks one long public English stream or podcast a day, cuts 50–55 s funny, informative or deep clips with AI, adds captions, a heading, music and gameplay, and uploads them to YouTube. Design, risks and status are in [PLAN.md](PLAN.md).

## Start (about 15 minutes of setup)

**1. Fill in `.env`**

```bash
cp .env.example .env
```

Set at least `GEMINI_API_KEY`, the six `SMTP_*` / `NOTIFY_EMAIL` values and `TZ`. Each line in `.env.example` says where to get it.

**2. Create the Google OAuth client (once)**

1. Open Google Cloud Console, create a project, and enable **YouTube Data API v3**.
2. APIs & Services > OAuth consent screen: set it up, add your own Google account as a test user, then **publish the app to "In production"**. In "Testing" mode the login expires every 7 days and unattended uploading stops.
3. Credentials > Create credentials > OAuth client ID > **Desktop app**. Download the JSON and save it as `secrets/client_secret.json`.

**3. Build, log in, check email**

```bash
docker compose build
```

```bash
docker compose run --rm -p 8765:8765 autoyt auth
```

Open the URL it prints, approve access with the Google account that owns the YouTube channel. It writes `secrets/token.json`.

```bash
docker compose run --rm autoyt test-email
```

**4. Download the libraries (once, about 170 MB: 5 music tracks and six 60-second gameplay files)**

```bash
docker compose run --rm autoyt fetch-assets
```

```bash
docker compose run --rm autoyt fetch-gameplay
```

Both are already done if you ran them during the build.

**5. Choose how videos are published (`AUTOYT_MODE` in `.env`)**

| Mode | What happens |
|---|---|
| `scheduled` (default) | Each video is uploaded and goes **public** at its own time slot, spread over the day |
| `public` | Each video is published **public** immediately, all at once |
| `dry_run` | Render only, nothing is uploaded, to review clips in `data/output/<today>/` first |
| `private` | Upload but keep private, for tests only |

To preview real clips first, set `AUTOYT_MODE=dry_run` and run `docker compose run --rm autoyt run --force`, then switch back to `scheduled`.

**6. Leave it running**

```bash
docker compose up -d
```

It starts at boot, checks every 15 minutes, and runs once a day while the laptop is on.

## Everyday commands

| Command | What it does |
|---|---|
| `docker compose logs -f` | Watch what it is doing |
| `docker compose exec autoyt autoyt status` | Recent runs, clips, disabled sources |
| `docker compose exec autoyt autoyt run --force` | Re-run today's job (resumes the same video, never re-uploads finished clips) |
| `docker compose run --rm autoyt run --fresh` | Archive today's run and do a brand-new one with your normal mode (stop the daemon first: `docker compose stop`) |
| `docker compose exec autoyt autoyt check-sources` | Count eligible videos per channel |
| `touch data/PAUSE` / `rm data/PAUSE` | Pause or resume without stopping the container |
| `docker compose restart` | After changing `.env` |

Edit channels in `config/sources.yaml`, music in `config/music.yaml`, and gameplay in `config/gameplay.yaml`. Changes to `config/` are read on the next run.

## Troubleshooting

**"Sign in to confirm you're not a bot" in the logs.** YouTube is rate-limiting your IP, usually after a burst of requests such as first-time setup (listing all channels, downloading the gameplay library). It normally clears within a few hours. The daily run retries on its own, and `fetch-gameplay` skips what it already has, so just run it again later. The app never uses cookies or a login by default. If the block keeps coming back during normal daily use, you can opt in yourself with `YTDLP_ARGS=--cookies-from-browser firefox` in `.env`, but that ties downloads to your Google account, which carries its own risk.

## Development

```bash
uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -e ".[dev]"
```

```bash
.venv/bin/python -m pytest -q
```
