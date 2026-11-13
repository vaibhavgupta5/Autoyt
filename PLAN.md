# AutoYT: Design and Status

Every day the system picks one long public English stream or podcast, finds the best funny, informative or deep moments with AI, renders vertical Shorts of 50–55 seconds, and schedules them on YouTube with no manual steps. It runs in one small Docker container on this laptop, makes one run per day whenever the laptop is on, retries on failure, and emails the results.

**Status (2026-11-11): GTA feed and reach work (see [the GTA plan](docs/superpowers/plans/2026-11-10-gta6-expansion.md)) built and tested: batches through the day, `gta6_talk` and `gta6_gameplay` kinds, PO-token server against the bot check, audience-time publishing, view-driven growth.** Earlier status (2026-11-08): built, tested, and live. The first 3 clips were uploaded to the channel in `scheduled` mode and verified through the YouTube API: private with `publishAt` set, going public at 16:30, 19:32 and 22:36 IST. Gemini, YouTube upload and Gmail all work. Custom thumbnails are refused (403) until the channel is phone-verified at youtube.com/verify. See [README.md](README.md) for operation.

---

## 1. Risks you accepted

| Risk | What this build does about it |
|---|---|
| **Copyright on source videos.** Channels in `config/sources.yaml` run paid clipping programs, but we did not join them, so there is no licence. Claims, strikes or takedowns are possible. | Public videos only. Every description credits and links the original with a timestamp. A video that YouTube rejects or removes for a copyright or terms reason disables its source automatically and emails you. |
| **Content ID claims.** The API cannot see a claim on a video that stays up. | Watch YouTube's copyright emails and Studio > Content > Copyright. Remove the source in `sources.yaml` when a claim lands. |
| **YouTube inauthentic and reused content rules.** Several same-format clips a day of other people's speech can lose monetization, with or without permission. | Posting ramps up slowly (3, then 5, then 8 a day), headings and titles vary, and each clip has a context line. This lowers the risk but does not remove it. |
| **Gameplay footage** is the publishers' content and downloaded gameplay is someone else's upload. | 2–3 stored videos per game, reused. If a file is claimed, delete it from `assets/gameplay/` and its entry in `index.json`. |
| **Downloading with yt-dlp** is against YouTube's Terms of Service. | Accepted. No cookies and no login are used. |
| **AI misquoting people.** | Prompt rules plus code checks: banned words, heading length, clip must end where a sentence ends when possible, no dead air. |

---

## 2. Daily flow

The container wakes every 15 minutes and starts a run only if today's run is not finished, a retry is due, the battery is above 40% (or charging), disk has at least 15 GB free, and no `data/PAUSE` file exists.

```
1. PICK        random channel, then a random never-used public video from its whole history
2. TRANSCRIPT  YouTube's own auto-captions with word timing (about 7 seconds, no download of the video)
               fallback if a video has none: download audio and run Whisper on all of it (10-30 minutes)
3. SELECT      Gemini proposes candidates for each 15-minute window (several windows at once),
               code validates and keeps the best N
4. PER CLIP    all N slices are fetched in parallel (50-55 s each, up to 720p) while clips render one by one:
               Whisper on just that slice for accurate captions, render, quality check,
               upload, set thumbnail, delete the local clip
5. FINISH      delete work files, send summary email
```

- **Why this is fast:** the slow part used to be running Whisper over the whole 1-3 hour video. YouTube's captions are good enough to FIND the clips, and Whisper only runs on the 3-8 chosen 55-second slices, so the words burned into the video stay accurate. Downloads, Gemini calls and renders overlap instead of waiting for each other, and the blurred background is rendered at a quarter of the size.
- **Only the clip sections are downloaded as video**, never the 2–3 hour file. Maximum quality is **720p**, which keeps size and time down.
- Every step is recorded in SQLite, so a crash or closed laptop lid resumes at the last finished step and never repeats an uploaded clip.
- Uploads go in as private with `publishAt`, so the laptop can be off when the videos go live.

## 3. Retries

| Level | Behavior |
|---|---|
| Step (download, render, upload) | 3 retries, waiting 1, 5, then 15 minutes |
| Whole run | Up to 4 attempts a day, 1 hour apart, resuming from the last finished step |
| Unusable video (private, wrong language, too little speech, fewer than 3 usable clips) | Marks it bad and picks another, up to 3 videos, no waiting |
| Needs you (bad Gemini key, expired YouTube login) | No retry. One email, run stops for the day |
| YouTube upload limit | Stops uploading, drops the rest of that day's clips, one email |

A day when the laptop is never on is skipped, with no catch-up batch.

---

## 4. Channels

All 11 are enabled in `config/sources.yaml`, with `permission: open_program_not_joined`. Eligible means public, English, 60–240 minutes. Counts checked on 2026-11-08.

| Channel | Style | Eligible public videos |
|---|---|---|
| IShowSpeed (videos and streams) | Funny | 848 |
| 2 Bears 1 Cave (on YMH Studios, filtered by title) | Funny | 620 |
| The Iced Coffee Hour | Informative | 419 |
| Lacy (videos and streams) | Funny | 289 |
| Nelk Boys (Full Send Podcast) | Funny | 149 |
| Mark Manson | Deep | 25 |
| Braeden | Funny | 25 |
| TJR | Informative | 20 |
| JShock | Funny | 7 |
| Jake Paul | Mixed | 1 |
| Kai Cenat | Funny | 0 |

Kai Cenat has no videos of that length and Jake Paul has one. Both are rarely picked and cost nothing. To include their shorter videos, add `min_minutes: 30` to their entries.

Picking is random by channel (equal chance unless you set `weight`), so small channels get used up quickly. Never-reused videos and a 2-day channel cooldown keep variety. Raise `weight` on big channels if you want more of them.

Not included: Joe Rogan, Theo Von, The Diary Of A CEO, Chris Williamson, Shawn Ryan, Jay Shetty, Mel Robbins, Alex Hormozi, Codie Sanchez, CaseOh. No open clipping program was found for them.

---

## 5. Clip selection

- Transcript becomes numbered sentences. Each 30-minute window (1 minute overlap) goes to `gemini-3.5-flash-lite` with rules: funny, informative or deep only, a hook in the first 3 seconds, a complete ending, no ads or intros, honest headings, no medical or financial advice.
- **Code validates**, not the AI: duration forced into 50.0–55.0 seconds (ending on a sentence boundary when possible), no dead air over 3.5 s, at least 80 words, no overlaps, no more than 3 clips per 15 minutes of video, unique headings, banned words, mixed categories, best-scoring first without repeating a category back to back.
- If fewer than the target survive, it asks again once with more candidates. If a video yields fewer than 3 usable clips, it is dropped and another is picked.

## 6. Video

1080×1920, 30 fps, H.264 CRF 21, AAC.

```
┌────────────────────┐
│ HEADING box        │  on screen from frame one (so the first-frame thumbnail looks finished)
│ via <credit>       │
│  podcast / stream  │  1080×1150
├─ word captions ────┤  current word highlighted
│  gameplay (muted)  │  1080×770, random game, file and start point
└────────────────────┘
```

- **Layouts per channel:** `fit_blur` (whole frame over a blurred copy, best for multi-person podcasts) or `center_crop` (IRL streamers). `custom` takes `crop: [x, y, w, h]`.
- **Gameplay quality rules:** only videos with a true 720p-class stream are accepted (short side at least 700 px, checked against the real format list), and portrait videos are tried first because they are natively sharp. The first two files I downloaded looked blurry and cut off the runner. Cause: my "720p" cap was by height, which silently picked the 360p version of portrait videos, and the frame was stretched about 2.7 times and cropped from the middle. Now portrait footage fills the panel width at no more than 1.6 times upscale and keeps the lower part of the frame where the runner is. Low-resolution footage is shown whole over a blurred copy instead of being stretched.
- **Gameplay library:** 3 Subway Surfers and 3 Temple Run 2 files, each only **60 seconds** (about 20–30 MB, muted, 1080×770), taken 5 minutes into a no-commentary video. A clip uses one of them from a random start within the first few seconds. That keeps storage tiny, but there are only 6 distinct backgrounds, so they repeat across clips. Raise `count` in `config/gameplay.yaml` and re-run `fetch-gameplay` for more variety at about 20 MB each.
- **Music:** one of 7 intense, driving Kevin MacLeod tracks at random (Volatile Reaction, Cut and Run, Rocket Power, Aggressor, Heroic Age, Crossing the Chasm, Prelude and Action). They were picked by measuring 12 candidates for loudness and steadiness and keeping the most intense. Each track's loudness is measured at download and brought to the same level (−24 LUFS) under speech at −14 LUFS, and the mix ducks the music further while someone is talking. They are CC BY, so the credit is added to each description as plain text.
- **Descriptions have no links at all.** A description is: a context line, the AI's description, "Clip from <channel>: <source title>", the credit and removal note, the music credit as text, and up to 5 hashtags. Any URL or bare domain the model writes is stripped in code. Instead, each clip gets 5–8 search **tags** from Gemini (topic, people discussed) plus the basics (shorts, category, channel), sent in YouTube's tags field. Trade-off: the licence link CC BY normally asks for is not in the description, and neither is a link back to the source video, as you asked. Put a link back in `CC_CREDIT` in `autoyt/assets.py` if you want the strictest attribution.
- **Quality check before upload:** size, duration, audio present and not silent, no long black stretch.
- **Thumbnail:** the first frame, set with `thumbnails.set`. If YouTube refuses it, the upload still stands.

---

## 7. Uploading and space

- Upload is private with `publishAt`. First clip about 30 minutes after upload, then one every 171 minutes at 8 clips (gap scales with count, 45–180 minutes), plus a few minutes of jitter.
- **Local clips are deleted as soon as the upload and thumbnail finish.** YouTube holds the scheduled video from then on. Source work files are deleted after the run. In `dry_run` mode clips stay for 3 days so you can review them.
- Only the gameplay and music libraries, the Whisper model, and the database stay long term.
- **Ramp-up** after the first real upload: 3 clips a day for 7 days, 5 for the next 7, then `CLIPS_PER_DAY` (8). Set `RAMP_UP=false` to skip it.
- **Monitor:** at the start of each run it checks the last 14 days of uploads. Rejected, failed or removed videos trigger an email, and copyright or terms rejections disable that source.
- `AUTOYT_MODE`: `scheduled` (default: uploaded now, each video goes **public** at its time slot), `public` (published public immediately), `dry_run` (render only, to review first), `private` (kept private, tests only). Nothing is private by default. YouTube shows a scheduled video as private until its publish time, then it is public.

## 8. Email

| Email | When |
|---|---|
| Daily summary | After each run: source, titles, publish times, Studio links, free disk |
| Run failed | After the day's 4 attempts are used, with the last log lines |
| Action needed | Bad or missing key, expired YouTube login, upload limit, removed or rejected videos |

At most one email per kind per day. If SMTP itself fails it is logged and the pipeline carries on. `autoyt test-email` checks your settings.

## 9. Project layout

```
autoyt/            Python package: sources, ytdlp, transcribe, select_clips, validate, subtitles,
                   render, qc, assets, upload, monitor, pipeline, scheduler, notify, main
config/            sources.yaml  gameplay.yaml  music.yaml  settings.yaml
assets/            music/  gameplay/  fonts/
data/              state.db, models/, work/, output/   (gitignored)
secrets/           client_secret.json, token.json      (gitignored)
tests/             52 tests (validator, YouTube captions parsing, tags and links, slots, ramp-up, battery, picking, Gemini parsing, gameplay framing, full pipeline with fakes)
Dockerfile  docker-compose.yml  .env.example  README.md
```

## 10. Verified so far

- **Real end-to-end dry run in the container (2026-11-08):** the daemon started, picked a video, transcribed it, asked Gemini, rendered 3 clips, passed quality checks, and emailed the summary. Nothing was uploaded because the mode was `dry_run`. Findings: the first video (a vlog) was dropped because Gemini returned almost nothing, so I changed the prompt to demand the requested count, shortened windows to 15 minutes, added a score floor in code, switched to Gemini structured output (18 of 18 calls valid versus 1 bad reply in 12 before), and cached transcripts per video so a retry never repeats transcription. Timing on this laptop: `small.en` transcribes at roughly 0.15–0.3 times real time (a 2-hour video took about 25 minutes while the browser was also busy), Gemini about 2 seconds per window, and each clip about 2 minutes to download and render.
- **Speed (measured 2026-11-08, fresh 3-clip run in the container):** 6 minutes 16 seconds from start to the summary email, versus roughly 45 minutes before. Picking and reading YouTube's captions took about 6 seconds (it was 11-25 minutes of Whisper), Gemini about 3 seconds for all windows at once, and each clip about 75-80 seconds from slice to upload (Whisper on the slice about 8 seconds, render, quality check, upload). The first slice download is the slowest single step at about 1.5 minutes. An 8-clip day should take roughly 15 minutes. Videos without captions fall back to the old full-Whisper path.
- 52 tests pass, including a full day with fake YouTube, Gemini and Whisper but real ffmpeg: scheduled upload, local clips deleted, dry run, resume after a crash, upload limit, replacing an unusable video, action-needed stop.
- A real 60 s slice of a public podcast was downloaded at 720p, transcribed with Whisper, and rendered. Captions, heading box, credit and music were checked by eye.
- The Docker image builds. Inside it ffmpeg, libass and yt-dlp with deno work, YouTube lookups succeed, and the daemon starts, honors `data/PAUSE`, runs as your user, and reads the battery.
- Channel handles resolved and counted against real YouTube data.

**Not verified, needs your credentials:** Gemini responses, YouTube upload and thumbnail, OAuth login, SMTP.

## 11. Decision left

**Goal: monetization or reach?** It does not block anything now. If monetization matters, consider lowering `CLIPS_PER_DAY` and adding clips with real added value, because that is where YouTube's inauthentic-content rules bite.

---

## References

- YouTube API quota costs: https://developers.google.com/youtube/v3/determine_quota_cost
- `videos.insert`: https://developers.google.com/youtube/v3/docs/videos/insert
- Common upload errors and channel caps: https://support.google.com/youtube/answer/10383400
- Inauthentic content overview (third-party): https://air.io/en/monetization/youtube-monetization-policy-changes-2026-a-complete-dated-timeline
- Open clipping programs (third-party): https://www.clyper.io/clipping-programs
- Kevin MacLeod license FAQ: https://incompetech.com/music/royalty-free/faq.html
- Gemini models: https://aistudio.google.com/docs/models
