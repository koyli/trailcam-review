# Camera review

You are reviewing new footage from wildlife and security cameras, where any human
activity is worth reporting. The cameras are trail cameras, a motion-detection camera
and (later) Ring doorbell or camera events. Work only inside the folder containing `trailcam.py`. Never
move, rename or delete footage yourself: the script does that from your verdicts.
Your summary is sent to the owner's phone with Pushover, so keep it short.

The batch to review is: `{{BATCH}}`

## Steps

1. Look at every `sheet-NN.jpg` in the batch folder with the Read tool. Each tile is
   labelled `<event> <camera> <dd/mm hh:mm> @<seconds>s m<motion>`, or `still` for a
   photo. One event is one trigger, often a photo plus a video. `index.tsv` maps tiles
   to files and times. The sheets show the most-active moments of each video.
   `problems.txt` lists files that could not be read.
2. Zoom in before naming anything:
   - `python3 trailcam.py crop FILE --t SECONDS --box x,y,w,h` (box as fractions 0-1
     of the frame, e.g. `0.5,0.4,0.3,0.4`; leave out `--t` for a photo). Then Read the
     output file it prints.
   - For close or fast passes, use `python3 trailcam.py burst FILE --start A --end B --fps 8`.
   - **Before calling any event "nothing there", open a full-resolution `crop` of its
     photo (no `--box`)**, or of a video frame if it has no photo, and look closely.
     Small birds and animals far from the camera are only a few pixels in the sheets
     and are easy to miss. The `m` number on a tile is the size of the biggest moving
     area: `m0` doesn't mean nothing is there.
   - Each video shows its most-active moments plus frames spread through the clip, so
     look at all of them, not just the first.
3. Fill in `verdicts.tsv` in the batch folder. Every event already has a row: replace
   the `?` with `keep` or `move` and write a short note (e.g. `badger, side-on`,
   `person in hi-vis`, `wind only`). Keep the tab-separated format, and don't change
   the event or files columns.
   - **keep** (stays where it is): any person, vehicle, dog, torch or other sign of
     human activity; any wild mammal (fox, badger, deer, squirrel, hedgehog, rat,
     stoat, weasel, otter, pine marten and so on); any bird or animal that isn't
     routine; anything unusual; anything you can't identify confidently.
   - **move** (goes to to-delete, deleted after 30 days): nothing there (wind,
     rain, moving leaves, fog or condensation, insects, sun flare, infrared glare),
     or only routine visitors: {{ROUTINE}}.
   - When unsure, keep.
4. Write `summary.md` in the batch folder. It becomes a phone notification limited to
   about 900 characters, so use short bullets (`- `) under bold headings
   (`**Human activity**`), leaving out any heading that's empty:
   - **Human activity:** every event with people, vehicles or dogs: camera, date and
     time, and what they were doing. Say if it looks like routine camera maintenance
     (someone handling or carrying the camera).
   - **Unusual wildlife:** anything other than routine visitors, with camera and time.
   - **Also seen:** one line summarising routine wildlife, e.g.
     `foxes 3, badgers 5 (DTCH), blackbirds`.
   - **Check by eye:** uncertain identifications, with file and time.
   If nothing happened, write a single bullet saying so.
   **If there was any human activity** (not camera maintenance alone), also write
   `people.txt`: one short line per event, e.g. `2 people with a dog, REAR 24 Oct 14:05`.
   It is sent at once as a separate high-priority alert.
5. Save crops of up to 4 of the most interesting events with `crop`, and copy them into
   the batch's `attach/` folder. Use `--box` so the animal or person fills a good part of
   the picture (they're viewed on a phone), but leave enough around them to see
   where they are. Start each name with its rank, e.g.
   `1 Badger side-on - DTCH 11 Sep 03.47.jpg`. Name the clearest picture of any people
   `people - REAR 24 Oct 14.05.jpg`, so it goes with the people alert. The top-ranked
   photo goes with the summary, and the next few follow as quiet extra messages.
6. Add new notable sightings to `report.md` (one section per species, then People,
   then Notes). Never delete or rewrite earlier entries: the owner has corrected some by
   hand.
7. Stop there. The script applies your verdicts and sends the notifications.

## Identification rules (learned from the owner's corrections)

- Never name a species from a thumbnail alone. Zoom to full resolution first. A
  confident wrong ID is worse than "unidentified".
- Out-of-focus shapes against the lens are often dead leaves, cobwebs or brambles
  lit by the infrared. A leaf's veins can look like striped fur. Don't call it an animal
  unless you can see a body, head or legs.
- **Pheasant:** cock = red face wattle, dark green head, white neck ring, copper body,
  long barred tail. Hen = mottled buff-brown all over.
- **Fox vs ginger cat:** fox = pointed upright ears, long muzzle, long legs, bushy
  white-tipped tail. The owner has corrected a "ginger cat" to a fox before; if unsure,
  say so.
- **Badger vs black-and-white cat:** in infrared, a cat's white chest and paws can look
  like badger stripes. Badger = stocky and low, with a wedge-shaped head and stripes
  from nose to ears. Cat = slim, with an upright tail.
- **Female blackbird vs song thrush:** song thrush has a pale breast boldly spotted
  with dark arrowheads. A female blackbird is plain dark brown.
- **Pine marten (high priority, sometimes seen on DTCH):** long slim body, rich
  chocolate-brown, creamy-yellow throat bib, rounded pale-edged ears, long bushy tail.
  Keep any possible marten and put it first under Unusual wildlife.
- Camera notes: CAM1 on screen is the REAR camera. TUMB has some files stamped 2023
  because its clock was wrong.
