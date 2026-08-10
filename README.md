# kgatlw-discography

Download King Gizzard & The Lizard Wizard's entire Bandcamp discography, package
each album as a `.zip`, extract the zips, and (optionally) build a matching
Spotify playlist per album.

## Requirements

- Python 3.10+
- A virtual environment with dependencies installed:

```bash
python3 -m venv bandcamp-env
bandcamp-env/bin/pip install bandcamp-downloader yt-dlp requests Pillow tqdm
```

## Download discography (Bandcamp)

```bash
bandcamp-env/bin/python download_discography.py download
```

This:
1. Fetches every album URL from `https://kinggizzard.bandcamp.com/music`.
2. Downloads each album (MP3s + cover art) into `.tmp/`.
3. Packages each into `Zips/<Artist - Album>.zip`.
4. Extracts each zip into `Extracted/<Artist - Album>/`.

`.tmp/` is a working directory and is removed when done.

## Build Spotify playlists

```bash
# 1. Create a Spotify app at https://developer.spotify.com/dashboard
#    and add the redirect URI (Settings -> Redirect URIs):
#        http://127.0.0.1:8888/callback
#    NOTE: use 127.0.0.1 (loopback). Spotify rejects the "localhost"
#    hostname as insecure.

# 2. Provide credentials (or copy .env.example to .env and fill it in)
export SPOTIFY_CLIENT_ID="your_client_id"
export SPOTIFY_CLIENT_SECRET="your_client_secret"

# 3. Run
bandcamp-env/bin/python download_discography.py spotify
```

The Spotify step:
- Authorizes via OAuth (PKCE) with a local callback server on port 8888.
- Creates one **private** playlist per extracted album, named `KGATLW - <Album>`.
- Matches each album's tracks to the Spotify catalogue by track/album/artist.
- Uploads a **colour-inverted** version of the album art as the playlist cover,
  so Spotify doesn't reject it as a duplicate of its internal artwork.

## Limitations

- **Local files cannot be added via the Spotify Web API.** Playlists are instead
  populated by matching each track to Spotify's catalogue (they can't reference
  the downloaded local files).
- Development-mode Spotify apps require the **app owner to have a Premium
  account** to function; non-allowlisted users get `403 Forbidden`.

## Layout

| Path | Contents |
|------|----------|
| `download_discography.py` | Main script |
| `bandcamp-env/` | Python virtual environment (git-ignored) |
| `Zips/`, `Raw/`, `Extracted/` | Album zips and extracted output (git-ignored) |
| `.env` | Spotify credentials (git-ignored) |
