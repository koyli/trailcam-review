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
  notify [--purged N]    send Pushover messages for batches not yet reported
"""
import argparse
import html
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

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

IMAGE_EXT = {".jpg", ".jpeg", ".png"}
VIDEO_EXT = {".mp4", ".mkv", ".avi", ".mov", ".m4v"}

# Motion scoring works on tiny grayscale frames.
SW, SH = 128, 72      # large enough that a small bird far from the camera covers a few pixels
SH_USED = 66          # ignore the bottom rows, where camera text overlays sit
DIFF = 1.2            # per-pixel threshold, in standard deviations
MIN_BLOB = 5          # blob size (pixels) that counts as "something moved"
MAX_SAMPLES = 120     # long clips are sampled more sparsely than 2 fps
EVENT_GAP = 5         # seconds; files from one camera this close together are one event
TRAILCAM_RE = re.compile(r"^([A-Z0-9]+)_(\d{8})(\d{6})_(\d+)$", re.I)


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


def is_reviewed(r, paths, legacy):
    return r in paths or (r.startswith(CONFIG["sources"][0] + "/") and r.rsplit("/", 1)[-1] in legacy)


def candidate_files():
    """Finished media files under the source folders (missing folders are skipped)."""
    min_age = CONFIG.get("min_file_age_minutes", 10) * 60
    now = time.time()
    out = []
    for src in SOURCES:
        if not src.is_dir():
            continue
        for p in src.rglob("*"):
            if not p.is_file() or any(part.startswith(".") for part in p.relative_to(ROOT).parts):
                continue
            if p.suffix.lower() not in IMAGE_EXT | VIDEO_EXT:
                continue
            if now - p.stat().st_mtime < min_age:
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
    m = TRAILCAM_RE.match(p.stem)
    if m:
        try:
            return datetime.strptime(m.group(2) + m.group(3), "%Y%m%d%H%M%S")
        except ValueError:
            pass
    return datetime.fromtimestamp(p.stat().st_mtime)


def camera_of(p):
    m = TRAILCAM_RE.match(p.stem)
    source = p.relative_to(ROOT).parts[0]
    if m:
        return m.group(1).upper()
    return f"{source}:{p.parent.name}" if p.parent != ROOT / source else source


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
    batch = BATCHES / datetime.now().strftime("%Y%m%d-%H%M%S")
    frames_dir = batch / "frames"
    frames_dir.mkdir(parents=True)
    (batch / "attach").mkdir()
    per_clip = CONFIG.get("frames_per_clip", 4)
    spread = CONFIG.get("spread_frames_per_clip", 3)

    tiles, index, problems = [], [], []
    events = group_events(todo)
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
        f.write("event\tverdict\tnote\tfiles\n")
        for e_no, ev in enumerate(events):
            f.write(f"E{e_no}\t?\t\t{' '.join(rel(p) for p in ev['files'])}\n")
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
    print(text)


def parse_box(s):
    x, y, w, h = (float(v) for v in s.split(","))
    return x, y, w, h


def resolve(name):
    for p in (Path(name), ROOT / name):
        if p.exists():
            return p
    for src in SOURCES:
        if src.is_dir():
            hits = list(src.rglob(Path(name).name))
            if hits:
                return hits[0]
    sys.exit(f"Not found: {name}")


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
        out = ZOOMS / f"{src.stem}_{args.t or 0:.1f}_{(args.box or 'full').replace(',', '-')}.jpg"
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
        out = ZOOMS / f"{src.stem}_burst_{args.start}-{args.end}.jpg"
        make_sheet(tiles, out, cols=6, tw=400)
    log(str(out))


def move_to_delete(p):
    dest = TO_DELETE / rel(p)
    dest.parent.mkdir(parents=True, exist_ok=True)
    n = 1
    while dest.exists():
        dest = dest.with_name(f"{dest.stem}.{n}{dest.suffix}")
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
        files = r[3].split() if len(r) > 3 else []
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
    log(f"{batch.name}: kept {len(kept_files)} files ({kept_events} events), "
        f"moved {len(moved_files)} files ({moved_events} events) to {TO_DELETE}")


def cmd_purge(_):
    """Delete to-delete files moved more than delete_after_days ago (by manifest; else by file age)."""
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


def pushover(title, message, priority=0, image=None):
    """Send one Pushover message (HTML allowed). Raises on failure so run.sh can report it."""
    p = CONFIG["pushover"]
    fields = {"token": secret(p["app_token_file"]), "user": secret(p["user_key_file"]),
              "title": title[:250], "message": message[:1024], "html": "1", "priority": str(priority)}
    if priority >= 2:
        fields.update(retry="300", expire="3600")
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


def cmd_notify(args):
    done = [b for b in sorted(BATCHES.glob("*")) if (b / "DONE").exists() and not (b / "NOTIFIED").exists()] \
        if BATCHES.exists() else []
    p = CONFIG["pushover"]
    pend = pending_batches()
    totals = {"events": 0, "kept_events": 0, "moved_files": 0, "kept_files": 0}
    summaries, people, photos, problems = [], [], [], 0
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

    # People first, as their own louder alert.
    if people:
        pushover(f"People seen on cameras ({when})", md_to_html("\n".join(f"• {l}" for l in people)),
                 priority=p.get("people_priority", 1), image=people_photos[0] if people_photos else None)

    counts = (f"{totals['events']} events: {totals['kept_events']} kept, "
              f"{totals['moved_files']} files to to-delete.")
    body = md_to_html("\n\n".join(summaries) or "No summary written.") + "\n\n" + \
        html.escape(" ".join([counts] + notes))
    lead = other_photos[0] if other_photos else (people_photos[0] if people_photos and not people else None)
    pushover(f"Cameras {when}", body, priority=p.get("summary_priority", 0), image=lead)

    # A few more of the best photos, quietly.
    extra = [f for f in other_photos if f != lead][:p.get("extra_photos", 3)]
    for f in extra:
        pushover(f.stem[:250], html.escape(f.stem), priority=-1, image=f)

    for b in done:
        (b / "NOTIFIED").write_text(datetime.now().isoformat() + "\n")
    log(f"Notified: {len(done)} batch(es), {len(people)} people alert line(s), {1 + len(extra)} summary message(s)")


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
    n = sub.add_parser("notify")
    n.add_argument("--purged", type=int, default=0)
    args = ap.parse_args()
    {"status": cmd_status, "next": cmd_next, "prompt": cmd_prompt, "crop": cmd_crop,
     "burst": cmd_burst, "done": cmd_done, "purge": cmd_purge, "notify": cmd_notify}[args.cmd](args)


if __name__ == "__main__":
    main()
