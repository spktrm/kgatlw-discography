#!/usr/bin/env python
"""Download King Gizzard & The Lizard Wizard's entire Bandcamp discography,
package each album as a .zip, extract the zips, and (optionally) build a
matching Spotify playlist per album.

Usage:
    python download_discography.py download                  # download + zip + extract
    python download_discography.py spotify                   # build Spotify playlists from extracted albums

Spotify needs these env vars: SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET
Your playlists get a colour-inverted version of the album art as their cover so
Spotify doesn't reject it as a duplicate of their internal artwork.

NOTE: The Spotify Web API cannot add local files to playlists — playlists are
populated by matching each album's tracks to the Spotify catalogue instead.
"""

import argparse
import base64
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import socket
import sys
import threading
import time
import zipfile
from urllib.parse import urlencode
from urllib.request import urlopen, Request

import requests
from PIL import Image, ImageOps
from tqdm import tqdm

HERE = os.path.dirname(os.path.abspath(__file__))
VENV_SITE = os.path.join(HERE, "bandcamp-env", "lib", "python3.10", "site-packages")
if VENV_SITE not in sys.path:
    sys.path.insert(0, VENV_SITE)

from bandcamp_dl.bandcamp import Bandcamp
from bandcamp_dl.bandcampdownloader import BandcampDownloader

ARTIST = "kinggizzard"
ARTIST_NAME = "King Gizzard & The Lizard Wizard"
WORK = os.path.join(HERE, ".tmp")
HIGH_WATER = os.path.join(WORK, ".downloaded.json")
DEST = os.path.join(HERE, "Raw")
EXTRACT = os.path.join(HERE, "Extracted")
SPOTIFY_TOKEN = os.path.join(HERE, ".spotify_token.json")
REDIRECT_PORT = 8888
REDIRECT_URI = f"http://127.0.0.1:{REDIRECT_PORT}/callback"
SCOPES = "playlist-modify-private playlist-read-private ugc-image-upload"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API = "https://api.spotify.com"


def sanitize(name):
    return name.replace("/", "-").replace(":", "-").replace("?", "").strip()


# --------------------------------------------------------------------------- #
# Download / zip / extract
# --------------------------------------------------------------------------- #
def build_args():
    return argparse.Namespace(
        template="%{artist} - %{album}/%{track} - %{title}",
        base_dir=WORK, overwrite=False, no_art=False, embed_art=False,
        embed_lyrics=False, group=False, no_slugify=True, ok_chars="-_~",
        space_char="-", ascii_only=False, keep_spaces=False, keep_upper=False,
        embed_genres=False, debug=False, no_confirm=True, full_album=False,
    )


def list_track_files(folder):
    return [os.path.join(folder, f) for f in os.listdir(folder)
            if f.lower().endswith(".mp3")]


def download_step():
    os.makedirs(WORK, exist_ok=True)
    done = set()
    if os.path.exists(HIGH_WATER):
        with open(HIGH_WATER) as f:
            done = set(json.load(f))

    bandcamp = Bandcamp()
    print("Fetching discography from Bandcamp...")
    urls = Bandcamp.get_full_discography(bandcamp, ARTIST, "music")
    album_urls = sorted({u for u in urls if "/album/" in u})
    print(f"Found {len(album_urls)} albums.")

    downloader = BandcampDownloader(build_args(), album_urls[0])
    for url in tqdm(album_urls, desc="Albums", unit="album"):
        if url in done:
            tqdm.write(f"Skipping (already downloaded): {url}")
            continue
        album = bandcamp.parse(url, art=True)
        if album is None:
            continue
        folder_name = sanitize(f"{album['artist']} - {album['title']}")
        folder = os.path.join(WORK, folder_name)
        tqdm.write(f"\n=== {folder_name} ({len(album['tracks'])} tracks) ===")
        downloader = BandcampDownloader(build_args(), url)
        downloader.start(album)
        done.add(url)
        with open(HIGH_WATER, "w") as f:
            json.dump(sorted(done), f, indent=2)


def zip_step():
    os.makedirs(DEST, exist_ok=True)
    folders = [os.path.join(WORK, d) for d in os.listdir(WORK)
               if os.path.isdir(os.path.join(WORK, d)) and not d.startswith(".")]
    for folder in folders:
        base = os.path.basename(folder)
        zip_path = os.path.join(DEST, f"{base}.zip")
        files = [os.path.join(r, f) for r, _d, fs in os.walk(folder) for f in fs]
        with tqdm(total=len(files), desc=f"Zipping {base}", unit="file") as pbar:
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for full in files:
                    zf.write(full, arcname=os.path.relpath(full, folder))
                    pbar.update(1)
    print(f"\nAll albums packaged as .zip in {DEST}")


def extract_step():
    os.makedirs(EXTRACT, exist_ok=True)
    zips = [f for f in os.listdir(DEST) if f.lower().endswith(".zip")]
    for name in zips:
        zip_path = os.path.join(DEST, name)
        target = os.path.join(EXTRACT, name[:-4])
        os.makedirs(target, exist_ok=True)
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            with tqdm(total=len(names), desc=f"Extracting {name}", unit="file") as pbar:
                for n in names:
                    zf.extract(n, target)
                    pbar.update(1)
    print(f"\nAll albums extracted to {EXTRACT}")


# --------------------------------------------------------------------------- #
# Spotify
# --------------------------------------------------------------------------- #
def load_dotenv(path=None):
    path = path or os.path.join(HERE, ".env")
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val


def spotify_token():
    if os.path.exists(SPOTIFY_TOKEN):
        data = json.load(open(SPOTIFY_TOKEN))
        if time.time() < data["expires_at"]:
            return data["access_token"], data["refresh_token"]
        refresh = refresh_access(data)
        if refresh:
            return refresh
    return authorize()


def _save_token(payload, refresh_token=None):
    data = {
        "access_token": payload["access_token"],
        "expires_at": time.time() + payload["expires_in"] - 60,
        "refresh_token": refresh_token or payload.get("refresh_token"),
    }
    with open(SPOTIFY_TOKEN, "w") as f:
        json.dump(data, f)
    return data


def _requests_token(form, headers=None):
    r = requests.post(TOKEN_URL, data=form, headers=headers)
    r.raise_for_status()
    return r.json()


def refresh_access(data):
    client_id = os.environ.get("SPOTIFY_CLIENT_ID", "")
    client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET", "")
    form = {"grant_type": "refresh_token",
            "refresh_token": data["refresh_token"],
            "client_id": client_id}
    if client_secret:
        form["client_secret"] = client_secret
    try:
        payload = _requests_token(form)
    except Exception:
        return None
    saved = _save_token(payload, data["refresh_token"])
    return saved["access_token"], saved["refresh_token"]


def authorize():
    client_id = os.environ.get("SPOTIFY_CLIENT_ID")
    client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET", "")
    if not client_id:
        sys.exit("Set SPOTIFY_CLIENT_ID (and optionally SPOTIFY_CLIENT_SECRET) "
                 "in the environment, then re-run.")

    verifier = secrets.token_urlsafe(64)
    challenge = (base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=").decode())

    auth_url = ("https://accounts.spotify.com/authorize?" + urlencode({
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "code_challenge_method": "S256",
        "code_challenge": challenge,
        "scope": SCOPES,
    }))
    print("Open this URL and authorise the app:\n" + auth_url)

    code_holder = {}
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", REDIRECT_PORT))
    server.listen(1)

    def serve():
        conn, _ = server.accept()
        req = conn.recv(4096).decode()
        path = req.split(" ")[1]
        if "code=" in path:
            code_holder["code"] = path.split("code=")[1].split("&")[0]
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK")
        conn.close()
        server.close()

    threading.Thread(target=serve, daemon=True).start()
    while "code" not in code_holder:
        time.sleep(0.2)

    form = {"grant_type": "authorization_code",
            "code": code_holder["code"],
            "redirect_uri": REDIRECT_URI,
            "client_id": client_id,
            "code_verifier": verifier}
    if client_secret:
        form["client_secret"] = client_secret
    payload = _requests_token(form)
    saved = _save_token(payload)
    return saved["access_token"], saved["refresh_token"]


def spotify_api(method, path, token, **kw):
    headers = {"Authorization": f"Bearer {token}"}
    headers.update(kw.pop("headers", {}))
    r = requests.request(method, API + path, headers=headers, **kw)
    r.raise_for_status()
    return r.json() if r.content else None


def inverted_cover_bytes(cover_path):
    img = Image.open(cover_path).convert("RGB")
    img.thumbnail((640, 640))
    inv = ImageOps.invert(img)
    buf = io.BytesIO()
    inv.save(buf, format="JPEG", quality=92)
    return base64.b64encode(buf.getvalue())


def search_track(token, title, album):
    album_q = album.replace('"', "\\\"")
    title_q = title.replace('"', "\\\"")
    q = f'track:"{title_q}" album:"{album_q}" artist:"{ARTIST_NAME}"'
    data = spotify_api("GET",
                       "/v1/search?" + urlencode({"q": q, "type": "track", "limit": 1}),
                       token)
    items = data.get("tracks", {}).get("items", [])
    if not items:
        q2 = f'track:"{title_q}" artist:"{ARTIST_NAME}"'
        data = spotify_api("GET",
                           "/v1/search?" + urlencode({"q": q2, "type": "track", "limit": 1}),
                           token)
        items = data.get("tracks", {}).get("items", [])
    return items[0]["uri"] if items else None


def parse_track_title(fname):
    title = fname.rsplit(".", 1)[0]
    if " - " in title:
        title = title.rsplit(" - ", 1)[1]
    return re.sub(r"^\d+\s+", "", title.strip()).strip()


def spotify_step():
    load_dotenv()
    if not os.path.isdir(EXTRACT):
        sys.exit(f"No extracted albums found in {EXTRACT}. Run `download` first.")
    token, _ = spotify_token()
    me = spotify_api("GET", "/v1/me", token)
    user_id = me["id"]
    print(f"Authenticated as {me.get('display_name', user_id)}")

    albums = [d for d in os.listdir(EXTRACT)
              if os.path.isdir(os.path.join(EXTRACT, d))]
    for album_dir in tqdm(albums, desc="Albums", unit="album"):
        folder = os.path.join(EXTRACT, album_dir)
        album_title = album_dir.replace("; or-", "; or", 1)
        if " - " in album_title:
            album_title = album_title.split(" - ", 1)[1]
        album_title = album_title.strip()

        audio_files = [os.path.join(r, f)
                       for r, _d, fs in os.walk(folder) for f in fs
                       if f.lower().endswith((".mp3", ".flac"))]
        cover = next((os.path.join(r, f)
                      for r, _d, fs in os.walk(folder) for f in fs
                      if f.lower() == "cover.jpg"), None)

        playlist = spotify_api("POST", f"/v1/users/{user_id}/playlists", token,
                               json={"name": f"KGATLW - {album_title}",
                                     "public": False})
        pl_id = playlist["id"]
        tqdm.write(f"\nCreated playlist: {playlist['name']}")

        uris = []
        for full in sorted(audio_files):
            fname = os.path.basename(full)
            track_title = parse_track_title(fname)
            uri = search_track(token, track_title, album_title)
            if uri:
                uris.append(uri)
            else:
                tqdm.write(f"  ! no match: {track_title}")

        for i in range(0, len(uris), 100):
            spotify_api("POST", f"/v1/playlists/{pl_id}/tracks", token,
                        json={"uris": uris[i:i + 100]})
        tqdm.write(f"  added {len(uris)}/{len(audio_files)} tracks")

        if cover:
            img_b64 = inverted_cover_bytes(cover)
            requests.put(f"{API}/v1/playlists/{pl_id}/images", data=img_b64,
                         headers={"Authorization": f"Bearer {token}",
                                  "Content-Type": "image/jpeg"}).raise_for_status()
            tqdm.write(f"  uploaded inverted cover art")
    print("\nDone.")


# --------------------------------------------------------------------------- #
def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="step", required=True)
    sub.add_parser("download", help="fetch, zip and extract every album")
    sub.add_parser("spotify", help="create playlists from extracted albums")
    args = parser.parse_args()

    if args.step == "download":
        download_step()
        zip_step()
        extract_step()
        shutil.rmtree(WORK, ignore_errors=True)
    elif args.step == "spotify":
        spotify_step()


if __name__ == "__main__":
    main()
