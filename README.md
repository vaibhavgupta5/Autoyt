# AutoYT

Cuts YouTube Shorts automatically, in small batches spread over the day, in three kinds:

| Kind | Source | Clip | Look |
|---|---|---|---|
| `podcast` | long public streams and podcasts | 50–55 s funny, informative or deep moments | split screen: speaker on top, gameplay below, captions, quiet music |
| `gta6_talk` | creators talking about GTA 6 (news, breakdowns, opinions) | 25–45 s | full screen, word captions, source speech only |
| `gta6_gameplay` | GTA gameplay videos: GTA 5 / GTA Online until 18 Nov 2026, GTA 6 from launch day (19 Nov) | 20–45 s loud action moments | full screen, hook heading, original game audio |

Volume grows only when views do, videos go public in the US evening, and a daily stats pass feeds back into what gets picked. Design, risks and status are in [PLAN.md](PLAN.md); the GTA plan and its reach addendum are in [docs/superpowers/plans/2026-11-10-gta6-expansion.md](docs/superpowers/plans/2026-11-10-gta6-expansion.md).

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

It starts at boot, checks every 15 minutes, and runs the day's batches at random times between 08:00 and 22:00 while the laptop is on (one batch per check, never a burst). Two containers run: `autoyt` (at most 3 cores and 3 GB, low priority) and `autoyt-bgutil`, a small token server that keeps YouTube from asking "are you a bot" (no cookies, no login).

**7. Optional: comments and retention stats**

```bash
docker compose run --rm -p 8765:8765 autoyt auth --extended
```

This adds two permissions: posting one question comment under each video once it is public, and reading average view percentage. Without it both features stay off by themselves.

## Everyday commands

| Command | What it does |
|---|---|
| `docker compose logs -f` | Watch what it is doing |
| `docker compose exec autoyt autoyt status` | Today's batch plan, growth levels, recent runs and clips, bot back-off, disabled sources |
| `docker compose exec autoyt autoyt stats` | Fetch view counts now and print the 7-day performance summary |
| `docker compose exec autoyt autoyt run --force` | Run all of today's batches now (resumes, never re-uploads finished clips) |
| `docker compose exec autoyt autoyt run --day 2026-11-12:gta6_talk` | Run one batch |
| `docker compose run --rm autoyt run --fresh` | Archive today's run and do a brand-new one with your normal mode (stop the daemon first: `docker compose stop`) |
| `docker compose exec autoyt autoyt check-sources` | Count eligible videos per channel |
| `touch data/PAUSE` / `rm data/PAUSE` | Pause or resume without stopping the container |
| `docker compose restart` | After changing `.env` |

Edit channels in `config/sources.yaml`, music in `config/music.yaml`, and gameplay in `config/gameplay.yaml`. Changes to `config/` are read on the next run.

## Troubleshooting

**"Sign in to confirm you're not a bot" in the logs.** YouTube is rate-limiting your IP. The pipeline now treats this as a pause for everything (2 h, then 4 h, then 8 h), never blames the video, and emails you if it lasts more than 6 hours. Check that the token server runs (`docker compose logs bgutil-provider`) and pull a newer yt-dlp (`docker compose build --pull && docker compose up -d`). The app never uses cookies or a login by default. If the block keeps coming back during normal daily use, you can opt in yourself with `YTDLP_ARGS=--cookies-from-browser firefox` in `.env`, but that ties downloads to your Google account, which carries its own risk.

## Development

```bash
uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -e ".[dev]"
```

```bash
.venv/bin/python -m pytest -q
```
