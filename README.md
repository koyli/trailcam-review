# Camera review on a Linux server

Twice a day, Claude Code reviews new footage from `~/capture/trailcam`, `~/capture/motion`
and `~/capture/ring-events` (all searched recursively; missing folders are skipped until
they appear). It sends a short summary to your phone with Pushover, leaves interesting
files where they are, and moves the rest to `~/capture/to-delete/`. Files there are
deleted 30 days after they were moved. Healthchecks.io tells you if a run fails or
doesn't happen. It all runs on your Claude plan, not on paid API tokens.

## How a run works (`run.sh`)

1. **Start:** pings Healthchecks.io.
2. **Purge:** deletes files in `to-delete` that were moved there more than 30 days ago.
3. **Scan:** `trailcam.py` collects up to 200 finished, unreviewed files and groups them
   into events (a photo and the video from the same trigger form one event). It samples
   each video at 2 frames a second, keeps the most-active moments, and builds contact
   sheets.
4. **Review:** Claude Code looks at the sheets, zooms in where needed, and writes:
   - `verdicts.tsv`: `keep` or `move` for each event, with a note, the species it
     identified confidently, and which pictures are good enough to share
   - `summary.md`: the phone summary
   - `people.txt`, if anyone was seen
   - ranked crops of the best moments
   - additions to `report.md`

   Claude itself never moves or deletes anything.
5. **File:** the script applies the verdicts. `keep` leaves files in place and records
   them as reviewed. `move` moves them to `~/capture/to-delete/<same path>`.
6. **Share:** if SmugMug is set up, the pictures marked for sharing are uploaded, one
   album per species (see below).
7. **Notify:** Pushover messages are sent, but only when there's something new:
   - **People alert** (high priority, breaks through quiet hours) whenever people,
     vehicles or dogs are seen, with the clearest photo.
   - **Summary:** short bullets (human activity, unusual wildlife, routine wildlife in
     one line, things to check by eye), the counts, and the top photo.
   - **Up to 3 more photos** as quiet messages.
8. **Finish:** tells Healthchecks.io the run succeeded, or that it failed (with the end
   of the log).

Up to 2 batches (400 files) are handled per run. A backlog carries over to the next run.

**What gets kept:** people, vehicles, dogs and any other sign of human activity; any
wild mammal; any unusual bird or animal; anything uncertain. **What gets moved:**
nothing there (wind, leaves, fog, glare) or only routine visitors, which are set in
`config.json` → `routine`. The defaults are blackbird, wood pigeon and domestic cats. Add
`"fox"` or `"badger"` there if you stop wanting those kept.

**Safety nets:**
- If Claude doesn't finish a batch (usage limit, crash), nothing is moved, the run is
  reported as failed to Healthchecks.io, and the next run finishes that batch first.
- Any event without a verdict is kept.
- Only files that this tool moved into `to-delete` (or older than 30 days by file date)
  are ever deleted.
- Files still being copied (less than 10 minutes old) and hidden partial files are
  skipped.
- If a Pushover message fails, those batches are reported again on the next run.

## Setup

1. Install the tools (Debian/Ubuntu):
   ```bash
   sudo apt update && sudo apt install -y ffmpeg python3 python3-pil cron curl
   ```
2. Install Claude Code, then run `claude` once and log in **with your Claude account**,
   not an API key:
   ```bash
   curl -fsSL https://claude.ai/install.sh | bash
   ```
3. Get the kit onto the server and create your own settings file from the template.
   `config.json` is ignored by git, so your ping URL and tweaks never get committed:
   ```bash
   git clone https://github.com/koyli/trailcam-review.git ~/trailcam-review
   ```
   ```bash
   cd ~/trailcam-review && cp config.json.in config.json
   ```
   To pick up later changes to the kit, run `git pull`. New settings in
   `config.json.in` won't be copied into your `config.json`, so compare the two after an
   update.
4. **Pushover:** on pushover.net, copy your **user key** from the dashboard, then under
   *Your Applications* create one (e.g. "Trailcams") and copy its **API token**. Store
   both privately on the server (paste each when nano opens, save, exit):
   ```bash
   mkdir -p ~/.config/trailcam && chmod 700 ~/.config/trailcam
   ```
   ```bash
   nano ~/.config/trailcam/pushover-user
   ```
   ```bash
   nano ~/.config/trailcam/pushover-token
   ```
   ```bash
   chmod 600 ~/.config/trailcam/pushover-*
   ```
5. **Healthchecks.io:** add a check named "Trailcams" with period **12 hours** and grace
   **3 hours**, plus a Pushover (or email) integration for its alerts. Copy the check's
   ping URL (`https://hc-ping.com/…`) into `config.json` → `healthchecks` → `ping_url`.
6. Check the folders are found:
   ```bash
   cd ~/trailcam-review && python3 trailcam.py status
   ```
7. Do one full run by hand, then read `logs/run-<date>.log`. The Healthchecks.io check
   should turn green.
   ```bash
   ~/trailcam-review/run.sh
   ```
8. Schedule it for 01:00 and 13:00 with `crontab -e`:
   ```
   0 1,13 * * * $HOME/trailcam-review/run.sh
   ```

If cron runs report that Claude isn't logged in, create a long-lived token with
`claude setup-token` and add `export CLAUDE_CODE_OAUTH_TOKEN=...` near the top of
`run.sh`. That keeps billing on your plan. Check the current Claude Code docs for this.
Make sure `ANTHROPIC_API_KEY` is never set (`run.sh` unsets it), otherwise you'd be
billed per token.

## Running in a container (recommended)

The container holds ffmpeg, Python and Claude Code, so nothing but Docker (or Podman) is
installed on the server. It can see only three things:
- this kit folder
- `~/capture`
- `~/.config/trailcam`, read-only

It runs as your user with no extra privileges, and is limited to 2 CPUs and 2 GB of
memory. Use this instead of steps 1, 2 and 8 above; Pushover and Healthchecks.io setup
(steps 4 and 5) are the same.

1. Install Docker with the Compose plugin (`docker compose version` should print 2.24 or
   newer). Rootless Docker or Podman is the more isolated choice: membership of the
   normal `docker` group is effectively root on the server.
2. In the kit folder, record your user and group IDs, then build the image:
   ```bash
   cd ~/trailcam-review && chmod +x run.sh trailcam.py && printf 'UID=%s\nGID=%s\n' "$(id -u)" "$(id -g)" > .env
   ```
   ```bash
   docker compose build
   ```
3. Log Claude Code in to your Claude account by creating a long-lived token, using the
   Claude Code inside the container (nothing to install on the server itself). It
   prints a link: open it in a browser on any device, approve, and paste the code
   back into the terminal.
   ```bash
   docker compose run --rm --entrypoint claude trailcam setup-token
   ```
   Then save the token it prints in `~/.config/trailcam/claude.env`, as one line
   `CLAUDE_CODE_OAUTH_TOKEN=<token>`:
   ```bash
   nano ~/.config/trailcam/claude.env && chmod 600 ~/.config/trailcam/claude.env
   ```
   Alternatively, log in interactively inside the container once. The login is kept
   in the `claude-config` volume:
   ```bash
   docker compose run --rm --entrypoint claude trailcam
   ```
4. Check it can see the footage:
   ```bash
   docker compose run --rm --entrypoint python3 trailcam trailcam.py status
   ```
5. Do one full run by hand, then read `logs/run-<date>.log`:
   ```bash
   docker compose run --rm trailcam
   ```
6. Schedule it on the server (not inside the container) with `crontab -e`:
   ```
   0 1,13 * * * cd $HOME/trailcam-review && mkdir -p logs && docker compose run --rm -T trailcam >> logs/cron.log 2>&1
   ```
   **For near-real-time alerts** (e.g. trespassers), run it every minute instead, with
   `* * * * *` in place of `0 1,13 * * *`. Then:
   - Set `max_files_per_batch` to about 100 and `max_batches_per_run` to 1, so each run
     is short and new footage isn't stuck behind a long one.
   - A run that starts while the previous one is still going just exits. When there's
     no new footage, Claude isn't started, so quiet runs take a couple of seconds and
     use none of your plan.
   - Set the Healthchecks.io check's period to 5 minutes, with about 1 hour's grace, to
     allow for the occasional long run.
   - `min_file_age_minutes` is how long a new file is left alone before it's read, so
     half-written files are skipped. It can be set per folder. The defaults are 0 for
     `ring-events` (its downloader writes `.part` files and renames them when complete),
     1 for `trailcam` (copies arrive as hidden temporary files) and 2 for `motion` (it
     writes clips while recording). Lower is faster, but only if the folder's files
     appear complete.

Healthchecks.io also catches the container failing to start, because the expected
ping never arrives. To update Claude Code or the tools, rebuild with
`docker compose build --pull --no-cache`.

## Links to the events (optional)

When `web.base_url` is set (e.g. `https://example.org/cams`), each notification links to
a page showing that alert's people, best photos, summary and kept events, with playable
clips. `<base_url>/` is a gallery of the recent pages. The pages are written to `web/` in
this folder and expire after `web.page_days` (default 30). Motion's `.mkv` clips are
converted to `.mp4` for the page, since phone browsers can't play them; other clips are
served straight from the capture folder.

A web server must serve two locations, both behind a password:
- `<base_url>/`: this kit's `web/` folder
- `<base_url>/media/`: the capture folder, read-only

For nginx, add to the HTTPS `server` block (paths as seen by nginx; in a container,
mount `~/trailcam-review/web` and `~/capture` read-only, plus the password file):

```nginx
location /cams/ {
    auth_basic           "Cameras";
    auth_basic_user_file /etc/nginx/cams.htpasswd;
    alias                /srv/cams/web/;
    add_header           X-Robots-Tag "noindex" always;
}
location /cams/media/ {
    auth_basic           "Cameras";
    auth_basic_user_file /etc/nginx/cams.htpasswd;
    alias                /srv/capture/;
    location ~ /\.       { deny all; }
}
```

Create the password file with `htpasswd -c cams.htpasswd <username>` (from the
`apache2-utils` package).

## Wildlife gallery on SmugMug (optional)

The review fills two extra columns in `verdicts.tsv`: `species` (wild animals and birds
it identified confidently, e.g. `badger` or `great tit, blue tit`) and `share` (what's
good enough to show: `photo`, the best video frame as `frame@SECONDS`, and `video`).
After each review, `trailcam.py share` uploads those to SmugMug, into an album per
species inside one folder: `Wildlife/Badger`, `Wildlife/Great tit` and so on. Folder
and albums are created as needed, unlisted by default. Rats are left out
(`smugmug.exclude`). Each picture is uploaded once, whichever species albums it goes
to; `smugmug-state.json` records what's done. Files already moved to `to-delete` are
still found.

To set it up:

1. Apply for a SmugMug API key at <https://api.smugmug.com/api/developer/apply>.
2. Put the key and secret in `~/.config/trailcam/smugmug.json`:
   `{"api_key": "...", "api_secret": "..."}`
3. Run `python3 trailcam.py smugmug-auth` on the host (it doesn't need Pillow), open
   the link it prints, allow access, and type in the 6-digit code. The access token is
   added to the same file.

Footage reviewed before this existed has no species. `python3 trailcam.py backfill`
queues the events earlier reviews kept, from `smugmug.backfill_sources` (default
`trailcam` and `motion`), as batches that are only
reviewed for species and sharing, with no notifications. Each event is queued once. The
batches go ahead of new footage until they're done, and use your plan like any review.
Ring events are mostly people, so for those, queue only the ones whose notes mention an
animal:

    python3 trailcam.py backfill --sources ring-events --match '\b(fox|badger|deer|squirrel|hedgehog|rabbit|hare|bird|owl|bat|animal)'

## Tuning (`config.json`)

| Setting | Default | Meaning |
|---|---|---|
| `routine` | blackbird, wood pigeon, domestic cats | Visitors not worth keeping |
| `pushover.people_priority` | `1` | `1` = high (bypasses quiet hours); `2` = emergency (repeats until acknowledged) |
| `pushover.summary_priority` | `0` | `-1` for a silent summary |
| `pushover.extra_photos` | `3` | Extra photos after the summary |
| `pushover.notify_when_nothing_new` | `false` | Message even when there's no new footage (Healthchecks.io already covers "is it working?") |
| `max_files_per_batch`, `max_batches_per_run` | `200`, `2` | Lower these to use less of your plan per run |
| `delete_after_days` | `30` | Days a file stays in `to-delete` |
| `smugmug.folder`, `smugmug.privacy` | `Wildlife`, `Unlisted` | Where species albums go, and who can see them (`Public`, `Unlisted`, `Private`) |
| `smugmug.exclude` | `rat` | Species never uploaded (any word of the name matches) |

## Already-reviewed footage

`reviewed.txt` lists the 4,603 trailcam files reviewed in the desktop session (by file
name), so they aren't reviewed again; they stay where they are. To have them sorted
into keep and to-delete too, delete those lines. Be aware that a backlog that size takes
about two weeks of runs and a lot of your plan allowance.

## Handy commands

| Command | What it does |
|---|---|
| `python3 trailcam.py status` | Folders found, unreviewed count, pending batches |
| `python3 trailcam.py crop FILE --t 7 --box 0,0.2,0.8,0.7` | Full-resolution zoom |
| `python3 trailcam.py burst FILE --start 1 --end 4 --fps 8` | Frame-by-frame sheet |
| `python3 trailcam.py purge` | Run the 30-day cleanup now |
| `python3 trailcam.py share` | Upload anything waiting for SmugMug now |
| `python3 trailcam.py backfill --sources trailcam` | Queue earlier kept events for species review |

To rescue a file from `to-delete`, move it back. To have it reviewed again, also make
sure its path isn't in `reviewed.txt`.
