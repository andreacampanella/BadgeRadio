# Tildagon Internet Radio

An MP3 internet-radio app for the EMF 2024 Tildagon badge. iPod-Classic
inspired UI with themes, live volume control, swappable station list.

![controls: ▲▼ stations · ◀▶ volume · A play/stop · B exit · ▲▼ hold 2s themes]

## What it does

- Streams plain HTTP MP3 (Icecast / Shoutcast) over WiFi.
- Decodes with [minimp3](https://github.com/lieff/minimp3) (public domain),
  compiled into the firmware as a user C module — see `drivers/mp3/`.
- Plays via I²S to an external DAC (PCM5102A in this build) on a
  hexpansion port. See [hardware wiring details](I2S_README.md) for more.
- Stations are user-editable via a JSON file.
- Themes, volume, last-played station persist across reboots.

## Installing the app

```sh
mpremote mkdir :/apps/radio || true
mpremote cp app.py        :/apps/radio/app.py
mpremote cp __init__.py   :/apps/radio/__init__.py
mpremote cp metadata.json :/apps/radio/metadata.json
mpremote cp stations.json :/apps/radio/stations.json
mpremote reset
```
or just install from the badge App store.

## Controls

| Button         | Action                                     |
|----------------|--------------------------------------------|
| **▲ / ▼**      | Previous / next station                    |
| **◀ / ▶**      | Volume −5 % / +5 %                         |
| **A** (CONFIRM)| Play / stop                                |
| **B** (CANCEL) | Exit to launcher                           |
| **▲ + ▼ hold 2 s** | Cycle visual theme                     |

Holding ▲ and ▼ together shows a progress bar at the bottom of the screen;
release before it fills to abort.

## Stations

Stations are read from `/apps/radio/stations.json`. Default ships with
Rainwave's five mounts, Classic FM, and a few SomaFM channels. Edit the file
and reset the badge to apply.

```json
[
  {"name": "Rainwave All",  "url": "http://allstream.rainwave.cc:8000/all.mp3"},
  {"name": "Classic FM",    "url": "http://media-ice.musicradio.com/ClassicFMMP3"},
  {"name": "SomaFM Groove", "url": "http://ice1.somafm.com/groovesalad-128-mp3"}
]
```

**Limits:**

- HTTP only — no HTTPS / TLS.
- Direct stream URLs only — `.m3u` / `.pls` playlist files won't auto-resolve.
  If a station gives you a playlist link, open it in a browser/text editor and
  grab the actual stream URL inside.
- MP3 codec only. AAC, FLAC and Opus streams won't decode (libhelix's AAC
  decoder could be added as a separate user C module if needed).

If `stations.json` is missing or malformed, the app falls back to a built-in
default list so it still works out of the box.

## Themes

Hold ▲ + ▼ for 2 s to cycle. Themes:

| Theme        | Vibe                                     |
|--------------|------------------------------------------|
| iPod LCD     | Cream-grey background, black text (default) |
| Solarized    | base03/base02 dark with yellow accent    |
| Tildagon     | Pale-/mid-/dark-green from the badge palette |
| B&W Inverted | Pure black / white terminal style        |

The chosen theme persists in `/apps/radio/settings.json`.

## Settings

`/apps/radio/settings.json` is auto-written when you change station, volume,
or theme. It looks like:

```json
{"station_idx": 4, "volume": 70, "theme_idx": 1}
```

Delete the file to reset to defaults.