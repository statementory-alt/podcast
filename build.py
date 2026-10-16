#!/usr/bin/env python3
"""Turn two-host Markdown scripts into MP3 episodes and a podcast RSS feed.

    python build.py            # estimate cost, ask, generate missing episodes, write feed.xml
    python build.py --voices   # list ElevenLabs voices and choose Host A / Host B
    python build.py --yes      # skip the confirmation prompt
    python build.py --estimate # only show character count and estimated credits
    python build.py --publish  # also commit and push to the git remote (GitHub Pages)

Standard library only. The ElevenLabs key is read from .env (ELEVENLABS_API_KEY)
and is never printed.
"""

import argparse
import email.utils
import hashlib
import html
import json
import os
import re
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SCRIPTS_DIR = ROOT / "scripts"
EPISODES_DIR = ROOT / "episodes"
PARTS_DIR = ROOT / ".parts"
CONFIG_PATH = ROOT / "config.json"
MANIFEST_PATH = ROOT / "episodes" / "manifest.json"
FEED_PATH = ROOT / "feed.xml"

API = "https://api.elevenlabs.io"
CHUNK_LIMIT = 2000  # Text to Dialogue: keep total input text <= 2,000 chars per request
OUTPUT_FORMAT = "mp3_44100_128"
PREFERRED_MODELS = ["eleven_v4", "eleven_v3"]

DEFAULT_CONFIG = {
    "podcast_title": "Course Podcast",
    "podcast_description": "Private study podcast generated from course scripts.",
    "author": "Adrián",
    "language": "en",
    "base_url": "",  # e.g. https://<user>.github.io/<repo>  (no trailing slash)
    "host_a": {"name": "", "voice_id": ""},
    "host_b": {"name": "", "voice_id": ""},
    "model_id": "",  # empty = pick the best available dialogue model
    "stability": 0.5,
}


# ---------------------------------------------------------------- config / env

def load_env(provider="elevenlabs"):
    env_path = ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    if provider == "fish":
        key = os.environ.get("FISH_API_KEY")
        if not key:
            sys.exit("Missing FISH_API_KEY in .env")
        return key
    key = os.environ.get("ELEVENLABS_API_KEY") or os.environ.get("XI_API_KEY")
    if not key:
        sys.exit("Missing ELEVENLABS_API_KEY in .env")
    return key


FISH_API = "https://api.fish.audio/v1/tts"
FISH_USD_PER_MB = 15.0  # $ per million UTF-8 bytes of input text (s2-pro)


def fish_tts(key, text, voices, model, speed, retries=3):
    body = json.dumps({"text": text, "reference_id": voices, "format": "mp3",
                       "mp3_bitrate": 128, "prosody": {"speed": speed}}).encode()
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json", "model": model}
    for attempt in range(retries + 1):
        req = urllib.request.Request(FISH_API, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=300, context=SSL_CTX) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:500]
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(2 ** (attempt + 1))
                continue
            sys.exit(f"Fish Audio API error {e.code}: {detail}")
        except urllib.error.URLError as e:
            if attempt < retries:
                time.sleep(2 ** (attempt + 1))
                continue
            sys.exit(f"Network error calling Fish Audio: {e.reason}")


def fish_text(chunk):
    # Fish S2 multi-speaker: <|speaker:N|> switches to reference_id[N].
    return " ".join(f"<|speaker:{0 if s == 'A' else 1}|>{t}" for s, t in chunk)


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if CONFIG_PATH.exists():
        user = json.loads(CONFIG_PATH.read_text())
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    return cfg


def save_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- HTTP

def _ssl_context():
    # python.org macOS builds ship without CA certs; fall back to certifi or the system bundle.
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass
    if ssl.get_default_verify_paths().cafile is None and Path("/etc/ssl/cert.pem").exists():
        return ssl.create_default_context(cafile="/etc/ssl/cert.pem")
    return ssl.create_default_context()


SSL_CTX = _ssl_context()


def api(key, method, path, body=None, query=None, raw=False, retries=3, fatal=True):
    url = API + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    data = json.dumps(body).encode() if body is not None else None
    headers = {"xi-api-key": key, "Accept": "*/*"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=300, context=SSL_CTX) as resp:
                payload = resp.read()
                if raw:
                    return payload, resp.headers
                return json.loads(payload)
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:500]
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(2 ** (attempt + 1))
                continue
            msg = f"ElevenLabs API error {e.code} on {path}: {detail}"
            if not fatal:
                raise RuntimeError(msg)
            sys.exit(msg)
        except urllib.error.URLError as e:
            if attempt < retries:
                time.sleep(2 ** (attempt + 1))
                continue
            sys.exit(f"Network error on {path}: {e.reason}")


# ---------------------------------------------------------------- voices

def list_voices(key):
    voices = []
    token = None
    while True:
        q = {"page_size": 100}
        if token:
            q["next_page_token"] = token
        res = api(key, "GET", "/v2/voices", query=q)
        voices += res.get("voices", [])
        if not res.get("has_more"):
            break
        token = res.get("next_page_token")
    return voices


def choose_voices(key, cfg):
    voices = list_voices(key)
    if not voices:
        sys.exit("No voices found in your ElevenLabs account.")
    print("\nVoices in your ElevenLabs account:\n")
    for i, v in enumerate(voices, 1):
        labels = v.get("labels") or {}
        desc = ", ".join(str(x) for x in (labels.get("gender"), labels.get("accent"),
                                          labels.get("age"), labels.get("descriptive")
                                          or labels.get("description")) if x)
        print(f"  {i:>2}. {v['name']:<28} {v['voice_id']}  {v.get('category', '')}  {desc}")
    if not sys.stdin.isatty():
        print("\nRun interactively, or set host_a.voice_id / host_b.voice_id in config.json.")
        return False

    def pick(label):
        while True:
            ans = input(f"\nNumber or voice_id for {label}: ").strip()
            if ans.isdigit() and 1 <= int(ans) <= len(voices):
                return voices[int(ans) - 1]
            for v in voices:
                if ans == v["voice_id"]:
                    return v
            print("Not found, try again.")

    a, b = pick("Host A"), pick("Host B")
    cfg["host_a"] = {"name": a["name"], "voice_id": a["voice_id"]}
    cfg["host_b"] = {"name": b["name"], "voice_id": b["voice_id"]}
    save_config(cfg)
    print(f"\nSaved to config.json: Host A = {a['name']}, Host B = {b['name']}")
    return True


def pick_model(key, cfg):
    if cfg.get("model_id"):
        return cfg["model_id"], 1.0
    try:
        models = api(key, "GET", "/v1/models", fatal=False)
    except RuntimeError:
        # Restricted keys may lack models_read; Eleven v3 is the documented default.
        return "eleven_v3", 1.0
    by_id = {m["model_id"]: m for m in models}
    for mid in PREFERRED_MODELS:
        m = by_id.get(mid)
        if m and m.get("can_do_text_to_speech", True):
            mult = (m.get("model_rates") or {}).get("character_cost_multiplier", 1.0)
            return mid, float(mult or 1.0)
    return "eleven_v3", 1.0


# ---------------------------------------------------------------- scripts

PAUSE_RE = re.compile(r"^\s*PAUSE\s*:\s*(\d+(?:\.\d+)?)\s*$", re.I)
HOST_RE = re.compile(r"^\s*[*_]*\s*HOST\s+([AB])\s*[*_]*\s*:\s*[*_]*\s*(.*)$", re.I)


def clean_md(text):
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)  # links
    text = re.sub(r"(\*\*|__|`)", "", text)
    text = re.sub(r"(?<!\w)[*_](\S[^*_]*?)[*_](?!\w)", r"\1", text)
    return re.sub(r"\s+", " ", text).strip()


def parse_script(path):
    raw = path.read_text(encoding="utf-8")
    meta = {}
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", raw, re.S)
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip().lower()] = v.strip().strip('"').strip("'")
        raw = raw[m.end():]

    turns, intro, title = [], [], None
    for line in raw.splitlines():
        pm = PAUSE_RE.match(line)
        hm = HOST_RE.match(line)
        if pm:
            turns.append(["P", pm.group(1)])  # silence, e.g. time to answer a quiz question
        elif hm:
            turns.append([hm.group(1).upper(), hm.group(2)])
        elif line.lstrip().startswith("#"):
            if title is None:
                title = line.lstrip("#").strip()
        elif turns and line.strip():
            turns[-1][1] += " " + line.strip()  # continuation of the previous turn
        elif not turns and line.strip():
            intro.append(line.strip())
    turns = [(s, t if s == "P" else clean_md(t)) for s, t in turns if clean_md(t)]

    num = meta.get("episode")
    if not num:
        nm = re.search(r"(\d+)", path.stem)
        num = nm.group(1) if nm else "0"
    return {
        "slug": path.stem,
        "title": meta.get("title") or title or path.stem.replace("-", " ").title(),
        "description": meta.get("description") or clean_md(" ".join(intro))
        or f"Episode {int(num)}",
        "episode": int(num),
        "turns": turns,
        # optional per-script overrides, e.g. for voice tests
        "voice_a": meta.get("host_a_voice"),
        "voice_b": meta.get("host_b_voice"),
        "stability": float(meta["stability"]) if "stability" in meta else None,
    }


def split_long(text, limit):
    parts, cur = [], ""
    for sent in re.split(r"(?<=[.!?…])\s+", text):
        while len(sent) > limit:  # pathological: no sentence breaks
            cut = sent.rfind(" ", 0, limit)
            cut = cut if cut > 0 else limit
            parts.append((cur + " " + sent[:cut]).strip() if cur else sent[:cut])
            cur, sent = "", sent[cut:].strip()
        if len(cur) + len(sent) + 1 > limit:
            parts.append(cur)
            cur = sent
        else:
            cur = (cur + " " + sent).strip()
    if cur:
        parts.append(cur)
    return parts


def spoken_chars(turns):
    return sum(len(t) for s, t in turns if s != "P")


def chunk_turns(turns, limit=CHUNK_LIMIT):
    pieces = []
    for spk, text in turns:
        for p in ([text] if spk == "P" else split_long(text, limit)):
            pieces.append((spk, p))
    chunks, cur, size = [], [], 0
    for spk, text in pieces:
        if spk == "P":  # a pause is its own silent segment between API requests
            if cur:
                chunks.append(cur)
            chunks.append([(spk, text)])
            cur, size = [], 0
            continue
        if cur and size + len(text) > limit:
            chunks.append(cur)
            cur, size = [], 0
        cur.append((spk, text))
        size += len(text)
    if cur:
        chunks.append(cur)
    return chunks


# ---------------------------------------------------------------- MP3 helpers

_BITRATES = {  # MPEG1 Layer III, MPEG2/2.5 Layer III (kbps)
    1: [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320],
    2: [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],
}
_RATES = {3: [44100, 48000, 32000], 2: [22050, 24000, 16000], 0: [11025, 12000, 8000]}


def strip_tags(data):
    if data[:3] == b"ID3" and len(data) > 10:
        size = (data[6] << 21) | (data[7] << 14) | (data[8] << 7) | data[9]
        data = data[10 + size:]
    if len(data) > 128 and data[-128:-125] == b"TAG":
        data = data[:-128]
    # Drop a leading Xing/Info frame: it states the length of this chunk only, which makes
    # players report the length of the first chunk for the whole joined episode.
    if len(data) > 4 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0 and \
            (b"Xing" in data[4:64] or b"Info" in data[4:64]):
        flen = _frame_len(data)
        if flen:
            data = data[flen:]
    return data


def _frame_len(data):
    ver, layer = (data[1] >> 3) & 3, (data[1] >> 1) & 3
    br_idx, sr_idx, pad = data[2] >> 4, (data[2] >> 2) & 3, (data[2] >> 1) & 1
    if ver == 1 or layer != 1 or br_idx in (0, 15) or sr_idx == 3:
        return 0
    mpeg1 = ver == 3
    br = _BITRATES[1 if mpeg1 else 2][br_idx] * 1000
    return (144 if mpeg1 else 72) * br // _RATES[ver][sr_idx] + pad


def mp3_duration(data):
    data = strip_tags(data)
    i, seconds, n = 0, 0.0, len(data)
    while i + 4 <= n:
        if data[i] != 0xFF or (data[i + 1] & 0xE0) != 0xE0:
            i += 1
            continue
        ver = (data[i + 1] >> 3) & 3
        layer = (data[i + 1] >> 1) & 3
        br_idx = data[i + 2] >> 4
        sr_idx = (data[i + 2] >> 2) & 3
        pad = (data[i + 2] >> 1) & 1
        if ver == 1 or layer != 1 or br_idx in (0, 15) or sr_idx == 3:
            i += 1
            continue
        mpeg1 = ver == 3
        br = _BITRATES[1 if mpeg1 else 2][br_idx] * 1000
        sr = _RATES[ver][sr_idx]
        samples = 1152 if mpeg1 else 576
        flen = (144 if mpeg1 else 72) * br // sr + pad
        seconds += samples / sr
        i += max(flen, 1)
    return seconds


def silence(seconds):
    # Silent MPEG-1 Layer III frames matching ElevenLabs' mp3_44100_128 mono output.
    frame = bytes([0xFF, 0xFB, 0x90, 0xC4]) + bytes(417 - 4)
    return frame * int(round(seconds * 44100 / 1152))


def hms(sec):
    sec = int(round(sec))
    return f"{sec // 3600:d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


# ---------------------------------------------------------------- generation

def generate_fish_per_turn(key, cfg, ep):
    """One request per spoken turn, with a short silence between turns so the hosts don't run on."""
    fc = cfg["fish"]
    voices = {"A": fc["voices"][0], "B": fc["voices"][1]}
    model, speed, gap = fc.get("model", "s2-pro"), fc.get("speed", 1.0), fc.get("turn_gap", 1.0)
    part_dir = PARTS_DIR / ep["slug"]
    part_dir.mkdir(parents=True, exist_ok=True)
    segments = []  # ("P", seconds) or (speaker, text)
    for spk, text in ep["turns"]:
        segments += [(spk, text)] if spk == "P" else [(spk, t) for t in split_long(text, CHUNK_LIMIT)]

    def part_path(i, spk, text):
        h = hashlib.sha1(json.dumps([voices[spk], text, model, speed]).encode()).hexdigest()[:12]
        return part_dir / f"t{i:04d}-{h}.mp3"

    jobs = [(i, s, t) for i, (s, t) in enumerate(segments) if s != "P"]
    todo = [j for j in jobs if not part_path(*j).exists()]
    print(f"    {len(jobs)} turns, {len(jobs) - len(todo)} cached", flush=True)

    def run(job):
        i, s, t = job
        part_path(i, s, t).write_bytes(fish_tts(key, t, voices[s], model, speed))

    with ThreadPoolExecutor(max_workers=4) as pool:
        for n, _ in enumerate(pool.map(run, todo), 1):
            if n % 20 == 0:
                print(f"    {n}/{len(todo)} turns generated", flush=True)

    audio, prev_speech = [], False
    for i, (s, t) in enumerate(segments):
        if s == "P":
            audio.append(silence(float(t)))
            prev_speech = False
            continue
        if prev_speech:
            audio.append(silence(gap))
        audio.append(strip_tags(part_path(i, s, t).read_bytes()))
        prev_speech = True
    out = EPISODES_DIR / f"{ep['slug']}.mp3"
    out.write_bytes(b"".join(audio))
    return out


def generate_episode(key, cfg, ep, model_id):
    if cfg.get("provider") == "fish" and cfg["fish"].get("per_turn"):
        return generate_fish_per_turn(key, cfg, ep)
    voice = {"A": ep.get("voice_a") or cfg["host_a"]["voice_id"],
             "B": ep.get("voice_b") or cfg["host_b"]["voice_id"]}
    stability = ep["stability"] if ep.get("stability") is not None else cfg.get("stability", 0.5)
    chunks = chunk_turns(ep["turns"])
    part_dir = PARTS_DIR / ep["slug"]
    part_dir.mkdir(parents=True, exist_ok=True)
    audio = []
    for idx, chunk in enumerate(chunks, 1):
        if chunk[0][0] == "P":
            audio.append(silence(float(chunk[0][1])))
            continue
        if cfg.get("provider") == "fish":
            fc = cfg["fish"]
            body = {"provider": "fish", "text": fish_text(chunk), "voices": fc["voices"],
                    "model": fc.get("model", "s2-pro"), "speed": fc.get("speed", 1.0)}
        else:
            body = {
                "inputs": [{"text": t, "voice_id": voice[s]} for s, t in chunk],
                "model_id": model_id,
                "settings": {"stability": stability},
            }
        # Cache each chunk by its content so an interrupted run resumes for free.
        h = hashlib.sha1(json.dumps(body, sort_keys=True).encode()).hexdigest()[:12]
        part = part_dir / f"{idx:03d}-{h}.mp3"
        if part.exists():
            print(f"    chunk {idx}/{len(chunks)} cached")
        else:
            print(f"    chunk {idx}/{len(chunks)} ({spoken_chars(chunk)} chars)...",
                  flush=True)
            if body.get("provider") == "fish":
                data = fish_tts(key, body["text"], body["voices"], body["model"], body["speed"])
            else:
                data, _ = api(key, "POST", "/v1/text-to-dialogue", body=body,
                              query={"output_format": OUTPUT_FORMAT}, raw=True)
            part.write_bytes(data)
        audio.append(strip_tags(part.read_bytes()))
    out = EPISODES_DIR / f"{ep['slug']}.mp3"
    out.write_bytes(b"".join(audio))
    return out


# ---------------------------------------------------------------- feed

def write_feed(cfg, episodes, manifest):
    base = cfg["base_url"].rstrip("/")
    if not base:
        print("\nNote: base_url is empty in config.json, so feed links are relative. "
              "Set it to your GitHub Pages URL.")
    e = lambda s: html.escape(str(s), quote=True)
    items = []
    for ep in sorted(episodes, key=lambda x: x["episode"], reverse=True):
        mp3 = EPISODES_DIR / f"{ep['slug']}.mp3"
        if not mp3.exists():
            continue
        info = manifest.setdefault(ep["slug"], {})
        info.setdefault("published", email.utils.formatdate(mp3.stat().st_mtime, usegmt=True))
        if info.get("size") != mp3.stat().st_size or "duration" not in info:
            info["size"] = mp3.stat().st_size
            info["duration"] = mp3_duration(mp3.read_bytes())
        url = f"{base}/episodes/{urllib.parse.quote(mp3.name)}"
        items.append(f"""    <item>
      <title>{e(ep['title'])}</title>
      <description>{e(ep['description'])}</description>
      <itunes:summary>{e(ep['description'])}</itunes:summary>
      <itunes:episode>{ep['episode']}</itunes:episode>
      <itunes:episodeType>full</itunes:episodeType>
      <itunes:duration>{hms(info['duration'])}</itunes:duration>
      <enclosure url="{e(url)}" length="{info['size']}" type="audio/mpeg"/>
      <guid isPermaLink="false">{e(ep['slug'])}</guid>
      <pubDate>{info['published']}</pubDate>
    </item>""")
    image = ""
    for name in ("cover.jpg", "cover.png"):
        if (FEED_PATH.parent / name).exists():
            image = f'\n    <itunes:image href="{e(base + "/" + name)}"/>'
    feed = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>{e(cfg['podcast_title'])}</title>
    <link>{e(base or '.')}</link>
    <atom:link href="{e(base + '/feed.xml')}" rel="self" type="application/rss+xml"/>
    <description>{e(cfg['podcast_description'])}</description>
    <language>{e(cfg['language'])}</language>
    <itunes:author>{e(cfg['author'])}</itunes:author>
    <itunes:explicit>false</itunes:explicit>
    <itunes:type>episodic</itunes:type>
    <itunes:block>Yes</itunes:block>{image}
{chr(10).join(items)}
  </channel>
</rss>
"""
    FEED_PATH.write_text(feed, encoding="utf-8")
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2) + "\n")
    (ROOT / ".nojekyll").touch()
    return len(items)


# ---------------------------------------------------------------- main

def publish():
    if not (ROOT / ".git").exists():
        sys.exit("Not a git repository yet; see README for the one-time GitHub Pages setup.")
    run = lambda *a: subprocess.run(["git", *a], cwd=ROOT, check=True)
    run("add", "-A")
    if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT).returncode:
        run("commit", "-m", f"Update podcast feed {time.strftime('%Y-%m-%d %H:%M')}")
    run("push")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--voices", action="store_true", help="list voices and choose hosts")
    ap.add_argument("--estimate", action="store_true", help="only show the cost estimate")
    ap.add_argument("--yes", "-y", action="store_true", help="don't ask for confirmation")
    ap.add_argument("--feed-only", action="store_true", help="skip audio, just rebuild feed.xml")
    ap.add_argument("--subject", metavar="DIR", help="build the show in this subfolder (own scripts, episodes, config, feed)")
    ap.add_argument("--only", nargs="+", metavar="SLUG", help="only generate these episodes (script names without .md)")
    ap.add_argument("--publish", action="store_true", help="git commit + push after building")
    args = ap.parse_args()
    if args.subject:
        global SCRIPTS_DIR, EPISODES_DIR, PARTS_DIR, CONFIG_PATH, MANIFEST_PATH, FEED_PATH
        base = ROOT / args.subject
        SCRIPTS_DIR, EPISODES_DIR, PARTS_DIR = base / "scripts", base / "episodes", base / ".parts"
        CONFIG_PATH, MANIFEST_PATH, FEED_PATH = base / "config.json", base / "episodes" / "manifest.json", base / "feed.xml"

    cfg = load_config()
    if not CONFIG_PATH.exists():
        save_config(cfg)

    if args.voices:
        choose_voices(load_env(), cfg)
        return

    episodes = [parse_script(p) for p in sorted(SCRIPTS_DIR.glob("*.md"))]
    if not episodes:
        sys.exit("No scripts found in scripts/*.md")
    for ep in episodes:
        if not ep["turns"]:
            print(f"Warning: {ep['slug']} has no 'HOST A:' / 'HOST B:' lines; skipping.")
    episodes = [ep for ep in episodes if ep["turns"]]
    EPISODES_DIR.mkdir(exist_ok=True)
    manifest = json.loads(MANIFEST_PATH.read_text()) if MANIFEST_PATH.exists() else {}

    if not args.feed_only:
        provider = cfg.get("provider", "elevenlabs")
        key = load_env(provider)
        if provider != "fish" and not (cfg["host_a"]["voice_id"] and cfg["host_b"]["voice_id"]):
            if not choose_voices(key, cfg):
                return

        todo = [ep for ep in episodes if not (EPISODES_DIR / f"{ep['slug']}.mp3").exists()]
        if args.only:
            todo = [ep for ep in todo if ep["slug"] in args.only]
        if todo:
            if provider == "fish":
                fc = cfg["fish"]
                model_id, mult = fc.get("model", "s2-pro"), None
                print(f"\nFish Audio {model_id}   Host A: {fc.get('names', ['', ''])[0]}"
                      f"   Host B: {fc.get('names', ['', ''])[1]}   speed {fc.get('speed', 1.0)}\n")
            else:
                model_id, mult = pick_model(key, cfg)
                print(f"\nModel: {model_id}   Host A: {cfg['host_a']['name'] or cfg['host_a']['voice_id']}"
                      f"   Host B: {cfg['host_b']['name'] or cfg['host_b']['voice_id']}\n")
            total = 0
            for ep in todo:
                chars = spoken_chars(ep["turns"])
                total += chars
                print(f"  {ep['slug']:<32} {chars:>7,} chars  {sum(c[0][0] != 'P' for c in chunk_turns(ep['turns'])):>3} chunks")
            if provider == "fish":
                nbytes = sum(len(fish_text(c).encode()) for ep in todo
                             for c in chunk_turns(ep["turns"]) if c[0][0] != "P")
                print(f"\n  Total: {total:,} characters ({nbytes:,} bytes)  ≈ "
                      f"${nbytes * FISH_USD_PER_MB / 1e6:.2f} at ${FISH_USD_PER_MB:g} per million bytes")
            else:
                credits = int(total * mult)
                print(f"\n  Total: {total:,} characters  ≈ {credits:,} credits "
                      f"(at {mult:g} credit/char)")
            if provider != "fish":
              try:
                sub = api(key, "GET", "/v1/user/subscription", fatal=False)
                left = sub["character_limit"] - sub["character_count"]
                print(f"  Remaining in your plan: {left:,} credits ({sub.get('tier', '')})")
              except RuntimeError:
                print("  (Remaining credits unknown: this API key can't read your subscription.)")
            if args.estimate:
                return
            if not args.yes:
                if not sys.stdin.isatty() or input("\nGenerate audio? [y/N] ").strip().lower() != "y":
                    print("Cancelled; no audio generated.")
                    return
            for ep in todo:
                print(f"\n  Generating {ep['slug']}")
                out = generate_episode(key, cfg, ep, model_id)
                print(f"    -> {out.relative_to(ROOT)} ({hms(mp3_duration(out.read_bytes()))})")
        else:
            print("All episodes already have an MP3.")

    n = write_feed(cfg, episodes, manifest)
    print(f"\nWrote feed.xml with {n} episode(s).")
    if cfg["base_url"]:
        print(f"Feed URL: {cfg['base_url'].rstrip('/')}/feed.xml")
    if args.publish:
        publish()


if __name__ == "__main__":
    main()
