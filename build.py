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


def main():
    cache = json.loads(CACHE_FILE.read_text()) if CACHE_FILE.exists() else {}
    all_videos = []
    problems = []

    for entry in read_channel_list():
        try:
            channel_id = find_channel_id(entry, cache)
            videos = fetch_videos(channel_id)
            all_videos.extend(videos)
            print(f"OK   {entry}: {len(videos)} videos")
        except Exception as error:
            problems.append(entry)
            print(f"FAIL {entry}: {error}")

    CACHE_FILE.write_text(json.dumps(cache, indent=2) + "\n")

    all_videos.sort(key=lambda v: v["published"], reverse=True)
    template = (HERE / "template.html").read_text()
    page = template.replace("__VIDEOS__", json.dumps(all_videos).replace("</", "<\\/"))
    OUTPUT_DIR.mkdir(exist_ok=True)
    (OUTPUT_DIR / "index.html").write_text(page)
    print(f"\nBuilt page with {len(all_videos)} videos.")

    if problems and not all_videos:
        sys.exit("No videos could be loaded at all.")


if __name__ == "__main__":
    main()
