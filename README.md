# printarr

**Acoustic-fingerprint music identifier, tagger and renamer for Lidarr — like
[namer](https://github.com/ThePornDatabase/namer), but for music.**

Downloads often arrive with mangled names and wrong or missing tags, and Lidarr
gives up on importing them ("Artist name mismatch", "Unable to parse", manual
import required). printarr watches your Lidarr queue for exactly those stuck
items, identifies the actual audio content via **Chromaprint/AcoustID acoustic
fingerprints**, fixes the tags from **MusicBrainz** (including the MusicBrainz
IDs Lidarr matches on), renames the files, and then tells Lidarr to import —
fully automatically.

```
Lidarr queue (stuck)            printarr                          Lidarr
┌───────────────────┐   ┌──────────────────────────┐   ┌───────────────────────┐
│ importBlocked     │──▶│ 1. fpcalc fingerprints   │──▶│ ManualImport command  │
│ importFailed      │   │ 2. AcoustID → recordings │   │ with exact per-file   │
│ importPending ⚠   │   │ 3. score MB releases     │   │ track mapping         │
└───────────────────┘   │ 4. write tags + MB IDs   │   │        ⇩              │
                        │ 5. rename files          │   │      imported ✔       │
                        └──────────────────────────┘   └───────────────────────┘
```

## Why it works

Lidarr's import identification reads embedded MusicBrainz tags first: when all
files of a download share one `MUSICBRAINZ_ALBUMID` (the release MBID), Lidarr
looks that release up directly and matching becomes deterministic. printarr
writes exactly those tags (Picard conventions: Vorbis comments, ID3v2.4 with
`UFID`/`TXXX` frames, MP4 freeform atoms) — and, for albums Lidarr already
grabbed, additionally triggers Lidarr's `ManualImport` command with an explicit
file→track mapping, so nothing is left to guesswork.

Like namer, printarr is deliberately conservative: **if the best match is
ambiguous or below the confidence threshold, it refuses and leaves the folder
alone**, writing a `printarr-report.json` with all candidates considered
instead. Mistagging a music library is worse than a manual import.

## Features

- **Queue mode**: polls the Lidarr queue, fixes stuck downloads, triggers the
  import — the ManualImport command carries per-file track IDs, with
  `disableReleaseSwitching` so Lidarr keeps the release printarr verified.
- **Acoustic fingerprinting** with `fpcalc`/AcoustID as the primary signal,
  falling back to duration, existing tags and track-number heuristics.
- **Correct tags**: full Picard-convention tag set including all MusicBrainz
  IDs, track/disc totals, label, catalog number, release country — for FLAC,
  MP3, M4A/MP4, Ogg Vorbis and Opus. Unrelated existing tags (ReplayGain,
  ratings) are preserved by default.
- **Cover art** from the Cover Art Archive (release, then release-group
  fallback).
- **Renaming** with configurable templates (`{track:02d} - {title}`), safe for
  every filesystem, collision-free, multi-disc aware.
- **Watch folders** independent of the queue, namer-style: a folder is picked
  up once it stops changing, processed, and optionally handed to Lidarr via
  `DownloadedAlbumsScan`.
- **Review web UI** for everything printarr refused to match automatically:
  see the candidates that were considered, pick the right one (or paste any
  MusicBrainz URL), and printarr force-tags the folder and triggers the
  import — namer's failed-dir web UI, adapted for Lidarr.
- **Dry-run everything** (`--dry-run`, `printarr identify`) before letting it
  touch your files.
- Path mappings for split-container setups, state file to avoid rework,
  API rate-limit compliance (MusicBrainz 1 req/s, AcoustID 3 req/s) built in.

## Installation

Requirements: Python ≥ 3.11 and `fpcalc` (Chromaprint).

```bash
# Debian/Ubuntu
sudo apt install libchromaprint-tools
pip install git+https://github.com/styx-techno/lidarr-printarr

# or with Docker
docker compose -f docker-compose.example.yml up -d
```

You need two free keys:

1. **Lidarr API key**: Settings → General → API Key.
2. **AcoustID application key**: log in at [acoustid.org](https://acoustid.org)
   and register an application. (Optional but strongly recommended — without it
   printarr matches on tags/durations only.)

## Configuration

Copy [`config/printarr.example.toml`](config/printarr.example.toml) to
`./printarr.toml`, `~/.config/printarr/printarr.toml` or `/config/printarr.toml`
and fill in the keys. Every option is also available as an environment variable
(`PRINTARR_<SECTION>_<KEY>`, e.g. `PRINTARR_LIDARR_API_KEY`), which wins over
the file.

Minimal example:

```toml
[lidarr]
url = "http://localhost:8686"
api_key = "…"

[acoustid]
api_key = "…"

[musicbrainz]
contact = "you@example.com"   # goes into the MusicBrainz User-Agent
```

If Lidarr and printarr see the download folder under different paths (separate
containers), add a mapping:

```toml
[queue]
path_mappings = ["/data/downloads => /downloads"]
```

## Usage

```bash
printarr identify /downloads/Weird.Release.2003-GRP   # dry-run: show the match
printarr process  /downloads/Weird.Release.2003-GRP   # fix tags + names in place
printarr process  --release-group <MBID> …            # with a release-group hint
printarr queue                                        # fix all stuck queue items once
printarr watch                                        # run continuously (daemon)
printarr --dry-run queue                              # see what would happen
```

Global flags (`--dry-run`, `--config`, `--log-level`) go before the subcommand.

## Review web UI

Automatic matching deliberately refuses ambiguous results instead of guessing.
The web UI is where those land for a human decision:

```toml
[web]
enabled = true          # started automatically by `printarr watch`
host = "127.0.0.1"      # "0.0.0.0" inside Docker
port = 8687
```

It lists all stuck Lidarr downloads and failed watch folders together with the
candidate releases printarr considered (scores, fingerprint hits, MusicBrainz
links). One click on **Use this** — or pasting any MusicBrainz release or
release-group URL — force-tags the folder with that release and triggers the
Lidarr import. **Retry auto** re-runs automatic identification (MusicBrainz
data improves over time). The UI has no authentication; keep it on localhost
or behind a reverse proxy.

`identify` output shows the per-file mapping, `♪` marking fingerprint-confirmed
tracks:

```
Boards of Canada — Geogaddi (2002)
  release:       https://musicbrainz.org/release/…
  matched 23/23 tracks, score 0.97
  ♪ 1-01 Ready Lets Go  <-  bocgeo01.mp3  (0.99)
  ♪ 1-02 Music Is Math  <-  bocgeo02.mp3  (0.98)
  …
```

## How matching works

1. Every audio file is fingerprinted with `fpcalc` and looked up on AcoustID,
   yielding candidate MusicBrainz *recordings* (with scores) and the *releases*
   containing them.
2. Candidate releases come from the Lidarr album (queue mode), a release-group
   hint, AcoustID release votes, or a MusicBrainz text search — in that order.
3. Files are assigned to release tracks one-to-one. A fingerprint hit scores
   0.7–1.0; otherwise duration proximity (45 %), title similarity (35 %) and
   track-number hints (20 %) decide.
4. The best release must clear `min_release_confidence`, cover all files (by
   default) and beat the runner-up by a clear margin, unless the runner-up is
   just another pressing from the same release group. Otherwise: refusal +
   report file, never a guess.

## Notes & caveats

- **Torrent seeding**: renaming files inside a still-seeding folder breaks
  seeding. Set `renaming.enabled = false` if that matters — corrected tags
  alone are usually enough for a clean Lidarr import (`import_mode = "auto"`
  copies instead of moving while a torrent is seeding).
- printarr never deletes audio files and never moves them out of their folder;
  Lidarr stays the owner of your library layout.
- MusicBrainz data improves over time — failed items are retried after
  `retry_cooldown` automatically in watch mode.

## Development

```bash
pip install -e ".[dev]"
sudo apt install ffmpeg libchromaprint-tools   # test fixtures are real audio
pytest
ruff check printarr tests
```

## License

[MIT](LICENSE)
