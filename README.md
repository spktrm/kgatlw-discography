# kgatlw-discography

Download King Gizzard & The Lizard Wizard's entire Bandcamp discography, package
each album as a `.zip`, extract the zips, and repair each track's metadata so it
plays correctly (correct track/disc order, totals, and embedded cover art). Once
you've imported the albums into Spotify by hand, `organize` names each playlist,
orders its tracks, and sets the album art.

## Requirements

- Python 3.10+
- A virtual environment with dependencies installed:

```bash
python3 -m venv bandcamp-env
bandcamp-env/bin/pip install bandcamp-downloader yt-dlp requests Pillow tqdm
```
- A Spotify Developer app for any Spotify command (see below), plus its
  `SPOTIFY_CLIENT_ID` and `SPOTIFY_CLIENT_SECRET`.

## Spotify Developer app (required for `organize`)

`organize` talks to the Spotify Web API, which needs a developer account and app:

1. Create a **free developer account** at https://developer.spotify.com/ .
2. Go to the **Dashboard** → **Create app**. Give it any name and description,
   and accept the terms.
3. Under **Redirect URIs**, add exactly:

   ```
   http://127.0.0.1:8888/callback
   ```

   (This matches the value hardcoded in `download_discography.py` — the script
   runs a tiny local server on port `8888` to catch Spotify's callback.)
4. Copy your **Client ID** and **Client Secret** into a `.env` file next to the
   script (or export them in your shell):

   ```bash
   SPOTIFY_CLIENT_ID=your_client_id
   SPOTIFY_CLIENT_SECRET=your_client_secret
   ```

On the first run of `organize`, the script prints a URL to open; you authorise
the app in your browser and the local server picks up the callback automatically.

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

The Spotify Web API can't add local audio files to a playlist, so importing them
is necessarily manual. Everything around that is automated:

| Step | Automated? | How |
|------|-----------|-----|
| Download tracks + cover from Bandcamp | ✅ automated | `download` |
| Package / extract / fix metadata + art | ✅ automated | `download`, `metafix` |
| Import the local files into Spotify (one playlist per album) | ⚠️ **manual** | in the Spotify app |
| Name each album playlist, order its tracks, set its cover | ✅ automated | `organize` |
| One-time app authorisation (accept the prompt) | ⚠️ **manual** | first run of any Spotify step |

## Command order

For a full album-by-album workflow from scratch, run them in this order:

1. `download` — fetch + package + extract + fix metadata on every album
   (this one step covers steps 1-4 of the pipeline above by itself).
2. **Manually** import each extracted album folder into Spotify as its own
   playlist (the API can't add local files).
3. `organize` — name each playlist after its album, order the tracks, and set
   the album art.

The three commands are:

```bash
bandcamp-env/bin/python download_discography.py download
# ... import the extracted albums into Spotify by hand (one playlist each) ...
bandcamp-env/bin/python download_discography.py organize
# optional: fix metadata only, without re-downloading
bandcamp-env/bin/python download_discography.py metafix
```

## organize

Run after manually importing the local files (one playlist per album):

```bash
bandcamp-env/bin/python download_discography.py organize
```

It finds the playlists that contain `spotify:local:` tracks, matches each one to
the album whose extracted folder has the same track names (track titles stay
intact), renames it to just the album title, reorders the tracks to the official
album order, and sets the album's cover art (resized to a 512x512 square) as the
playlist image. Unrelated playlists are left untouched.

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