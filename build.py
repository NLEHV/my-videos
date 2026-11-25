"""
Builds your personal YouTube page.

What it does, step by step:
1. Reads your allowed channels from channels.txt
2. Looks up each channel's ID (YouTube needs that for the feed)
3. Downloads the latest videos of each channel (no Shorts)
4. Writes a simple web page (_site/index.html) showing only those videos

It only uses tools that come with Python, so nothing needs to be installed.
"""

import html
import json
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).parent
CHANNELS_FILE = HERE / "channels.txt"
CACHE_FILE = HERE / "channel_ids.json"   # remembers channel IDs so we don't look them up every time
OUTPUT_DIR = HERE / "_site"
VIDEOS_PER_CHANNEL = 15

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
                  "(KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Accept-Language": "en-US,en;q=0.9",
    "Cookie": "SOCS=CAI",  # skips YouTube's cookie-consent page
}

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}


def download(url):
    request = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8")


def read_channel_list():
    channels = []
    for line in CHANNELS_FILE.read_text().splitlines():
        line = line.split("#")[0].strip()   # everything after # is a note
        if line:
            channels.append(line)
    return channels


def find_channel_id(entry, cache):
    """Turns '@handle', a channel link, or a 'UC...' ID into a channel ID."""
    if entry in cache:
        return cache[entry]

    match = re.search(r"(UC[\w-]{22})", entry)
    if match:
        channel_id = match.group(1)
    else:
        handle = entry.rstrip("/").split("/")[-1]
        if not handle.startswith("@"):
            handle = "@" + handle
        page = download(f"https://www.youtube.com/{handle}")
        match = (re.search(r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[\w-]{22})"', page)
                 or re.search(r'"externalId":"(UC[\w-]{22})"', page))
        if not match:
            raise ValueError("could not find this channel")
        channel_id = match.group(1)

    cache[entry] = channel_id
    return channel_id


def fetch_videos(channel_id):
    # "UULF" is YouTube's list of a channel's normal videos (without Shorts).
    # If that doesn't work for some reason, fall back to all uploads.
    urls = [
        f"https://www.youtube.com/feeds/videos.xml?playlist_id=UULF{channel_id[2:]}",
        f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}",
    ]
    last_error = None
    for url in urls:
        try:
            feed = ET.fromstring(download(url))
            break
        except Exception as error:
            last_error = error
    else:
        raise last_error

    channel_name = feed.findtext("atom:author/atom:name", default="", namespaces=NS) \
        or feed.findtext("atom:title", default="", namespaces=NS)

    videos = []
    for item in feed.findall("atom:entry", NS)[:VIDEOS_PER_CHANNEL]:
        video_id = item.findtext("yt:videoId", namespaces=NS)
        if not video_id:
            continue
        videos.append({
            "id": video_id,
            "title": item.findtext("atom:title", default="", namespaces=NS),
            "published": item.findtext("atom:published", default="", namespaces=NS),
            "channel": channel_name,
        })
    return videos


def find_all(data, key, found):
    """Searches YouTube's page data for every item called `key`."""
    if isinstance(data, dict):
        for k, v in data.items():
            if k == key:
                found.append(v)
            find_all(v, key, found)
    elif isinstance(data, list):
        for item in data:
            find_all(item, key, found)
    return found


def fetch_playlists(channel_id):
    """Reads the playlists shown on the channel's 'Playlists' tab."""
    page = download(f"https://www.youtube.com/channel/{channel_id}/playlists")
    start = page.find("{", page.find("ytInitialData"))
    data = json.JSONDecoder().raw_decode(page[start:])[0]

    playlists = []
    for item in find_all(data, "lockupViewModel", []):
        if "PLAYLIST" not in item.get("contentType", "") and "SHOW" not in item.get("contentType", ""):
            continue
        text = json.dumps(item)
        count = re.search(r'"text": "([\d,.]+\s+(?:videos?|episodes?|lessons?))"', text)
        thumb = re.search(r'"url": "(https://i\.ytimg\.com/[^"]+)"', text)
        playlists.append({
            "id": item["contentId"],
            "title": item["metadata"]["lockupMetadataViewModel"]["title"]["content"],
            "count": count.group(1) if count else "",
            "thumb": thumb.group(1) if thumb else "",
        })
    return playlists


def fetch_playlist_videos(playlist_id):
    """Reads the videos in one playlist (YouTube shows up to about 100 at once)."""
    page = download(f"https://www.youtube.com/playlist?list={playlist_id}")
    start = page.find("{", page.find("ytInitialData"))
    data = json.JSONDecoder().raw_decode(page[start:])[0]

    # YouTube uses three different layouts for playlist items, so check all of them.
    videos = []
    for item in find_all(data, "lockupViewModel", []):
        if "VIDEO" in item.get("contentType", ""):
            videos.append({
                "id": item["contentId"],
                "title": item["metadata"]["lockupMetadataViewModel"]["title"]["content"],
            })
    for item in find_all(data, "playlistVideoRenderer", []):
        if item.get("videoId"):
            videos.append({
                "id": item["videoId"],
                "title": "".join(run.get("text", "") for run in item.get("title", {}).get("runs", [])),
            })
    for item in find_all(data, "shortsLockupViewModel", []):
        video_id = re.search(r'"videoId": "([\w-]{11})"', json.dumps(item))
        if video_id:
            videos.append({
                "id": video_id.group(1),
                "title": item.get("overlayMetadata", {}).get("primaryText", {}).get("content", ""),
            })
    return videos


def add_playlist_videos(all_playlists):
    """Fills in the videos of every playlist, several at a time to save time."""
    playlists = [p for channel in all_playlists.values() for p in channel]

    def fill(playlist):
        try:
            playlist["videos"] = fetch_playlist_videos(playlist["id"])
        except Exception as error:
            playlist["videos"] = []
            print(f"     playlist '{playlist['title']}' failed ({error})")

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(fill, playlists))
    print(f"Loaded videos for {len(playlists)} playlists.")


def main():
    cache = json.loads(CACHE_FILE.read_text()) if CACHE_FILE.exists() else {}
    all_videos = []
    all_playlists = {}   # channel name -> list of playlists
    problems = []

    for entry in read_channel_list():
        try:
            channel_id = find_channel_id(entry, cache)
            videos = fetch_videos(channel_id)
            all_videos.extend(videos)
        except Exception as error:
            problems.append(entry)
            print(f"FAIL {entry}: {error}")
            continue

        # Playlists are a bonus: if they fail, the videos still show.
        playlist_note = ""
        if videos:
            try:
                all_playlists[videos[0]["channel"]] = fetch_playlists(channel_id)
                playlist_note = f", {len(all_playlists[videos[0]['channel']])} playlists"
            except Exception as error:
                playlist_note = f", playlists failed ({error})"
        print(f"OK   {entry}: {len(videos)} videos{playlist_note}")

    add_playlist_videos(all_playlists)
    CACHE_FILE.write_text(json.dumps(cache, indent=2) + "\n")

    all_videos.sort(key=lambda v: v["published"], reverse=True)
    template = (HERE / "template.html").read_text()
    page = (template
            .replace("__VIDEOS__", json.dumps(all_videos).replace("</", "<\\/"))
            .replace("__PLAYLISTS__", json.dumps(all_playlists).replace("</", "<\\/")))
    OUTPUT_DIR.mkdir(exist_ok=True)
    (OUTPUT_DIR / "index.html").write_text(page)
    print(f"\nBuilt page with {len(all_videos)} videos.")

    if problems and not all_videos:
        sys.exit("No videos could be loaded at all.")


if __name__ == "__main__":
    main()
