"""Internet streaming radio — Tildagon app, player runs in its own _thread."""
import asyncio
import socket
import network
import json
import time
import gc
import _thread
import micropython

# Load the bundled minimp3 natmod (/apps/radio/mp3.mpy)
import sys
if '/apps/radio' not in sys.path:
    sys.path.insert(0, '/apps/radio')
import mp3 as _mp3_natmod

class _Mp3Adapter:
    def init(self):
        _mp3_natmod.op = 0
        return _mp3_natmod.process() == 1
    def decode(self, buf):
        _mp3_natmod.op = 1
        _mp3_natmod.input = buf
        return _mp3_natmod.process()
    def deinit(self):
        pass

mp3 = _Mp3Adapter()

from machine import I2S, Pin
import app
from events.input import Buttons, BUTTON_TYPES

I2S_ID  = 0
PIN_BCK = 35
PIN_LCK = 36
PIN_DIN = 37

STATIONS_FILE = '/apps/radio/stations.json'
SETTINGS_FILE = '/apps/radio/settings.json'

THEME_COMBO_MS = 2000

# Give the player thread plenty of stack — minimp3 decode frames + natmod call
# frames need more than the ESP32 default of 4 KB.
PLAYER_THREAD_STACK = 16 * 1024

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
    if not url.startswith('http://'):
        raise ValueError("only http:// supported")
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
    try:
        with open(STATIONS_FILE) as f:
            data = json.load(f)
        if isinstance(data, list) and data:
            return [(s['name'], s['url']) for s in data if 'name' in s and 'url' in s]
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
    except Exception:
        pass


@micropython.viper
def _apply_volume(buf: ptr8, n_bytes: int, vol_num: int):
    p = ptr16(buf)
    n = n_bytes >> 1
    i = 0
    while i < n:
        s = int(p[i])
        if s >= 32768:
            s -= 65536
        s = (s * vol_num) >> 8
        if s > 32767:
            s = 32767
        elif s < -32768:
            s = -32768
        p[i] = s & 0xFFFF
        i += 1


THEMES = [
    {
        'name':     'iPod LCD',
        'BG':       (0.84, 0.84, 0.79),
        'BAR_BG':   (0.74, 0.74, 0.69),
        'LINE':     (0.18, 0.18, 0.16),
        'INK':      (0.10, 0.10, 0.08),
        'DIM_INK':  (0.40, 0.40, 0.35),
        'HILITE':   (0.10, 0.10, 0.08),
        'HILITE_T': (0.94, 0.94, 0.90),
    },
    {
        'name':     'Solarized',
        'BG':       (0.000, 0.169, 0.212),
        'BAR_BG':   (0.027, 0.212, 0.259),
        'LINE':     (0.345, 0.431, 0.459),
        'INK':      (0.514, 0.580, 0.588),
        'DIM_INK':  (0.345, 0.431, 0.459),
        'HILITE':   (0.710, 0.537, 0.000),
        'HILITE_T': (0.000, 0.169, 0.212),
    },
    {
        'name':     'Tildagon',
        'BG':       (0.129, 0.188, 0.094),
        'BAR_BG':   (0.320, 0.512, 0.160),
        'LINE':     (0.684, 0.785, 0.266),
        'INK':      (0.684, 0.785, 0.266),
        'DIM_INK':  (0.500, 0.640, 0.220),
        'HILITE':   (0.684, 0.785, 0.266),
        'HILITE_T': (0.129, 0.188, 0.094),
    },
    {
        'name':     'B&W Inverted',
        'BG':       (0.00, 0.00, 0.00),
        'BAR_BG':   (0.15, 0.15, 0.15),
        'LINE':     (1.00, 1.00, 1.00),
        'INK':      (1.00, 1.00, 1.00),
        'DIM_INK':  (0.55, 0.55, 0.55),
        'HILITE':   (1.00, 1.00, 1.00),
        'HILITE_T': (0.00, 0.00, 0.00),
    },
]


TITLE_LABEL = {
    'idle':         'Stopped',
    'stopped':      'Stopped',
    'playing':      'Playing',
    'connecting':   'Connecting',
    'reconnecting': 'Reconnecting',
    'error':        'Error',
}


class BadgeRadio(app.App):
    def __init__(self):
        super().__init__()
        self.button_states = Buttons(self)
        self.stations = load_stations()
        s = load_settings()
        self.station_idx = min(s.get('station_idx', 0), max(0, len(self.stations) - 1))
        self.volume = max(0, min(100, s.get('volume', 70)))
        self.theme_idx = s.get('theme_idx', 0) % len(THEMES)
        self.status = 'idle'
        self.frames = 0
        self.rate = 0
        self.chans = 0
        self.error = ''
        # Thread control flags
        self._thread_running = False
        self._thread_stop = False
        # UI button-edge tracking
        self._prev = {'CONFIRM': False, 'CANCEL': False, 'UP': False,
                      'DOWN': False, 'LEFT': False, 'RIGHT': False}
        self._draw_acc = 0
        self._combo_ms = 0
        self._combo_consumed = False
        self._theme_toast_ms = 0

    def _save(self):
        save_settings({
            'station_idx': self.station_idx,
            'volume':      self.volume,
            'theme_idx':   self.theme_idx,
        })

    def _vol_num(self):
        return (self.volume * 256) // 100

    def _change_station(self, delta_idx):
        self.station_idx = (self.station_idx + delta_idx) % len(self.stations)
        self._save()
        if self._thread_running:
            # Signal thread to stop and restart with new station
            self._thread_stop = True
            # Start a thread that waits then starts a new one
            asyncio.create_task(self._restart_after_stop())

    async def _restart_after_stop(self):
        while self._thread_running:
            await asyncio.sleep_ms(50)
        self._start_player()

    def _cycle_theme(self):
        self.theme_idx = (self.theme_idx + 1) % len(THEMES)
        self._theme_toast_ms = 1500
        self._save()

    @property
    def theme(self):
        return THEMES[self.theme_idx]

    # ----------------------------------------------------------------
    # Player runs in its own preemptive _thread, isolated from asyncio.
    # ----------------------------------------------------------------
    def _player_thread(self):
        print('THREAD: start')
        self._thread_running = True
        audio = None
        cur_rate = cur_ch = 0
        try:
            while not self._thread_stop:
                try:
                    self.status = 'connecting'
                    name, url = self.stations[self.station_idx]
                    host, port, path = parse_url(url)
                    print('THREAD: connecting to', host, port, path)
                    ai = socket.getaddrinfo(host, port)[0][-1]
                    sock = socket.socket()
                    sock.connect(ai)
                    req = ('GET {} HTTP/1.0\r\nHost: {}\r\n'
                           'User-Agent: tildagon/radio\r\nIcy-MetaData: 0\r\n\r\n'
                           ).format(path, host).encode()
                    sock.send(req)
                    buf = b''
                    while b'\r\n\r\n' not in buf:
                        chunk = sock.recv(256)
                        if not chunk:
                            raise OSError("server closed before headers")
                        buf += chunk
                    _, buf = buf.split(b'\r\n\r\n', 1)
                    while len(buf) < 4096:
                        chunk = sock.recv(2048)
                        if not chunk:
                            raise OSError("no data")
                        buf += chunk
                    print('THREAD: stream open, buf=', len(buf))
                    self.status = 'playing'
                    self.frames = 0
                    while not self._thread_stop:
                        if len(buf) < 2048:
                            chunk = sock.recv(2048)
                            if not chunk:
                                break
                            buf += chunk
                        pcm, consumed, ch, hz, samp = mp3.decode(buf)
                        buf = buf[consumed:]
                        if not pcm:
                            continue
                        if hz != cur_rate or ch != cur_ch:
                            if audio is not None:
                                audio.deinit()
                            audio = I2S(
                                I2S_ID,
                                sck=Pin(PIN_BCK), ws=Pin(PIN_LCK), sd=Pin(PIN_DIN),
                                mode=I2S.TX, bits=16,
                                format=I2S.STEREO if ch == 2 else I2S.MONO,
                                rate=hz, ibuf=80000,
                            )
                            cur_rate, cur_ch = hz, ch
                            self.rate, self.chans = hz, ch
                            print('THREAD: I2S', hz, ch)
                        vn = self._vol_num()
                        if vn != 256:
                            pcm_buf = bytearray(pcm)
                            _apply_volume(pcm_buf, len(pcm_buf), vn)
                            audio.write(pcm_buf)
                        else:
                            audio.write(pcm)
                        self.frames += 1
                        if self.frames in (1, 5, 50, 500):
                            print('THREAD: frames=', self.frames)
                    try:
                        sock.close()
                    except Exception:
                        pass
                    if self._thread_stop:
                        break
                    self.status = 'reconnecting'
                    time.sleep_ms(500)
                except Exception as e:
                    print('THREAD: exception:', repr(e))
                    self.error = repr(e)
                    self.status = 'error'
                    time.sleep_ms(2000)
        finally:
            if audio is not None:
                try:
                    audio.deinit()
                except Exception:
                    pass
            self.status = 'stopped'
            self._thread_running = False
            print('THREAD: exit')

    def _start_player(self):
        if self._thread_running:
            return
        w = network.WLAN(network.STA_IF)
        if not w.isconnected():
            self.error = 'WiFi not connected'
            self.status = 'error'
            return
        try:
            mp3.init()
        except Exception as e:
            self.error = repr(e)
            self.status = 'error'
            return
        self._thread_stop = False
        try:
            _thread.stack_size(PLAYER_THREAD_STACK)
        except Exception:
            pass
        _thread.start_new_thread(self._player_thread, ())

    def _stop_player(self):
        self._thread_stop = True
        # Don't block; thread will clean up itself.

    async def _shutdown_and_minimise(self):
        self._stop_player()
        # Give the thread a moment to exit cleanly before minimising
        for _ in range(20):
            if not self._thread_running:
                break
            await asyncio.sleep_ms(50)
        self.minimise()

    # --- App lifecycle (asyncio side) -------------------------------
    def update(self, delta):
        bs = self.button_states
        up_now      = bs.get(BUTTON_TYPES['UP'])
        down_now    = bs.get(BUTTON_TYPES['DOWN'])
        left_now    = bs.get(BUTTON_TYPES['LEFT'])
        right_now   = bs.get(BUTTON_TYPES['RIGHT'])
        confirm_now = bs.get(BUTTON_TYPES['CONFIRM'])
        cancel_now  = bs.get(BUTTON_TYPES['CANCEL'])

        if cancel_now and not self._prev['CANCEL']:
            self._prev['CANCEL'] = cancel_now
            asyncio.create_task(self._shutdown_and_minimise())
            return True
        self._prev['CANCEL'] = cancel_now

        if up_now and down_now:
            self._combo_ms += delta
            if (not self._combo_consumed) and self._combo_ms >= THEME_COMBO_MS:
                self._cycle_theme()
                self._combo_consumed = True
            self._prev['UP'] = up_now
            self._prev['DOWN'] = down_now
            return True

        if not (up_now and down_now):
            self._combo_ms = 0
            self._combo_consumed = False

        if up_now and not self._prev['UP'] and not down_now:
            self._prev['UP'] = up_now
            self._change_station(-1)
            return True
        if down_now and not self._prev['DOWN'] and not up_now:
            self._prev['DOWN'] = down_now
            self._change_station(+1)
            return True
        self._prev['UP'] = up_now
        self._prev['DOWN'] = down_now

        if left_now and not self._prev['LEFT']:
            self._prev['LEFT'] = left_now
            self.volume = max(0, self.volume - 5); self._save()
            return True
        self._prev['LEFT'] = left_now
        if right_now and not self._prev['RIGHT']:
            self._prev['RIGHT'] = right_now
            self.volume = min(100, self.volume + 5); self._save()
            return True
        self._prev['RIGHT'] = right_now

        if confirm_now and not self._prev['CONFIRM']:
            self._prev['CONFIRM'] = confirm_now
            if not self._thread_running:
                self._start_player()
            else:
                self._stop_player()
            return True
        self._prev['CONFIRM'] = confirm_now

        if self._theme_toast_ms > 0:
            self._theme_toast_ms = max(0, self._theme_toast_ms - delta)

        self._draw_acc += delta
        if self._draw_acc >= 250:
            self._draw_acc = 0
            return True
        return False

    @staticmethod
    def _fit_text(ctx, text, max_w, base_size, min_size=12):
        size = base_size
        ctx.font_size = size
        while size > min_size and int(ctx.text_width(text)) > max_w:
            size -= 1
            ctx.font_size = size
        return size

    def draw(self, ctx):
        t = self.theme
        ctx.save()
        ctx.rgb(*t['BG']).rectangle(-120, -120, 240, 240).fill()
        ctx.rgb(*t['BAR_BG']).rectangle(-120, -100, 240, 32).fill()
        ctx.rgb(*t['LINE']).rectangle(-120, -68, 240, 1).fill()
        ctx.text_baseline = ctx.MIDDLE
        ctx.text_align = ctx.LEFT
        ctx.font_size = 22
        glyph = '\u25B6' if self.status == 'playing' else '\u25A0'
        ctx.rgb(*t['INK']).move_to(-46, -84).text(glyph)
        ctx.text_align = ctx.CENTER
        ctx.font_size = 22
        ctx.rgb(*t['INK']).move_to(0, -84).text(TITLE_LABEL.get(self.status, 'Radio'))
        n = len(self.stations)
        prev_name = self.stations[(self.station_idx - 1) % n][0]
        cur_name  = self.stations[self.station_idx][0]
        next_name = self.stations[(self.station_idx + 1) % n][0]
        ctx.text_align = ctx.CENTER
        self._fit_text(ctx, prev_name, max_w=220, base_size=22, min_size=14)
        ctx.rgb(*t['DIM_INK']).move_to(0, -38).text(prev_name)
        ctx.rgb(*t['HILITE']).rectangle(-120, -18, 240, 36).fill()
        self._fit_text(ctx, cur_name, max_w=220, base_size=26, min_size=14)
        ctx.rgb(*t['HILITE_T']).move_to(0, 0).text(cur_name)
        self._fit_text(ctx, next_name, max_w=220, base_size=22, min_size=14)
        ctx.rgb(*t['DIM_INK']).move_to(0, 30).text(next_name)
        bar_x = -70; bar_y = 58; bar_w = 140; bar_h = 10
        ctx.rgb(*t['LINE']).rectangle(bar_x - 1, bar_y - 1, bar_w + 2, bar_h + 2).stroke()
        fill_w = (bar_w * self.volume) // 100
        ctx.rgb(*t['INK']).rectangle(bar_x, bar_y, fill_w, bar_h).fill()
        ctx.font_size = 12
        ctx.text_align = ctx.LEFT
        ctx.rgb(*t['INK']).move_to(-100, 63).text("VOL")
        ctx.text_align = ctx.RIGHT
        ctx.rgb(*t['INK']).move_to(100, 63).text("{}%".format(self.volume))
        ctx.text_align = ctx.CENTER
        ctx.font_size = 14
        if self._combo_ms > 0 and not self._combo_consumed:
            pw = 100
            ctx.rgb(*t['LINE']).rectangle(-pw // 2 - 1, 79, pw + 2, 10).stroke()
            done = (pw * self._combo_ms) // THEME_COMBO_MS
            if done > pw: done = pw
            ctx.rgb(*t['INK']).rectangle(-pw // 2, 80, done, 8).fill()
            ctx.font_size = 10
            ctx.rgb(*t['DIM_INK']).move_to(0, 96).text("hold ▲▼ for theme")
        elif self._theme_toast_ms > 0:
            ctx.rgb(*t['INK']).move_to(0, 88).text("Theme: " + t['name'])
        elif self.status == 'error' and self.error:
            ctx.rgb(*t['INK']).move_to(0, 88).text(self.error[:22])
        elif self.rate:
            ctx.rgb(*t['DIM_INK']).move_to(0, 88).text(
                "{} Hz · {} ch".format(self.rate, self.chans))
        if not (self._combo_ms > 0 and not self._combo_consumed):
            ctx.font_size = 11
            ctx.rgb(*t['DIM_INK']).move_to(0, 105).text("▲▼ stn  ◀▶ vol")
        ctx.restore()
        self.draw_overlays(ctx)
