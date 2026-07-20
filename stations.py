"""Stations data, URL parsing, and settings persistence."""
import json

STATIONS_FILE = '/apps/andreacampanella_BadgeRadio/stations.json'
SETTINGS_FILE = '/apps/andreacampanella_BadgeRadio/settings.json'

DEFAULT_STATIONS = [
    {"name": "Rainwave All",     "url": "http://allstream.rainwave.cc:8000/all.mp3"},
    {"name": "Rainwave Game",    "url": "http://allstream.rainwave.cc:8000/game.mp3"},
    {"name": "Rainwave OCRemix", "url": "http://allstream.rainwave.cc:8000/ocremix.mp3"},
    {"name": "Classic FM",       "url": "http://media-ice.musicradio.com/ClassicFMMP3"},
    {"name": "SomaFM Groove",    "url": "http://ice1.somafm.com/groovesalad-128-mp3"},
    {"name": "SomaFM Drone",     "url": "http://ice1.somafm.com/dronezone-128-mp3"},
    {"name": "SomaFM DefCon",    "url": "http://ice1.somafm.com/defcon-128-mp3"},
]


def parse_url(url):
    """Return (host, port, path). HTTPS is rejected up-front — the app has
    no TLS stack."""
    if url.startswith('https://'):
        raise ValueError("HTTPS not supported (use http://)")
    if not url.startswith('http://'):
        raise ValueError("only http:// URLs supported")
    rest = url[7:]
    slash = rest.find('/')
    hostport = rest if slash < 0 else rest[:slash]
    path = '/' if slash < 0 else rest[slash:]
    if ':' in hostport:
        host, port = hostport.rsplit(':', 1)
        port = int(port)
    else:
        host, port = hostport, 80
    return host, port, path


def load_stations():
    """Return list of (name, url) tuples. Falls back to DEFAULT_STATIONS on
    parse error or missing file."""
    try:
        with open(STATIONS_FILE) as f:
            data = json.load(f)
        if isinstance(data, list) and data:
            return [(s['name'], s['url'])
                    for s in data if 'name' in s and 'url' in s]
    except Exception:
        pass
    return [(s['name'], s['url']) for s in DEFAULT_STATIONS]


def load_settings():
    try:
        with open(SETTINGS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_settings(d):
    try:
        with open(SETTINGS_FILE, 'w') as f:
            json.dump(d, f)
        return True
    except Exception:
        return False
