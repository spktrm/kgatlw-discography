# kgatlw-discography

Download King Gizzard & The Lizard Wizard's entire Bandcamp discography, package
each album as a `.zip`, extract the zips, and repair each track's metadata so it
plays correctly (correct track/disc order, totals, and embedded cover art).

## Requirements

- Python 3.10+
- A virtual environment with dependencies installed:

```bash
python3 -m venv bandcamp-env
bandcamp-env/bin/pip install bandcamp-downloader yt-dlp requests Pillow tqdm
```

## Usage

```bash
bandcamp-env/bin/python download_discography.py download
```

This performs the full pipeline, in order:

1. **download** — fetches every album from `https://kinggizzard.bandcamp.com/music`
   (MP3s + cover art) into a temporary working dir.
2. **zip** — packages each album into `Zips/<Artist - Album>.zip`.
3. **extract** — unpacks each zip into `Extracted/<Artist - Album>/`.
4. **metafix** — repairs the tags on every extracted file, and may also be run
   on its own.

The temporary working dir is removed when done.

## What's automated vs manual

The only step Spotify's API can't do is adding the local audio files to a
playlist, so that one step is manual. Everything around it is automated:

| Step | Automated? | How |
|------|-----------|-----|
| Download tracks + cover from Bandcamp | ✅ automated | `download` |
| Package / extract / fix metadata + art | ✅ automated | `download`, `metafix` |
| Import the local files into Spotify (one playlist per album) | ⚠️ **manual** | in the Spotify app |
| Name, order, and set cover on those playlists | ✅ automated | `organize` |
| Build playlists from the Spotify catalogue (no local import) | ✅ automated | `spotify` |
| One-time app authorisation (accept the prompt) | ⚠️ **manual** | first run of any Spotify step |

After the manual import, run `organize` to finish the job:

```bash
bandcamp-env/bin/python download_discography.py organize
```

It finds the playlists that contain only `spotify:local:` tracks, matches each
one to the album whose extracted folder has the same track names, renames it to
just the album title, reorders the tracks to the official album order, and sets
the album's cover art (resized to a 512x512 square) as the playlist image.

## metafix

Run alone (e.g. after editing/fixing the extracted files elsewhere):

```bash
bandcamp-env/bin/python download_discography.py metafix
```

For every audio file it sets (without touching title/artist/album/year):

- `TRCK` / `tracknumber` → `track/total` (e.g. `12/34`)
- `TPOS` / `discnumber` → `disc/total-discs` (adds disc info if missing)
- Embeds the folder's `cover.jpg` as the track's cover art

This gives players and importer tools the correct track ordering and artwork.

## Layout

| Path | Contents |
|------|----------|
| `download_discography.py` | Main script |
| `bandcamp-env/` | Python virtual environment (git-ignored) |
| `Zips/`, `Raw/`, `Extracted/` | Album zips and extracted output (git-ignored) |