# Course podcast

`scripts/*.md` (two-host dialogue: `HOST A: ...` / `HOST B: ...`) → `episodes/*.mp3` + `feed.xml`.

```
python3 build.py              # estimate cost, confirm, generate missing MP3s, write feed.xml
python3 build.py --voices     # list ElevenLabs voices and pick Host A / Host B
python3 build.py --estimate   # only show characters and estimated credits
python3 build.py --publish    # build, then git commit + push (GitHub Pages)
```

- `.env` holds `ELEVENLABS_API_KEY=...` and is git-ignored.
- `config.json` holds the voices, podcast title/description and `base_url` (the GitHub Pages URL).
- Audio uses ElevenLabs Text to Dialogue, in chunks of at most 2,000 characters, joined into one MP3 per episode. Finished chunks are cached in `.parts/`, so an interrupted run resumes without paying twice.
- Episodes that already have an MP3 are skipped. Delete the MP3 to regenerate it.
- Optional script front matter: `title:`, `description:`, `episode:`. Otherwise the title is the first `#` heading, the description is any text before the first host line, and the episode number comes from the filename.
- The feed sets `itunes:block` so it isn't listed in podcast directories, but anyone with the URL can reach it.
