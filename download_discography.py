#!/usr/bin/env python
"""Download King Gizzard & The Lizard Wizard's entire Bandcamp discography,
package each album as a .zip, extract the zips, and take charge of the matching
Spotify playlists once the local files have been imported by hand.

Usage:
    python download_discography.py download    # download + zip + extract + metafix
    python download_discography.py organize    # name + order + set cover on hand-imported playlists
    python download_discography.py metafix     # repair tags + embed cover on extracted files

Spotify needs these env vars: SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET

NOTE: The Spotify Web API cannot add local files to playlists, so importing the
files into Spotify (one playlist per album) is a manual step. `organize` then
names each playlist after its album, orders the tracks, and sets the album art.
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
from PIL import Image
from tqdm import tqdm

HERE = os.path.dirname(os.path.abspath(__file__))
VENV_SITE = os.path.join(HERE, "bandcamp-env", "lib", "python3.10", "site-packages")
if VENV_SITE not in sys.path:
    sys.path.insert(0, VENV_SITE)

from bandcamp_dl.bandcamp import Bandcamp
from bandcamp_dl.bandcampdownloader import BandcampDownloader

ARTIST = "kinggizzard"
WORK = os.path.join(HERE, ".tmp")
HIGH_WATER = os.path.join(WORK, ".downloaded.json")
DEST = os.path.join(HERE, "Raw")
EXTRACT = os.path.join(HERE, "Extracted")
SPOTIFY_TOKEN = os.path.join(HERE, ".spotify_token.json")
REDIRECT_PORT = 8888
REDIRECT_URI = f"http://127.0.0.1:{REDIRECT_PORT}/callback"
SCOPES = "playlist-modify-private playlist-modify-public playlist-read-private ugc-image-upload"
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
        "show_dialog": "true",
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


def get_playlist_items(token, playlist_id):
    items = []
    offset = 0
    while True:
        data = spotify_api("GET",
                           f"/v1/playlists/{playlist_id}/items?limit=50&offset={offset}",
                           token)
        batch = data["items"]
        items.extend(batch)
        if data.get("next"):
            offset += len(batch)
        else:
            break
    return items


def reorder_playlist(token, playlist_id, desired):
    """Reorder a playlist so its tracks match `desired` (list of track URIs)."""
    items = get_playlist_items(token, playlist_id)
    cur = [it["item"]["uri"] for it in items]
    if set(cur) != set(desired):
        raise RuntimeError("Playlist items do not match the expected track set.")
    pos = {u: i for i, u in enumerate(cur)}
    for i, uri in enumerate(desired):
        j = pos[uri]
        if j == i:
            continue
        spotify_api("PUT", f"/v1/playlists/{playlist_id}/items", token,
                    json={"range_start": j, "range_length": 1, "insert_before": i})
        el = cur.pop(j)
        cur.insert(i, el)
        pos = {u: idx for idx, u in enumerate(cur)}


def _norm(s):
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def _folder_order(folder):
    """Map normalized title -> official order index, from numbered filenames."""
    order = {}
    for name in sorted(os.listdir(folder)):
        base = os.path.splitext(name)[0]
        title = base
        if " - " in base:
            head, _, title = base.partition(" - ")
        order.setdefault(_norm(title), [])
        order[_norm(title)].append(base)
    return order


def _local_title(uri):
    body = uri.replace("spotify:local:", "")
    try:
        _artist, _album, title, _length = body.split(":")
    except ValueError:
        return None
    return title


def _order_key(uri, folder_order):
    if uri.startswith("spotify:local:"):
        t = _local_title(uri)
        return (0, "_".join(folder_order.get(_norm(t), []))) if t and _norm(t) in folder_order \
            else (1, uri)
    return (2, uri)


def _parse_filename_number(name):
    """Extract (disc, track) from a filename like 'NN - Title' or 'D-NN - Title'."""
    base = os.path.splitext(os.path.basename(name))[0]
    head = base.partition(" - ")[0].strip()
    disc, track = 1, None
    m = re.fullmatch(r"(\d+)[-_](\d+)", head)
    if m:
        disc, track = int(m.group(1)), int(m.group(2))
    elif re.fullmatch(r"\d+", head):
        track = int(head)
    return disc, track


def _read_cover(folder):
    for name in ("cover.jpg", "cover.png", "cover.jpeg", "folder.jpg"):
        path = os.path.join(folder, name)
        if os.path.exists(path):
            with open(path, "rb") as f:
                data = f.read()
            mime = "image/png" if name.endswith("png") else "image/jpeg"
            return data, mime
    return None, None


def _embed_art(aud, ext, data, mime):
    if not data:
        return
    if ext in (".flac", ".ogg"):
        from mutagen.flac import Picture
        pic = Picture()
        pic.type = 3
        pic.mime = mime
        pic.desc = "cover"
        pic.data = data
        aud.add_picture(pic)
    elif ext in (".mp3", ".m4a"):
        from mutagen.id3 import APIC
        aud.add(APIC(encoding=3, mime=mime, type=3, desc="cover", data=data))


def fix_tags(path, title, album, artist, disc, track, total_tracks, total_discs, art=()):
    ext = os.path.splitext(path)[1].lower()
    art_data, art_mime = art
    if ext in (".flac", ".ogg"):
        from mutagen.flac import FLAC
        from mutagen import FLACNoHeaderError  # noqa
        aud = FLAC(path)
        aud["tracknumber"] = [str(track)]
        aud["totaltracks"] = [str(total_tracks)]
        aud["discnumber"] = [str(disc)]
        aud["totaldiscs"] = [str(total_discs)]
        _embed_art(aud, ext, art_data, art_mime)
        aud.save()
        return
    if ext in (".mp3", ".m4a"):
        from mutagen.mp3 import MP3
        from mutagen.id3 import ID3, TRCK, TPOS
        aud = MP3(path)
        tags = aud.tags if aud.tags is not None else ID3()
        tags.add(TRCK(encoding=3, text=[f"{track}/{total_tracks}"]))
        tags.add(TPOS(encoding=3, text=[f"{disc}/{total_discs}"]))
        _embed_art(tags, ext, art_data, art_mime)
        aud.tags = tags
        aud.save()
        return
    raise ValueError(f"Unsupported audio format: {ext}")


def _find_cover(folder):
    for name in ("cover.jpg", "cover.png", "cover.jpeg", "folder.jpg"):
        path = os.path.join(folder, name)
        if os.path.exists(path):
            return path
    return None


def album_cover_bytes(cover_path, size=512, max_bytes=256 * 1024):
    """Return a base64 JPEG of the cover, centre-cropped to a square of `size`.

    Spotify caps playlist-image uploads at 256 KB, so the image is resized to a
    512x512 square and saved at a quality that keeps it under the limit.
    """
    img = Image.open(cover_path).convert("RGB")
    w, h = img.size
    side = min(w, h)
    img = img.crop(((w - side) // 2, (h - side) // 2,
                    (w + side) // 2, (h + side) // 2))
    img = img.resize((size, size), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    if buf.tell() < max_bytes:
        return base64.b64encode(buf.getvalue())
    for quality in (75, 60, 45, 30):
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        if buf.tell() < max_bytes:
            return base64.b64encode(buf.getvalue())
    return base64.b64encode(buf.getvalue())


def upload_cover(token, pid, folder):
    cover = _find_cover(folder)
    if not cover:
        return False
    img_b64 = album_cover_bytes(cover)
    requests.put(f"{API}/v1/playlists/{pid}/images", data=img_b64,
                 headers={"Authorization": f"Bearer {token}",
                          "Content-Type": "image/jpeg"}).raise_for_status()
    return True


def organize_step():
    """Name, order, and set cover on playlists the user imported manually.

    The Web API can't add local files, so the user has to import them by hand as a
    playlist per album. This step finds those playlists (ones containing
    `spotify:local:` tracks), figures out which album each matches by comparing the
    track titles against the extracted folder, renames it to the album title, and
    reorders it to the official album order.
    """
    load_dotenv()
    if not os.path.isdir(EXTRACT):
        sys.exit(f"No extracted albums found in {EXTRACT} to derive ordering from. "
                 "Run `download` first.")
    token, _ = spotify_token()
    me = spotify_api("GET", "/v1/me", token)
    print(f"Authenticated as {me.get('display_name', me['id'])}")

    # Index normalised local-title -> (album dir, filenames seen), so we can match
    # each hand-imported playlist to the album it came from.
    index = {}
    for album_dir in os.listdir(EXTRACT):
        folder = os.path.join(EXTRACT, album_dir)
        if not os.path.isdir(folder):
            continue
        order = _folder_order(folder)
        for norm in order:
            index.setdefault(norm, (album_dir, order))

    playlists = []
    offset = 0
    while True:
        data = spotify_api("GET", f"/v1/me/playlists?limit=50&offset={offset}", token)
        playlists.extend(data["items"])
        if data.get("next"):
            offset = len(playlists)
        else:
            break

    updated = renamed = 0
    for pl in playlists:
        pid = pl["id"]
        if pl["owner"]["id"] != me["id"]:
            continue
        try:
            items = get_playlist_items(token, pid)
        except Exception as e:
            print(f"  ! skip '{pl['name']}': {e}")
            continue

        local = [it["item"]["uri"] for it in items
                 if it["item"]["uri"].startswith("spotify:local:")]
        if not local:
            continue  # not a hand-imported local-files playlist

        titles = {_norm(t) for t in (_local_title(u) for u in local) if t}
        if not titles:
            print(f"  ! '{pl['name']}' has unreadable local tracks, skipping")
            continue

        # Pick the album whose folder filenames match the most tracks.
        best = best_hits = best_extra = None
        for album_dir, order in index.values():
            hits = sum(1 for t in titles if t in order)
            extra = len(titles - set(order))
            if best is None or (hits, -extra) > (best_hits, -best_extra):
                best, best_hits, best_extra = album_dir, hits, extra
        if best is None or not best_hits:
            continue  # not one of our albums — leave unrelated playlists untouched

        album_title = best.replace("; or-", "; or", 1)
        album_title = album_title.split(" - ", 1)[1].strip() if " - " in album_title else album_title.strip()
        name = album_title

        folder = os.path.join(EXTRACT, best)
        fold = _folder_order(folder)
        desired = [u for _k, u in sorted(((_order_key(u, fold), u) for u in local),
                                         key=lambda x: x[0])]

        try:
            if pl["name"] != name:
                spotify_api("PUT", f"/v1/playlists/{pid}", token,
                            json={"name": name, "public": False})
                print(f"  renamed '{pl['name']}' -> '{name}'")
                renamed += 1
            reorder_playlist(token, pid, desired)
            updated += 1
            if upload_cover(token, pid, folder):
                print(f"  updated cover for '{name}'")
            print(f"  ordered '{name}' ({len(local)} tracks)")
        except RuntimeError as e:
            print(f"  ! reorder failed for '{name}': {e}")
        except Exception as e:
            print(f"  ! failed for '{name}': {e}")

    print(f"\nDone. Named {renamed} and ordered {updated} manually-imported playlists.")


def metafix_step():
    AUDIO = (".mp3", ".flac", ".m4a", ".ogg", ".wav", ".aiff")
    if not os.path.isdir(EXTRACT):
        sys.exit(f"No {EXTRACT} folder found.")
    folders = [d for d in sorted(os.listdir(EXTRACT))
               if os.path.isdir(os.path.join(EXTRACT, d))]
    fixed = 0
    for album_dir in folders:
        folder = os.path.join(EXTRACT, album_dir)
        audio = [f for f in os.listdir(folder) if os.path.splitext(f)[1].lower() in AUDIO]
        if not audio:
            continue
        album, artist = album_dir.split(" - ", 1) if " - " in album_dir else (album_dir, album_dir)
        disc_nums = {_parse_filename_number(f)[0] for f in audio}
        total_discs = max(disc_nums) if disc_nums else 1
        art = _read_cover(folder)
        for name in sorted(audio):
            disc, track = _parse_filename_number(name)
            if track is None:
                continue
            path = os.path.join(folder, name)
            title = os.path.splitext(name)[0]
            title = title.partition(" - ")[2] if " - " in title else title
            try:
                fix_tags(path, title, album, artist, disc, track, len(audio), total_discs, art)
                fixed += 1
            except Exception as e:
                print(f"  ! {album_dir}/{name}: {e}")
        print(f"metafix: {album_dir} ({len(audio)} files)")
    print(f"\nMetafix complete: {fixed} files updated.")


# --------------------------------------------------------------------------- #
def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="step", required=True)
    sub.add_parser("download", help="fetch, zip and extract every album")
    sub.add_parser("organize", help="title + order + set cover on playlists you imported manually as local files (one per album)")
    sub.add_parser("metafix", help="repair track/disc number + embed cover tags on extracted files (runs automatically after download; also callable standalone)")
    args = parser.parse_args()

    if args.step == "download":
        download_step()
        zip_step()
        extract_step()
        metafix_step()
        shutil.rmtree(WORK, ignore_errors=True)
    elif args.step == "organize":
        organize_step()
    elif args.step == "metafix":
        metafix_step()


if __name__ == "__main__":
    main()
