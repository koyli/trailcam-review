#!/usr/bin/env python3
"""Camera triage: turn new footage into labelled contact sheets, then file the results.

Needs: python3, ffmpeg/ffprobe on PATH, Pillow (apt install python3-pil).
All state lives next to this script; settings are in config.json (copied from config.json.in).

  status                 unreviewed files and pending batches
  next                   print the batch to review (pending one, or a new one); empty if none
  prompt BATCH           print Claude's instructions for that batch
  crop FILE --box x,y,w,h [--t SEC]
                         full-resolution zoom (box as fractions 0-1 of the frame)
  burst FILE --start A --end B [--fps N] [--box x,y,w,h]
                         frame-by-frame sheet of part of a video
  done BATCH [--force]   apply verdicts: keep in place, or move to to-delete
  purge                  delete to-delete files older than delete_after_days; prints the count
  clear-skipped [--dry-run]
                         move clips of the skip_kinds types to to-delete
  notify [--purged N]    send Pushover messages for batches not yet reported
  checks                 apply the answers given on the alert pages to "check by eye" events
  serve [--port N]       small web service that records those answers (behind the web server)
  share                  upload good pictures of identified wildlife to SmugMug
  backfill [--sources S ...] [--match REGEX] [--reshare]
                         queue already-kept events for a species-only review, for SmugMug
  smugmug-auth           one-off SmugMug login (runs without Pillow, e.g. on the host)
"""
import argparse
import base64
import hashlib
import hmac
import html
import http.server
import json
import os
import re
import secrets
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # only smugmug-auth works without Pillow
    Image = ImageDraw = ImageFont = None

HERE = Path(__file__).resolve().parent
if not (HERE / "config.json").exists():
    sys.exit(f"No config.json: copy {HERE / 'config.json.in'} to config.json and edit it.")
CONFIG = json.loads((HERE / "config.json").read_text())
ROOT = Path(os.path.expanduser(CONFIG["capture_root"]))
SOURCES = [ROOT / s for s in CONFIG["sources"]]
TO_DELETE = ROOT / CONFIG["to_delete_dir"]
MANIFEST = TO_DELETE / ".moved.tsv"
BATCHES = HERE / "batches"
REVIEWED = HERE / "reviewed.txt"
ZOOMS = HERE / "zooms"
DECISIONS = HERE / "decisions"   # answers given on the alert pages, written by serve

IMAGE_EXT = {".jpg", ".jpeg", ".png"}
VIDEO_EXT = {".mp4", ".mkv", ".avi", ".mov", ".m4v"}

# Motion scoring works on tiny grayscale frames.
SW, SH = 128, 72      # large enough that a small bird far from the camera covers a few pixels
SH_USED = 66          # ignore the bottom rows, where camera text overlays sit
DIFF = 1.2            # per-pixel threshold, in standard deviations
MIN_BLOB = 5          # blob size (pixels) that counts as "something moved"
MAX_SAMPLES = 120     # long clips are sampled more sparsely than 2 fps
EVENT_GAP = 5         # seconds; files from one camera this close together are one event
TRAILCAM_RE = re.compile(r"^([A-Z0-9]+)_(\d{8})(\d{6})_(\d+)(?:\D.*)?$", re.I)  # "_o (1)": a copy
DATE_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_PREFIX_RE = re.compile(r"^(\d{6})(?:\D|$)")
DATETIME_PREFIX_RE = re.compile(r"^(\d{14})(?:\D|$)")
RING_KIND_RE = re.compile(r"^\d{6}-([a-z_]+)-\d+$")


def log(msg):
    print(msg, flush=True)


# ---------- files and state ----------

def rel(p):
    return p.relative_to(ROOT).as_posix()


def reviewed_sets():
    """Relative paths, plus bare names from the original desktop review (trailcam only)."""
    paths, legacy = set(), set()
    if REVIEWED.exists():
        for line in REVIEWED.read_text().splitlines():
            line = line.strip()
            if line:
                (paths if "/" in line else legacy).add(line)
    return paths, legacy


def files_field(paths):
    """The space-separated files column of verdicts.tsv. Spaces (and %) in names are
    %-encoded, so a name like "IMG_1 (1).JPG" stays one file."""
    return " ".join(rel(p).replace("%", "%25").replace(" ", "%20") for p in paths)


def event_files(field):
    """The relative paths in a files column (see files_field)."""
    return [urllib.parse.unquote(f) for f in field.split()]


def is_reviewed(r, paths, legacy):
    return r in paths or (r.startswith(CONFIG["sources"][0] + "/") and r.rsplit("/", 1)[-1] in legacy)


def event_kind(p):
    """Ring's event type from <HHMMSS>-<kind>-<id>.mp4 (motion, ding, on_demand, ...); else None."""
    m = RING_KIND_RE.match(p.stem)
    return m.group(1) if m else None


def skipped(p):
    return event_kind(p) in CONFIG.get("skip_kinds", [])


def min_file_age(source):
    """Minutes a file must be left alone before it's read, so half-written files are skipped.
    min_file_age_minutes is either one number, or per source folder with a "default"."""
    v = CONFIG.get("min_file_age_minutes", 10)
    if isinstance(v, dict):
        return v.get(source, v.get("default", 10))
    return v


def candidate_files(include_skipped=False):
    """Finished media files under the source folders (missing folders are skipped).
    Event types listed in skip_kinds (e.g. Ring live view) are left alone, unreviewed."""
    now = time.time()
    out = []
    for src in SOURCES:
        if not src.is_dir():
            continue
        min_age = min_file_age(src.name) * 60
        for p in src.rglob("*"):
            if not p.is_file() or any(part.startswith(".") for part in p.relative_to(ROOT).parts):
                continue
            if p.suffix.lower() not in IMAGE_EXT | VIDEO_EXT:
                continue
            if now - p.stat().st_mtime < min_age:
                continue
            if not include_skipped and skipped(p):
                continue
            out.append(p)
    return out


def unreviewed():
    paths, legacy = reviewed_sets()
    return [p for p in candidate_files() if not is_reviewed(rel(p), paths, legacy)]


def pending_batches():
    if not BATCHES.exists():
        return []
    return sorted(b for b in BATCHES.iterdir() if b.is_dir() and not (b / "DONE").exists())


def batch_path(name):
    b = Path(name)
    if b.is_absolute() or b.is_dir():
        return b.resolve()
    return BATCHES / b.name


def capture_time(p):
    """When the clip was recorded: from the name where possible, since files are often
    downloaded in bulk and their modification times say nothing about the event."""
    m = TRAILCAM_RE.match(p.stem)
    if m:
        try:
            return datetime.strptime(m.group(2) + m.group(3), "%Y%m%d%H%M%S")
        except ValueError:
            pass
    # motion: <YYYYmmddHHMMSS>-<event>.mkv
    t = DATETIME_PREFIX_RE.match(p.stem)
    if t:
        try:
            return datetime.strptime(t.group(1), "%Y%m%d%H%M%S")
        except ValueError:
            pass
    # Ring downloads: <camera>/<YYYY-MM-DD>/<HHMMSS>-<kind>-<id>.mp4 (UTC)
    d, t = DATE_DIR_RE.match(p.parent.name), TIME_PREFIX_RE.match(p.stem)
    if d and t:
        try:
            return datetime.strptime(d.group(0) + t.group(1), "%Y-%m-%d%H%M%S")
        except ValueError:
            pass
    return datetime.fromtimestamp(p.stat().st_mtime)


def camera_of(p):
    """Trail cameras name themselves in the file name; elsewhere the camera is the
    folder directly under the source (motion/anpr/..., ring-events/front-door/...)."""
    m = TRAILCAM_RE.match(p.stem)
    if m:
        return m.group(1).upper()
    parts = p.relative_to(ROOT).parts
    return f"{parts[0]}:{parts[1]}" if len(parts) > 2 else parts[0]


# ---------- video helpers ----------

def duration(video):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", str(video)], capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return None


def ffmpeg_frame(video, t, out, vf=None):
    cmd = ["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.2f}", "-i", str(video),
           "-map", "0:v:0", "-frames:v", "1", "-q:v", "2"]
    if vf:
        cmd += ["-vf", vf]
    cmd.append(str(out))
    return subprocess.run(cmd, capture_output=True).returncode == 0 and Path(out).exists()


def tiny_frames(video, fps):
    cmd = ["ffmpeg", "-v", "error", "-i", str(video), "-map", "0:v:0",
           "-vf", f"fps={fps},scale={SW}:{SH},format=gray", "-f", "rawvideo", "-"]
    r = subprocess.run(cmd, capture_output=True)
    n = SW * SH
    return [r.stdout[i:i + n] for i in range(0, len(r.stdout) - n + 1, n)]


def normalise(frame):
    m = sum(frame) / len(frame)
    sd = (sum((x - m) ** 2 for x in frame) / len(frame)) ** 0.5 or 1
    return [(x - m) / sd for x in frame]


def biggest_blob(mask):
    seen = set()
    best = 0
    for p in range(SW * SH_USED):
        if not mask[p] or p in seen:
            continue
        stack, size = [p], 0
        seen.add(p)
        while stack:
            q = stack.pop()
            size += 1
            x, y = q % SW, q // SW
            for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                if 0 <= nx < SW and 0 <= ny < SH_USED:
                    r = ny * SW + nx
                    if mask[r] and r not in seen:
                        seen.add(r)
                        stack.append(r)
        best = max(best, size)
    return best


def motion_scores(video):
    """(seconds per sample, per-sample size of the largest region unlike the clip's background)."""
    d = duration(video)
    fps = 2.0 if not d else min(2.0, MAX_SAMPLES / d)
    if d and d * fps < 4:
        fps = 4 / d    # very short clips (motion often records under a second): still get 4 samples
    frames = [normalise(f) for f in tiny_frames(video, fps)]
    if len(frames) < 3:
        return 1 / fps, []
    bg = [statistics.median(f[p] for f in frames) for p in range(SW * SH)]
    return 1 / fps, [biggest_blob([abs(f[p] - bg[p]) > DIFF for p in range(SW * SH_USED)]) for f in frames]


def pick_times(step, scores, k):
    """Up to k of the most-active moments (seconds), at least 1.5 s apart."""
    gap = max(1, round(1.5 / step))
    order = sorted(range(len(scores)), key=lambda i: -scores[i])
    picks = []
    for i in order:
        if scores[i] < MIN_BLOB:
            break
        if all(abs(i - j) >= gap for j in picks):
            picks.append(i)
        if len(picks) == k:
            break
    return [(i * step, scores[i]) for i in sorted(picks)]


def spread_times(step, scores, n, taken):
    """n moments spread evenly through the clip, skipping any within 1.5 s of one already taken,
    so something motion detection missed (a small or distant animal) still gets looked at."""
    out = []
    for j in range(1, n + 1):
        i = round(j * (len(scores) - 1) / (n + 1))
        t = i * step
        if all(abs(t - u) >= 1.5 for u, _ in taken + out):
            out.append((t, scores[i]))
    return out


# ---------- sheets ----------

def font(size):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def make_sheet(tiles, out, cols=6, tw=320):
    """tiles: list of (image path or None, label). 16:9 tiles with a yellow label."""
    th = tw * 9 // 16
    rows = max(1, (len(tiles) + cols - 1) // cols)
    sheet = Image.new("RGB", (cols * tw, rows * th), "black")
    draw = ImageDraw.Draw(sheet)
    f = font(15)
    for i, (path, label) in enumerate(tiles):
        x, y = (i % cols) * tw, (i // cols) * th
        if path:
            try:
                im = Image.open(path).convert("RGB")
                im.thumbnail((tw - 2, th - 2))
                sheet.paste(im, (x, y))
            except Exception:
                pass
        box = draw.textbbox((x + 4, y + 2), label, font=f)
        draw.rectangle((box[0] - 2, box[1] - 1, box[2] + 2, box[3] + 1), fill="black")
        draw.text((x + 4, y + 2), label, fill="yellow", font=f)
    sheet.save(out, quality=82)


# ---------- commands ----------

def cmd_status(_):
    log(f"Capture folder: {ROOT}")
    for s in SOURCES:
        log(f"  {'found  ' if s.is_dir() else 'missing'} {s}")
    log(f"Unreviewed finished files: {len(unreviewed())}")
    n = sum(skipped(p) for p in candidate_files(include_skipped=True))
    if n:
        log(f"Skipped by type ({', '.join(CONFIG.get('skip_kinds', []))}): {n}")
    for b in pending_batches():
        log(f"PENDING batch: {b}")


def group_events(files):
    """Consecutive files from the same folder and camera, a few seconds apart, form one event."""
    items = sorted(((camera_of(p), p.parent, capture_time(p), p) for p in files),
                   key=lambda t: (str(t[1]), t[0], t[2]))
    events, last = [], None
    for cam, parent, ts, p in items:
        if last and last[0] == cam and last[1] == parent and (ts - last[2]).total_seconds() <= EVENT_GAP:
            events[-1]["files"].append(p)
        else:
            events.append({"camera": cam, "time": ts, "files": [p]})
        last = (cam, parent, ts)
    events.sort(key=lambda e: e["time"])
    return events


def new_batch():
    todo = sorted(unreviewed(), key=capture_time)[:CONFIG.get("max_files_per_batch", 200)]
    if not todo:
        return None
    return build_batch(group_events(todo))


def build_batch(events, verdicts=None):
    """Sheets, index and verdict rows for events (as from group_events). verdicts, if
    given, pre-fills (verdict, note) for each event."""
    name = datetime.now().strftime("%Y%m%d-%H%M%S")
    batch, n = BATCHES / name, 1
    while batch.exists():  # backfill makes several a second
        batch, n = BATCHES / f"{name}-{n}", n + 1
    frames_dir = batch / "frames"
    frames_dir.mkdir(parents=True)
    (batch / "attach").mkdir()
    per_clip = CONFIG.get("frames_per_clip", 4)
    spread = CONFIG.get("spread_frames_per_clip", 3)

    tiles, index, problems = [], [], []
    for e_no, ev in enumerate(events):
        eid = f"E{e_no}"
        head = f"{eid} {ev['camera']} {ev['time']:%d/%m %H:%M}"
        for p in ev["files"]:
            r = rel(p)
            if p.suffix.lower() in IMAGE_EXT:
                out = frames_dir / f"{len(tiles):04d}.jpg"
                try:
                    im = Image.open(p)
                    im.thumbnail((640, 640))
                    im.convert("RGB").save(out, quality=80)
                    index.append((len(tiles), eid, r, ""))
                    tiles.append((out, f"{head} still"))
                except Exception as e:
                    problems.append(f"{r}: unreadable image ({e})")
                continue
            step, scores = motion_scores(p)
            if not scores:
                problems.append(f"{r}: could not decode video (corrupt or partial)")
                continue
            picks = pick_times(step, scores, per_clip)
            picks = sorted(picks + spread_times(step, scores, spread, picks))
            for t, sc in picks:
                out = frames_dir / f"{len(tiles):04d}.jpg"
                if ffmpeg_frame(p, t, out, "scale=640:-2"):
                    index.append((len(tiles), eid, r, f"{t:.1f}"))
                    tiles.append((out, f"{head} @{t:.1f}s m{sc}"))

    for s in range(0, len(tiles), 36):
        make_sheet(tiles[s:s + 36], batch / f"sheet-{s // 36 + 1:02d}.jpg")
    with open(batch / "index.tsv", "w") as f:
        f.write("tile\tevent\tfile\tseconds\n")
        for row in index:
            f.write("\t".join(map(str, row)) + "\n")
    with open(batch / "verdicts.tsv", "w") as f:
        f.write("event\tverdict\tnote\tfiles\tspecies\tshare\tcheck\n")
        for e_no, ev in enumerate(events):
            verdict, note = verdicts[e_no] if verdicts else ("?", "")
            f.write(f"E{e_no}\t{verdict}\t{note}\t{files_field(ev['files'])}\t\t\t\n")
    (batch / "problems.txt").write_text("\n".join(problems) + ("\n" if problems else ""))
    return batch


def cmd_next(_):
    pend = pending_batches()
    b = pend[0] if pend else new_batch()
    if b:
        print(b)


def cmd_prompt(args):
    batch = batch_path(args.batch)
    text = (HERE / "PROMPT.md").read_text()
    text = text.replace("{{BATCH}}", str(batch))
    text = text.replace("{{ROUTINE}}", ", ".join(CONFIG.get("routine", [])) or "(none)")
    known = sorted(smug_state().get("albums", {}))
    text = text.replace("{{SPECIES}}", ", ".join(known) if known else
                        "(none yet; for example fox, badger, roe deer, grey squirrel, pheasant, robin)")
    if (batch / "BACKFILL").exists():
        text = BACKFILL_NOTE + text
    print(text)


BACKFILL_NOTE = """**This batch was reviewed before.** Its verdicts and notes are already filled in:
leave them exactly as they are. Only fill in the `species` and `share` columns (step 3)
for every event, zooming in as usual, then write `summary.md` with the single line
`- Backfill: species and sharing added.` Skip steps 4 to 6.

"""


def parse_box(s):
    x, y, w, h = (float(v) for v in s.split(","))
    return x, y, w, h


def resolve(name):
    """A path as given in index.tsv (relative to the capture folder), or a bare file name
    if it's unique: two cameras can produce identically named files."""
    for p in (Path(name), ROOT / name):
        if p.exists():
            return p.resolve()
    hits = [h for src in SOURCES if src.is_dir() for h in src.rglob(Path(name).name)]
    if len(hits) == 1:
        return hits[0]
    if hits:
        sys.exit(f"{name} matches several files; use the path from index.tsv:\n"
                 + "\n".join(f"  {rel(h)}" for h in hits))
    sys.exit(f"Not found: {name}")


def zoom_stem(src):
    """Output name that keeps the camera folder, so same-named clips don't overwrite."""
    try:
        return rel(src).rsplit(".", 1)[0].replace("/", "_")
    except ValueError:
        return src.stem


def cmd_crop(args):
    src = resolve(args.file)
    ZOOMS.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        if src.suffix.lower() in VIDEO_EXT:
            full = Path(td) / "f.png"
            if not ffmpeg_frame(src, args.t or 0, full):
                sys.exit("Could not read that frame")
            im = Image.open(full)
        else:
            im = Image.open(src)
        im = im.convert("RGB")
        if args.box:
            x, y, w, h = parse_box(args.box)
            W, H = im.size
            im = im.crop((int(x * W), int(y * H), int((x + w) * W), int((y + h) * H)))
        im.thumbnail((1200, 1200))
        out = ZOOMS / f"{zoom_stem(src)}_{args.t or 0:.1f}_{(args.box or 'full').replace(',', '-')}.jpg"
        im.save(out, quality=88)
    log(str(out))


def cmd_burst(args):
    src = resolve(args.file)
    ZOOMS.mkdir(exist_ok=True)
    vf = f"fps={args.fps}"
    if args.box:
        x, y, w, h = parse_box(args.box)
        vf += f",crop=iw*{w}:ih*{h}:iw*{x}:ih*{y}"
    with tempfile.TemporaryDirectory() as td:
        subprocess.run(["ffmpeg", "-v", "error", "-ss", str(args.start), "-to", str(args.end),
                        "-i", str(src), "-map", "0:v:0", "-vf", vf + ",scale=480:-2",
                        f"{td}/f%03d.jpg"], check=False)
        frames = sorted(Path(td).glob("f*.jpg"))
        tiles = [(f, f"+{args.start + i / args.fps:.2f}s") for i, f in enumerate(frames)]
        out = ZOOMS / f"{zoom_stem(src)}_burst_{args.start}-{args.end}.jpg"
        make_sheet(tiles, out, cols=6, tw=400)
    log(str(out))


def move_to_delete(p):
    """Move a clip to the same relative place under to-delete. Sidecar files such as Ring's
    <name>.json stay where they are: the downloader uses them to know which events it
    already has, and without them it downloads the clip again."""
    dest = TO_DELETE / rel(p)
    dest.parent.mkdir(parents=True, exist_ok=True)
    n = 1
    while dest.exists():
        dest = dest.with_name(f"{p.stem}.{n}{p.suffix}")
        n += 1
    shutil.move(str(p), str(dest))
    with open(MANIFEST, "a") as f:
        f.write(f"{datetime.now().isoformat(timespec='seconds')}\t{dest.relative_to(TO_DELETE).as_posix()}\n")


def cmd_done(args):
    batch = batch_path(args.batch)
    rows = [l.split("\t") for l in (batch / "verdicts.tsv").read_text().splitlines()[1:] if l.strip()]
    unanswered = [r[0] for r in rows if len(r) < 2 or r[1].strip().lower() not in ("keep", "move")]
    if not (batch / "summary.md").exists() and not args.force:
        sys.exit(f"{batch.name}: summary.md missing; review not finished, batch left pending")
    if unanswered and not args.force:
        sys.exit(f"{batch.name}: {len(unanswered)} events have no keep/move verdict; batch left pending")

    kept_files, moved_files, kept_events, moved_events, missing = [], [], 0, 0, 0
    TO_DELETE.mkdir(parents=True, exist_ok=True)
    for r in rows:
        verdict = r[1].strip().lower() if len(r) > 1 else ""
        files = event_files(r[3]) if len(r) > 3 else []
        move = verdict == "move"     # anything unanswered stays where it is
        kept_events += not move
        moved_events += move
        for f in files:
            p = ROOT / f
            if not p.exists():
                missing += 1
                continue
            if move:
                move_to_delete(p)
                moved_files.append(f)
            else:
                kept_files.append(f)
    if kept_files:
        with open(REVIEWED, "a") as f:
            f.write("\n".join(kept_files) + "\n")
    stats = {"events": len(rows), "kept_events": kept_events, "moved_events": moved_events,
             "kept_files": len(kept_files), "moved_files": len(moved_files),
             "missing_files": missing, "unanswered": len(unanswered)}
    (batch / "stats.json").write_text(json.dumps(stats, indent=1))
    (batch / "DONE").write_text(datetime.now().isoformat() + "\n")
    if (batch / "BACKFILL").exists():  # already reported when first reviewed
        (batch / "NOTIFIED").write_text(datetime.now().isoformat() + "\n")
    log(f"{batch.name}: kept {len(kept_files)} files ({kept_events} events), "
        f"moved {len(moved_files)} files ({moved_events} events) to {TO_DELETE}")


def cmd_clear_skipped(args):
    """Move every finished clip of a skipped type (e.g. Ring live view) to to-delete,
    so it's purged with everything else after delete_after_days."""
    clips = [p for p in candidate_files(include_skipped=True) if skipped(p)]
    size = sum(p.stat().st_size for p in clips)
    log(f"{'Would move' if args.dry_run else 'Moving'} {len(clips)} clips "
        f"({size / 1e9:.1f} GB) of types: {', '.join(CONFIG.get('skip_kinds', []))}")
    if args.dry_run:
        for p in clips[:5]:
            log(f"  e.g. {rel(p)}")
        return
    TO_DELETE.mkdir(parents=True, exist_ok=True)
    for p in clips:
        move_to_delete(p)
    log(f"Moved {len(clips)} clips to {TO_DELETE}")


def purge_sidecars():
    """Delete sidecar files (e.g. Ring's .json) whose event is older than sidecar_max_age_days
    (by the date and time in the path) and whose clip is no longer beside them. The Ring
    downloader only looks back 7 days, so after that they're no longer needed to stop it
    re-downloading. Sidecars next to kept clips, and anything without a dated name, stay."""
    days = CONFIG.get("sidecar_max_age_days")
    if not days:
        return 0
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
    deleted = 0
    for src in SOURCES:
        if not src.is_dir():
            continue
        for p in list(src.rglob("*")):
            if (not p.is_file() or p.suffix.lower() in IMAGE_EXT | VIDEO_EXT
                    or any(part.startswith(".") for part in p.relative_to(ROOT).parts)):
                continue
            if not (DATE_DIR_RE.match(p.parent.name) and TIME_PREFIX_RE.match(p.stem)):
                continue
            if capture_time(p) >= cutoff:
                continue
            if any(p.with_suffix(ext).exists() for ext in IMAGE_EXT | VIDEO_EXT | {e.upper() for e in IMAGE_EXT | VIDEO_EXT}):
                continue
            p.unlink()
            deleted += 1
        # Remove date folders left empty (never the camera folders: the downloader checks those).
        for d in sorted((d for d in src.rglob("*") if d.is_dir() and DATE_DIR_RE.match(d.name)),
                        key=lambda d: -len(d.parts)):
            if not any(d.iterdir()):
                d.rmdir()
    return deleted


def cmd_purge(_):
    """Delete to-delete files moved more than delete_after_days ago (by manifest; else by file age),
    and old sidecar files whose clip has gone. Prints the number deleted from to-delete."""
    n = purge_sidecars()
    if n:
        print(f"Deleted {n} old sidecar files", file=sys.stderr)
    n = purge_pages()
    if n:
        print(f"Deleted {n} expired alert pages", file=sys.stderr)
    if not TO_DELETE.is_dir():
        print(0)
        return
    cutoff = datetime.now() - timedelta(days=CONFIG.get("delete_after_days", 30))
    moved_at = {}
    if MANIFEST.exists():
        for line in MANIFEST.read_text().splitlines():
            if "\t" in line:
                ts, r = line.split("\t", 1)
                moved_at[r] = datetime.fromisoformat(ts)
    deleted = 0
    for p in TO_DELETE.rglob("*"):
        if not p.is_file() or p == MANIFEST:
            continue
        r = p.relative_to(TO_DELETE).as_posix()
        when = moved_at.get(r) or datetime.fromtimestamp(p.stat().st_mtime)
        if when < cutoff:
            p.unlink()
            moved_at.pop(r, None)
            deleted += 1
    for d in sorted((d for d in TO_DELETE.rglob("*") if d.is_dir()), key=lambda d: -len(d.parts)):
        if not any(d.iterdir()):
            d.rmdir()
    if MANIFEST.exists():
        MANIFEST.write_text("".join(f"{t.isoformat(timespec='seconds')}\t{r}\n"
                                    for r, t in moved_at.items() if (TO_DELETE / r).exists()))
    print(deleted)


def secret(path):
    return Path(os.path.expanduser(path)).read_text().strip()


def pushover(title, message, priority=0, image=None, url=None, url_title="Open on the web"):
    """Send one Pushover message (HTML allowed). Raises on failure so run.sh can report it."""
    p = CONFIG["pushover"]
    fields = {"token": secret(p["app_token_file"]), "user": secret(p["user_key_file"]),
              "title": title[:250], "message": message[:1024], "html": "1", "priority": str(priority)}
    if priority >= 2:
        fields.update(retry="300", expire="3600")
    if url:
        fields.update(url=url, url_title=url_title)
    boundary = uuid.uuid4().hex
    body = b""
    for k, v in fields.items():
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n").encode()
    if image:
        data = image_for_pushover(image)
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"attachment\"; "
                 f"filename=\"{image.name}\"\r\nContent-Type: image/jpeg\r\n\r\n").encode() + data + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(p.get("api_url", "https://api.pushover.net/1/messages.json"), data=body,
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        if json.loads(r.read().decode()).get("status") != 1:
            raise RuntimeError("Pushover rejected the message")


def image_for_pushover(path):
    """JPEG bytes, shrunk if needed to stay well under Pushover's attachment limit."""
    data = path.read_bytes()
    if len(data) < 2_000_000:
        return data
    im = Image.open(path).convert("RGB")
    im.thumbnail((1600, 1600))
    out = Path(tempfile.mkstemp(suffix=".jpg")[1])
    im.save(out, quality=80)
    data = out.read_bytes()
    out.unlink()
    return data


def md_to_html(text):
    """The summary is short Markdown; Pushover understands <b> and plain line breaks."""
    text = html.escape(text, quote=False)
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)


# ---------- web pages for alerts ----------
# Each notification gets a page at <base_url>/a/<token>/ (summary, people, kept events with
# photos and playable clips), and <base_url>/ is a gallery of the recent pages. The pages are
# written into web/ next to this script; a web server serves web/ at <base_url>/ and the
# capture folder, read-only, at <base_url>/media/ (both behind a password, see README).

WEB = HERE / "web"

PAGE_CSS = """
:root{--bg:#fafaf8;--fg:#1d1d1b;--card:#fff;--line:#e3e1da;--muted:#5f5e58;--link:#1a5fb4;--alert:#c9302c}
@media (prefers-color-scheme:dark){:root{--bg:#151513;--fg:#ecebe6;--card:#22221f;--line:#3a3a35;
  --muted:#b3b1aa;--link:#8ab4f8;--alert:#ff6b66}}
body{font:16px/1.45 -apple-system,system-ui,sans-serif;margin:0;padding:12px 14px 40px;
  background:var(--bg);color:var(--fg)}
a{color:var(--link)}
h1{font-size:1.25rem;margin:.2rem 0 .8rem}h2{font-size:1.05rem;margin:1.4rem 0 .5rem}
.card{background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:10px;
  padding:10px 12px;margin:10px 0}
.muted{color:var(--muted);font-size:.9rem}
img,video{width:100%;height:auto;border-radius:6px;margin:6px 0;background:#000}
.people{border-color:var(--alert);border-width:2px}ul{padding-left:1.2rem}
.grid a{display:block;text-decoration:none;color:inherit}
.check{border-color:var(--link);border-width:2px}.status{font-weight:600}
.decide{display:flex;flex-wrap:wrap;gap:8px;margin:8px 0}
.decide button,.decide input{font:inherit;padding:8px 12px;border:1px solid var(--line);border-radius:8px;
  background:var(--bg);color:var(--fg)}
.decide input{flex:1;min-width:10rem}
"""

# Shows what's been answered on a page's "check by eye" cards, from serve's /status.
CHECK_JS = """
fetch('../../api/status?page=' + PAGE, {cache: 'no-store'}).then(r => r.json()).then(d => {
  for (const c of document.querySelectorAll('[data-key]')) {
    const s = d[c.dataset.key];
    if (!s) continue;
    c.querySelector('.status').textContent = s.text;
    if (s.applied) c.querySelectorAll('form').forEach(f => f.remove());
  }
}).catch(() => {});
"""


def web_base():
    return (CONFIG.get("web") or {}).get("base_url", "").rstrip("/")


def media_url(r):
    return f"{web_base()}/media/" + urllib.parse.quote(r)


def playable(src, page_dir, n):
    """URL for a clip a phone browser can play: mp4s as they are; H.264 in other
    containers (motion's .mkv) remuxed, and anything else (the ESP32's MJPEG .avi,
    which ffmpeg will happily put in an mp4 that no browser plays) re-encoded, to an
    mp4 beside the page."""
    if src.suffix.lower() in (".mp4", ".m4v", ".mov"):
        return media_url(rel(src))
    out = page_dir / f"clip{n}.mp4"
    encode = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "26", "-pix_fmt", "yuv420p", "-an"]
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                            "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(src)],
                           capture_output=True, text=True)
    for codec in ([["-c", "copy"]] if probe.stdout.strip() == "h264" else []) + [encode]:
        r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), *codec,
                            "-movflags", "+faststart", str(out)], capture_output=True)
        if r.returncode == 0 and out.exists() and out.stat().st_size:
            return out.name
    return None


def page_html(title, body):
    return (f"<!doctype html><html lang=en><head><meta charset=utf-8>"
            f"<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<meta name=robots content=noindex><title>{html.escape(title)}</title>"
            f"<style>{PAGE_CSS}</style></head><body>{body}</body></html>")


def build_alert_page(batches, people, when, token=None):
    """Write the page for one notification; returns its URL, or None if pages are off.
    With token, rewrites that page in place."""
    if not web_base():
        return None
    token = token or secrets.token_urlsafe(16)
    page = WEB / "a" / token
    page.mkdir(parents=True, exist_ok=True)
    parts = [f"<p class=muted><a href='../../'>All recent events</a></p><h1>Cameras {html.escape(when)}</h1>"]
    if people:
        parts.append("<div class='card people' id=people><b>People seen</b><ul>"
                     + "".join(f"<li>{html.escape(l)}</li>" for l in people) + "</ul></div>")
    best = []
    for b in batches:
        for f in sorted((b / "attach").glob("*.jpg")):
            shutil.copy2(f, page / f"best{len(best)}.jpg")
            best.append((f"best{len(best)}.jpg", f.stem))
    if best:
        parts.append("<h2>Best moments</h2>" + "".join(
            f"<div class=card><img src='{n}' alt='{html.escape(s)}'><div class=muted>{html.escape(s)}</div></div>"
            for n, s in best))
    summary = "\n\n".join((b / "summary.md").read_text().strip() for b in batches if (b / "summary.md").exists())
    if summary:
        parts.append("<h2>Summary</h2><div class=card>" + md_to_html(summary).replace("\n", "<br>") + "</div>")
    kept, checks, check_cards, clip_n = [], {}, [], 0
    for b in batches:
        for line in (b / "verdicts.tsv").read_text().splitlines()[1:]:
            r = line.split("\t") + [""] * 7
            guess = r[6].strip()
            if not guess and r[1].strip().lower() != "move" and UNSURE_RE.search(r[2]):
                guess = "?"  # the review was unsure but didn't offer a guess
            if not r[3].strip() or (r[1].strip().lower() == "move" and not guess):
                continue
            media = []
            for f in event_files(r[3]):
                src = ROOT / f
                if not src.exists():
                    continue
                if src.suffix.lower() in IMAGE_EXT:
                    media.append(f"<img loading=lazy src='{media_url(f)}' alt=''>")
                else:
                    url = playable(src, page, clip_n)
                    clip_n += 1
                    if url:
                        media.append(f"<video controls playsinline preload=metadata src='{url}'></video>")
            first = ROOT / event_files(r[3])[0]
            head = f"{camera_of(first)} · {capture_time(first):%d %b %H:%M:%S}" if first.exists() else r[0]
            card = (f"<b>{html.escape(r[2].strip() or 'kept')}</b>"
                    f"<div class=muted>{html.escape(head)}</div>{''.join(media)}")
            if guess:
                key = f"{b.name}/{r[0]}"
                checks[key] = guess
                check_cards.append(f"<div class='card check' id='{html.escape(f'check-{b.name}-{r[0]}')}' "
                                   f"data-key='{html.escape(key)}'>{card}{decide_forms(token, b.name, r[0], guess)}"
                                   f"<div class=status></div></div>")
            else:
                kept.append(f"<div class=card id='{b.name}-{r[0]}'>{card}</div>")
    if check_cards:
        parts.insert(2 if people else 1, "<h2 id=check>Check by eye</h2><p class=muted>Is the guess right? "
                     "Confirmed pictures go to SmugMug; discarded ones to to-delete.</p>" + "".join(check_cards))
        (page / "checks.json").write_text(json.dumps(checks))
    parts.append(f"<h2>Kept events ({len(kept)})</h2>" + ("".join(kept) or "<p class=muted>None.</p>"))
    if check_cards:
        parts.append(f"<script>const PAGE = {json.dumps(token)};{CHECK_JS}</script>")
    title = f"Cameras {when}"
    (page / "index.html").write_text(page_html(title, "".join(parts)))
    lead = best[0][0] if best else None
    (page / "meta.json").write_text(json.dumps({"title": title, "when": datetime.now().isoformat(),
                                                "people": len(people), "kept": len(kept), "lead": lead,
                                                "checks": len(checks), "batches": [b.name for b in batches]}))
    build_gallery()
    return f"{web_base()}/a/{token}/"


def candidates(guess):
    """The species a check-by-eye guess offers: "badger or domestic cat" -> both; "?" -> none."""
    return [c for c in (clean_species(g.strip(" ?")) for g in re.split(r"\s+or\s+", guess)) if c]


# A note that says the review wasn't sure what it saw.
UNSURE_RE = re.compile(r"\b(unsure|uncertain|unidentified|not sure|possibl[ey]|probabl[ey]|maybe|"
                       r"not (fully )?ruled out)\b|\?", re.I)


def decide_forms(token, batch, event, guess):
    """A button per candidate species, No, and something else, posted to serve. The
    species box has its own form so that pressing Enter in it doesn't pick a candidate."""
    hidden = "".join(f"<input type=hidden name={k} value='{html.escape(v)}'>"
                     for k, v in (("page", token), ("batch", batch), ("event", event)))
    picks = candidates(guess)
    return (f"<form class=decide method=post action='../../api/decide'>{hidden}"
            + "".join(f"<button name=confirm value='{html.escape(c)}'>"
                      f"{'Yes, ' if len(picks) == 1 else ''}{html.escape(c)}</button>" for c in picks)
            + f"<button name=action value=discard>No, discard</button></form>"
            f"<form class=decide method=post action='../../api/decide'>{hidden}"
            f"<input type=hidden name=action value=other><input name=species required maxlength=60 "
            f"autocapitalize=none placeholder='Something else, e.g. stoat'><button>Save</button></form>")


def build_gallery():
    """web/index.html: the recent alert pages, newest first."""
    pages = []
    for m in (WEB / "a").glob("*/meta.json") if (WEB / "a").exists() else []:
        try:
            pages.append((json.loads(m.read_text()), m.parent.name))
        except ValueError:
            continue
    pages.sort(key=lambda x: x[0]["when"], reverse=True)
    items = []
    for meta, token in pages:
        img = f"<img loading=lazy src='a/{token}/{meta['lead']}' alt=''>" if meta.get("lead") else ""
        flag = " people" if meta.get("people") else ""
        items.append(f"<a class='card{flag}' href='a/{token}/'><b>{html.escape(meta['title'])}</b>"
                     f"<div class=muted>{meta['kept']} kept"
                     f"{' · people seen' if meta.get('people') else ''}"
                     f"{f' · {n} to check' if (n := meta.get('checks')) else ''}</div>{img}</a>")
    WEB.mkdir(exist_ok=True)
    (WEB / "index.html").write_text(page_html(
        "Camera events", "<h1>Camera events</h1><div class=grid>"
        + ("".join(items) or "<p class=muted>Nothing yet.</p>") + "</div>"))


def purge_pages():
    """Delete alert pages older than the web page_days (default: delete_after_days)."""
    if not (WEB / "a").exists():
        return 0
    days = (CONFIG.get("web") or {}).get("page_days", CONFIG.get("delete_after_days", 30))
    cutoff = time.time() - days * 86400
    gone = 0
    for d in (WEB / "a").iterdir():
        if d.is_dir() and (d / "meta.json").exists() and (d / "meta.json").stat().st_mtime < cutoff:
            shutil.rmtree(d)
            gone += 1
    if gone:
        build_gallery()
    return gone


def cmd_notify(args):
    done = [b for b in sorted(BATCHES.glob("*")) if (b / "DONE").exists() and not (b / "NOTIFIED").exists()] \
        if BATCHES.exists() else []
    p = CONFIG["pushover"]
    pend = pending_batches()
    totals = {"events": 0, "kept_events": 0, "moved_files": 0, "kept_files": 0}
    summaries, people, photos, problems, checks = [], [], [], 0, 0
    for b in done:
        st = json.loads((b / "stats.json").read_text())
        for k in totals:
            totals[k] += st.get(k, 0)
        if (b / "summary.md").exists():
            summaries.append((b / "summary.md").read_text().strip())
        if (b / "people.txt").exists():
            people += [l.strip() for l in (b / "people.txt").read_text().splitlines() if l.strip()]
        photos += sorted((b / "attach").glob("*.jpg"))
        if (b / "problems.txt").exists():
            problems += len([l for l in (b / "problems.txt").read_text().splitlines() if l])
        checks += sum(1 for l in (b / "verdicts.tsv").read_text().splitlines()[1:]
                      if (l.split("\t") + [""] * 7)[6].strip())

    notes = []
    if args.purged:
        notes.append(f"Deleted {args.purged} old files from to-delete.")
    if pend:
        notes.append(f"{len(pend)} batch(es) still to review; next run continues.")
    if problems:
        notes.append(f"{problems} file(s) unreadable.")
    if not done:
        if notes and p.get("notify_when_nothing_new", False):
            pushover("Cameras: nothing new", "\n".join(notes), priority=-1)
        log("Nothing to report")
        return

    when = datetime.now().strftime("%d %b %H:%M")
    people_photos = [f for f in photos if f.name.lower().startswith("people")]
    other_photos = [f for f in photos if f not in people_photos]

    link = build_alert_page(done, people, when)

    # People first, as their own louder alert.
    if people:
        pushover(f"People seen on cameras ({when})", md_to_html("\n".join(f"• {l}" for l in people)),
                 priority=p.get("people_priority", 1), image=people_photos[0] if people_photos else None,
                 url=link and link + "#people")

    counts = (f"{totals['events']} events: {totals['kept_events']} kept, "
              f"{totals['moved_files']} files to to-delete.")
    body = md_to_html("\n\n".join(summaries) or "No summary written.") + "\n\n" + \
        html.escape(" ".join([counts] + notes))
    lead = other_photos[0] if other_photos else (people_photos[0] if people_photos and not people else None)
    if link and checks:
        pushover(f"Cameras {when}", body, priority=p.get("summary_priority", 0), image=lead,
                 url=link + "#check", url_title=f"Confirm or discard {checks} sighting{'s' * (checks > 1)}")
    else:
        pushover(f"Cameras {when}", body, priority=p.get("summary_priority", 0), image=lead, url=link)

    # A few more of the best photos, quietly.
    extra = [f for f in other_photos if f != lead][:p.get("extra_photos", 3)]
    for f in extra:
        pushover(f.stem[:250], html.escape(f.stem), priority=-1, image=f, url=link)

    for b in done:
        (b / "NOTIFIED").write_text(datetime.now().isoformat() + "\n")
    log(f"Notified: {len(done)} batch(es), {len(people)} people alert line(s), {1 + len(extra)} summary message(s)")


# ---------- answers to "check by eye" ----------
# An alert page shows each event the review was unsure of (the `check` column: its best
# guess) with Yes / No / something-else buttons. serve, behind the web server's password,
# only records the answer in decisions/<batch>__<event>.json; the next run's `checks`
# applies it: a species goes into verdicts.tsv for the next SmugMug upload, and No moves
# the event to to-delete. Once applied (<name>.done.json), an answer can't be changed.

PAGE_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{8,64}")


def decision_paths(batch, event):
    return DECISIONS / f"{batch}__{event}.json", DECISIONS / f"{batch}__{event}.done.json"


def write_atomic(path, text):
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def clean_species(s):
    """Lower-case species names, comma-separated; '' if it doesn't look like one."""
    s = ", ".join(re.sub(r"\s+", " ", p).strip() for p in s.lower().split(",") if p.strip())
    return s if len(s) <= 60 and re.fullmatch(r"[a-z][a-z' -]*(, [a-z][a-z' -]*)*", s) else ""


def smug_excluded(species):
    """True if any word of the name is in smugmug.exclude (e.g. rat)."""
    exclude = {w.lower() for w in smug_conf().get("exclude", ["rat"])}
    return bool(exclude & set(re.split(r"[\s-]+", species)))


def apply_decision(d):
    """Apply one recorded answer to its event in verdicts.tsv; returns what happened."""
    v = BATCHES / d["batch"] / "verdicts.tsv"
    lines = v.read_text().splitlines()
    width = len(lines[0].split("\t"))
    for i, line in enumerate(lines[1:], 1):
        r = line.split("\t")
        r += [""] * (width - len(r))
        if r[0] != d["event"]:
            continue
        note = r[2].strip()
        if d["action"] == "discard":
            moved = 0
            for f in event_files(r[3]):
                if (ROOT / f).exists():
                    move_to_delete(ROOT / f)
                    moved += 1
            r[1], r[2], r[4] = "move", f"{note} - discarded by eye".lstrip(" -"), ""
            result = f"Discarded: {moved} file{'s' * (moved != 1)} moved to to-delete."
        else:
            sp = d["species"]
            if not r[5].strip():
                r[5] = "photo" if any(f.lower().endswith(tuple(IMAGE_EXT)) for f in event_files(r[3])) else "video"
            # Added to any species the review was already sure of (e.g. a blackbird beside it).
            r[4] = ", ".join(dict.fromkeys(s.strip() for s in f"{r[4]},{sp}".split(",") if s.strip()))
            r[2] = f"{note} - {'confirmed' if d['action'] == 'confirm' else 'identified'} by eye: {sp}".lstrip(" -")
            (BATCHES / d["batch"] / "SHARED").unlink(missing_ok=True)  # so share looks at it again
            names = [s.strip() for s in sp.split(",")]
            result = f"{'Confirmed' if d['action'] == 'confirm' else 'Recorded'} as {sp}"
            if not smug_cred_path().exists():
                result += "."
            elif all(smug_excluded(s) for s in names):
                result += ": not uploaded (in smugmug.exclude)."
            else:
                result += ", for the SmugMug album."
        lines[i] = "\t".join(r)
        write_atomic(v, "\n".join(lines) + "\n")
        return result
    return "That event wasn't found; nothing was done."


def cmd_checks(_):
    for f in sorted(DECISIONS.glob("*.json")) if DECISIONS.exists() else []:
        if f.name.endswith(".done.json"):
            continue
        d = json.loads(f.read_text())
        done = decision_paths(d["batch"], d["event"])[1]
        if done.exists():
            continue
        result = apply_decision(d)
        write_atomic(done, json.dumps({**d, "applied": datetime.now().isoformat(timespec="seconds"),
                                       "result": result}))
        log(f"{d['batch']} {d['event']}: {result}")


def page_checks(page):
    """{batch/event: guess} for an alert page's check-by-eye cards, or None."""
    f = WEB / "a" / page / "checks.json"
    if not PAGE_TOKEN_RE.fullmatch(page) or not f.exists():
        return None
    return json.loads(f.read_text())


def check_state(key):
    pending, done = decision_paths(*key.split("/"))
    if done.exists():
        return {"applied": True, "text": json.loads(done.read_text())["result"]}
    if pending.exists():
        d = json.loads(pending.read_text())
        what = "discard" if d["action"] == "discard" else d["species"]
        return {"applied": False, "text": f"Saved ({what}). It's applied at the next run, usually "
                                          f"within a minute; until then you can still change it."}
    return None


class CheckHandler(http.server.BaseHTTPRequestHandler):
    """/status?page=TOKEN (GET) and /decide (POST, from the page's forms). Any path prefix
    the web server adds (e.g. /cams/api/) is ignored."""

    def reply(self, code, body, ctype="text/html; charset=utf-8", location=None):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        if location:
            self.send_header("Location", location)
        self.end_headers()
        self.wfile.write(data)

    def fail(self, code, msg):
        self.reply(code, page_html("Not saved", f"<h1>Not saved</h1><p>{html.escape(msg)}</p>"
                                               "<p>Go back to the page and try again.</p>"))

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        checks = page_checks((urllib.parse.parse_qs(url.query).get("page") or [""])[0])
        if not url.path.endswith("/status") or checks is None:
            return self.fail(404, "Not found.")
        states = {k: s for k in checks if (s := check_state(k))}
        self.reply(200, json.dumps(states), "application/json")

    def do_POST(self):
        if not urllib.parse.urlsplit(self.path).path.endswith("/decide"):
            return self.fail(404, "Not found.")
        # Browsers send the saved password with forms from any site, so only accept our own.
        origin = self.headers.get("Origin") or self.headers.get("Referer") or ""
        if urllib.parse.urlsplit(origin).netloc != self.headers.get("Host"):
            return self.fail(403, "That form didn't come from this site.")
        n = int(self.headers.get("Content-Length") or 0)
        if n > 4096:
            return self.fail(413, "Too much data.")
        f = {k: v[0] for k, v in urllib.parse.parse_qs(self.rfile.read(n).decode(errors="replace")).items()}
        page, batch, event, action = (f.get(k, "") for k in ("page", "batch", "event", "action"))
        checks = page_checks(page)
        key = f"{batch}/{event}"
        if not checks or key not in checks:
            return self.fail(404, "That sighting isn't on this page.")
        species = {"discard": ""}.get(action)
        picks = candidates(checks[key])
        if not action and f.get("confirm") in picks:
            action, species = "confirm", f["confirm"]
        elif action == "confirm" and len(picks) == 1:  # pages made before candidate buttons
            species = picks[0]
        if action == "other":
            species = clean_species(f.get("species", ""))
            if not species:
                return self.fail(400, "Type a species name: letters, spaces and hyphens, "
                                      "with commas between several (e.g. great tit, blue tit).")
        if species is None:
            return self.fail(400, "Unknown answer.")
        pending, done = decision_paths(batch, event)
        if done.exists():
            return self.fail(409, "That sighting has already been dealt with: "
                                  + json.loads(done.read_text())["result"])
        write_atomic(pending, json.dumps({"page": page, "batch": batch, "event": event, "action": action,
                                          "species": species, "at": datetime.now().isoformat(timespec="seconds")}))
        self.reply(303, "", location=f"../a/{page}/#check-{batch}-{event}")


def cmd_serve(args):
    DECISIONS.mkdir(exist_ok=True)
    log(f"Recording answers from the alert pages on port {args.port}")
    http.server.ThreadingHTTPServer((args.host, args.port), CheckHandler).serve_forever()


# ---------- SmugMug ----------
# Good pictures of identified wildlife go to <folder>/<Species> albums: the camera photo,
# the best video frame and the clip, as the review's `share` column says. Credentials
# (API key and secret, then the access token from smugmug-auth) are in credentials_file.

SMUG_API = "https://api.smugmug.com"
SMUG_UPLOAD = "https://upload.smugmug.com/"
SMUG_STATE = HERE / "smugmug-state.json"   # folder and album ids, and what's uploaded


def smug_conf():
    return CONFIG.get("smugmug") or {}


def smug_cred_path():
    return Path(os.path.expanduser(smug_conf().get("credentials_file", "~/.config/trailcam/smugmug.json")))


def smug_state():
    return json.loads(SMUG_STATE.read_text()) if SMUG_STATE.exists() else {}


def oauth_q(s):
    return urllib.parse.quote(str(s), safe="~")


def oauth_header(method, url, cred, token=None, token_secret="", extra=None):
    """OAuth 1.0a (HMAC-SHA1) Authorization header. A query string in url is signed too;
    extra adds or overrides oauth_* fields."""
    parts = urllib.parse.urlsplit(url)
    o = {"oauth_consumer_key": cred["api_key"], "oauth_nonce": uuid.uuid4().hex,
         "oauth_signature_method": "HMAC-SHA1", "oauth_timestamp": str(int(time.time())),
         "oauth_version": "1.0"}
    if token:
        o["oauth_token"] = token
    o.update(extra or {})
    params = urllib.parse.parse_qsl(parts.query, keep_blank_values=True) + list(o.items())
    norm = "&".join(f"{k}={v}" for k, v in sorted((oauth_q(k), oauth_q(v)) for k, v in params))
    base = "&".join([method.upper(), oauth_q(f"{parts.scheme}://{parts.netloc}{parts.path}"), oauth_q(norm)])
    key = f"{oauth_q(cred['api_secret'])}&{oauth_q(token_secret)}"
    o["oauth_signature"] = base64.b64encode(hmac.new(key.encode(), base.encode(), hashlib.sha1).digest()).decode()
    return "OAuth " + ", ".join(f'{oauth_q(k)}="{oauth_q(v)}"' for k, v in o.items())


class SmugError(Exception):
    def __init__(self, status, msg):
        super().__init__(f"SmugMug HTTP {status}: {msg}")
        self.status = status


def smug_request(method, url, cred, body=None, headers=None):
    url = url if url.startswith("http") else SMUG_API + url
    h = {"Accept": "application/json", **(headers or {})}
    if isinstance(body, dict):
        body = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    h["Authorization"] = oauth_header(method, url, cred, cred["access_token"], cred["access_secret"])
    req = urllib.request.Request(url, data=body, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise SmugError(e.code, e.read().decode(errors="replace")[:300]) from None


def uri_of(x):
    return x["Uri"] if isinstance(x, dict) else x


def smug_child(cred, parent, name, kind, privacy):
    """The child folder or album called name under node parent, created if needed."""
    r = smug_request("GET", f"/api/v2/node/{parent}!children?count=1000", cred)
    for n in r["Response"].get("Node", []):
        if n.get("Type") == kind and n.get("Name", "").lower() == name.lower():
            return n
    url_name = re.sub(r"[^A-Za-z0-9]+", "-", name.title()).strip("-") or kind
    r = smug_request("POST", f"/api/v2/node/{parent}!children", cred,
                     {"Type": kind, "Name": name, "UrlName": url_name,
                      "Privacy": privacy})
    return r["Response"]["Node"]


def smug_album(cred, state, species):
    albums = state.setdefault("albums", {})
    if species not in albums:
        if "folder" not in state:
            user = smug_request("GET", "/api/v2!authuser", cred)["Response"]["User"]
            root = uri_of(user["Uris"]["Node"]).rsplit("/", 1)[-1]
            state["folder"] = smug_child(cred, root, smug_conf().get("folder", "Wildlife"), "Folder",
                                         smug_conf().get("privacy", "Unlisted"))["NodeID"]
        # Public albums show when browsing the folder, but the folder's own privacy still
        # applies to them: inside an unlisted folder they're only found through its link.
        node = smug_child(cred, state["folder"], species.capitalize(), "Album",
                          smug_conf().get("album_privacy", "Public"))
        albums[species] = uri_of(node["Uris"]["Album"])
    return albums[species]


def header_text(s):
    """HTTP headers are Latin-1: keep notes and names to plain ASCII."""
    return s.replace("\u2013", "-").replace("\u2014", "-").encode("ascii", "replace").decode()


def album_md5s(cred, album):
    """Checksums of the pictures already in an album, however they got there. For a
    video, SmugMug's checksum is of a still it makes, so cmd_share also keeps its own."""
    sums, start = set(), 1
    while True:
        r = smug_request("GET", f"{album}!images?start={start}&count=200", cred)["Response"]
        sums |= {i["ArchivedMD5"] for i in r.get("AlbumImage", []) if i.get("ArchivedMD5")}
        if not r.get("Pages", {}).get("NextPage"):
            return sums
        start += 200


def smug_upload(cred, album, path, title, caption, keywords, data=None):
    data = path.read_bytes() if data is None else data
    r = smug_request("POST", SMUG_UPLOAD, cred, data, {
        "Content-MD5": hashlib.md5(data).hexdigest(), "Content-Type": "application/octet-stream",
        "X-Smug-AlbumUri": album, "X-Smug-ResponseType": "JSON", "X-Smug-Version": "v2",
        "X-Smug-FileName": path.name, "X-Smug-Title": header_text(title),
        "X-Smug-Caption": header_text(caption), "X-Smug-Keywords": header_text(keywords)})
    if r.get("stat") != "ok":
        raise SmugError(200, r.get("message", "upload not accepted"))


def find_media(f):
    """A file named in verdicts.tsv, whether it stayed put or was moved to to-delete."""
    for p in (ROOT / f, TO_DELETE / f):
        if p.exists():
            return p
    return None


def share_items(files, share, tmp):
    """(key, path) for each item the review picked: photo (the first photo), photo@N (the
    Nth photo, for cameras that take several per trigger), frame@SECONDS, video."""
    photos = [f for f in files if f.suffix.lower() in IMAGE_EXT]
    video = next((f for f in files if f.suffix.lower() in VIDEO_EXT), None)
    items = []
    for i, word in enumerate(share):
        if word == "photo" or word.startswith("photo@"):
            n = word[6:] if "@" in word else "1"
            if n.isdigit() and 1 <= int(n) <= len(photos):
                items.append((word, photos[int(n) - 1]))
        elif word.startswith("frame@") and video:
            try:
                t = float(word[6:])
            except ValueError:
                continue
            out = tmp / f"{video.stem}_{t:.1f}s.jpg"
            if ffmpeg_frame(video, t, out):
                items.append((word, out))
        elif word == "video" and video:
            if video.suffix.lower() in (".mp4", ".m4v", ".mov"):
                items.append((word, video))
            else:
                name = playable(video, tmp, i)
                if name:
                    items.append((word, (tmp / name).rename(tmp / f"{video.stem}.mp4")))
    return items


def cmd_share(_):
    """Upload what each finished batch marked for sharing. Batches reviewed before the
    species column existed are just marked done; backfill covers them."""
    path = smug_cred_path()
    cred = json.loads(path.read_text()) if path.exists() else {}
    if not cred.get("access_token"):
        return  # not set up
    state = smug_state()
    uploaded = set(state.get("uploaded", []))
    in_album = {}  # album -> checksums: the same picture reviewed twice (e.g. moved) goes up once
    sent_md5 = state.setdefault("md5", {})  # album -> checksums of what this has uploaded
    count = skipped = 0

    def save():
        state["uploaded"] = sorted(uploaded)
        SMUG_STATE.write_text(json.dumps(state, indent=1))

    for b in sorted(BATCHES.glob("*")) if BATCHES.exists() else []:
        if not (b / "DONE").exists() or (b / "SHARED").exists():
            continue
        lines = (b / "verdicts.tsv").read_text().splitlines()
        if lines and "species" in lines[0].split("\t"):
            for line in lines[1:]:
                r = line.split("\t") + [""] * 6
                species = [s.strip().lower() for s in r[4].split(",") if s.strip()]
                species = [s for s in species if not smug_excluded(s)]
                if not species or not r[5].split():
                    continue
                files = [p for p in map(find_media, event_files(r[3])) if p]
                if not files:
                    continue
                when = f"{camera_of(ROOT / event_files(r[3])[0])} {capture_time(files[0]):%d %b %Y %H:%M}"
                with tempfile.TemporaryDirectory() as td:
                    for key, item in share_items(files, r[5].split(), Path(td)):
                        for sp in species:
                            k = f"{b.name}/{r[0]}/{key}/{sp}"
                            if k in uploaded:
                                continue
                            album = smug_album(cred, state, sp)
                            if album not in in_album:
                                in_album[album] = album_md5s(cred, album) | set(sent_md5.get(album, []))
                            data = item.read_bytes()
                            md5 = hashlib.md5(data).hexdigest()
                            if md5 in in_album[album]:
                                uploaded.add(k)
                                skipped += 1
                                save()
                                continue
                            try:
                                smug_upload(cred, album, item, f"{sp.capitalize()} - {when}", r[2].strip(), sp, data)
                                in_album[album].add(md5)
                                sent_md5.setdefault(album, []).append(md5)
                            except SmugError as e:
                                if not 400 <= e.status < 500 or e.status in (401, 429):
                                    save()
                                    raise
                                log(f"SmugMug refused {item.name}: {e}")  # won't work next time either
                            uploaded.add(k)
                            count += 1
                            save()
        (b / "SHARED").write_text(datetime.now().isoformat() + "\n")
    if count or skipped:
        log(f"SmugMug: uploaded {count} item(s)" + (f", {skipped} already there" if skipped else ""))


def cmd_backfill(args):
    """Queue events kept by earlier reviews, from the given sources (and, with --match,
    only those whose note matches), as batches whose review only adds species and
    share. With --reshare, instead queue events that got a species but nothing to
    share, wherever their files are now, for another look. Each event is queued once."""
    sources = args.sources or (CONFIG["sources"] if args.reshare else
                               smug_conf().get("backfill_sources", ["trailcam", "motion"]))
    match = re.compile(args.match, re.I) if args.match else None
    done_file = HERE / ("reshared.txt" if args.reshare else "backfilled.txt")
    seen = set(done_file.read_text().split()) if done_file.exists() else set()
    events = []
    for v in sorted(BATCHES.glob("*/verdicts.tsv")) if BATCHES.exists() else []:
        lines = v.read_text().splitlines()
        if not lines or ("species" in lines[0].split("\t")) != args.reshare:
            continue  # backfill: batches without species; reshare: those with
        for line in lines[1:]:
            r = line.split("\t") + [""] * 6
            if args.reshare:
                if not r[4].strip() or r[5].strip():
                    continue
                files = [find_media(f) for f in event_files(r[3])]  # routine visitors are in to-delete
            else:
                if r[1].strip().lower() != "keep":
                    continue
                files = [ROOT / f for f in event_files(r[3])]
            if not files or not all(p and p.exists() for p in files):
                continue
            first = event_files(r[3])[0]  # where the review saw it
            if first in seen or not any(first.startswith(s.rstrip("/") + "/") for s in sources):
                continue
            if match and not match.search(r[2]):
                continue
            seen.add(first)
            events.append({"camera": camera_of(ROOT / first), "time": capture_time(files[0]),
                           "files": files, "note": r[2].strip(), "key": first})
    events.sort(key=lambda e: e["time"])
    per, chunk, made = CONFIG.get("max_files_per_batch", 200), [], 0

    def flush():
        nonlocal made
        b = build_batch(chunk, [("keep", e["note"]) for e in chunk])
        (b / "BACKFILL").write_text("")
        with open(done_file, "a") as f:
            f.write("".join(e["key"] + "\n" for e in chunk))
        print(b)
        made += 1

    for e in events:
        if chunk and sum(len(c["files"]) for c in chunk) + len(e["files"]) > per:
            flush()
            chunk = []
        chunk.append(e)
    if chunk:
        flush()
    log(f"backfill: {len(events)} events in {made} batch(es)")


def cmd_smugmug_auth(_):
    """Gets an access token for the API key in the credentials file, with a PIN."""
    path = smug_cred_path()
    cred = json.loads(path.read_text()) if path.exists() else {}
    if not cred.get("api_key") or not cred.get("api_secret"):
        sys.exit(f'First put your SmugMug API key and secret in {path}:\n'
                 '  {"api_key": "...", "api_secret": "..."}')
    base = SMUG_API + "/services/oauth/1.0a/"

    def call(endpoint, token=None, secret="", extra=None):
        url = base + endpoint
        req = urllib.request.Request(url, headers={"Authorization": oauth_header("GET", url, cred, token, secret, extra)})
        with urllib.request.urlopen(req, timeout=60) as r:
            return dict(urllib.parse.parse_qsl(r.read().decode()))

    t = call("getRequestToken", extra={"oauth_callback": "oob"})
    print("Open this link, sign in to SmugMug and allow access, then type the 6-digit code it shows:")
    print(base + "authorize?" + urllib.parse.urlencode(
        {"oauth_token": t["oauth_token"], "Access": "Full", "Permissions": "Modify"}))
    code = input("Code: ").strip()
    a = call("getAccessToken", t["oauth_token"], t["oauth_token_secret"], {"oauth_verifier": code})
    cred.update(access_token=a["oauth_token"], access_secret=a["oauth_token_secret"])
    path.write_text(json.dumps(cred, indent=1))
    path.chmod(0o600)
    print(f"Saved to {path}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("next")
    pr = sub.add_parser("prompt")
    pr.add_argument("batch")
    c = sub.add_parser("crop")
    c.add_argument("file")
    c.add_argument("--t", type=float, help="seconds into the video")
    c.add_argument("--box", help="x,y,w,h as fractions of the frame, e.g. 0.4,0.3,0.3,0.4")
    b = sub.add_parser("burst")
    b.add_argument("file")
    b.add_argument("--start", type=float, required=True)
    b.add_argument("--end", type=float, required=True)
    b.add_argument("--fps", type=float, default=6)
    b.add_argument("--box")
    d = sub.add_parser("done")
    d.add_argument("batch")
    d.add_argument("--force", action="store_true", help="apply even if unfinished (unanswered = keep)")
    sub.add_parser("purge")
    cs = sub.add_parser("clear-skipped")
    cs.add_argument("--dry-run", action="store_true")
    n = sub.add_parser("notify")
    n.add_argument("--purged", type=int, default=0)
    sub.add_parser("share")
    sub.add_parser("checks")
    sv = sub.add_parser("serve")
    sv.add_argument("--host", default="0.0.0.0")
    sv.add_argument("--port", type=int, default=8080)
    bf = sub.add_parser("backfill")
    bf.add_argument("--sources", nargs="+", help="capture subfolders, e.g. trailcam motion/esp32cam")
    bf.add_argument("--match", help="only events whose note matches this regular expression")
    bf.add_argument("--reshare", action="store_true",
                    help="events that got a species but nothing to share, for another look")
    sub.add_parser("smugmug-auth")
    args = ap.parse_args()
    {"status": cmd_status, "next": cmd_next, "prompt": cmd_prompt, "crop": cmd_crop,
     "burst": cmd_burst, "done": cmd_done, "purge": cmd_purge, "clear-skipped": cmd_clear_skipped,
     "notify": cmd_notify, "share": cmd_share, "checks": cmd_checks, "serve": cmd_serve,
     "backfill": cmd_backfill,
     "smugmug-auth": cmd_smugmug_auth}[args.cmd](args)


if __name__ == "__main__":
    main()
