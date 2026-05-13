"""Internet streaming radio — Tildagon app.

Uses the zero-alloc natmod protocol (mp3.mpy v2, op=2).

Set DEBUG = False at the top to silence the diagnostics.
"""
import asyncio
import socket
import network
import json
import time
import gc
import struct
import _thread
import micropython


DEBUG = False

# Frames between gc.collect() calls. ~64 frames ≈ 1.7 s of audio at 44.1 kHz.
# gc.collect on ESP32 is ~10-30 ms; I2S DMA buffers ~1 s of audio, so the
# pause is inaudible. Without this, immutable-bytes churn from buf slicing
# fills the heap in seconds.
GC_EVERY_N_FRAMES = 64

# Player thread stack. minimp3's decode call chain plus the MicroPython
# interpreter frames + natmod call frame need substantial stack. 16 KB
# overflows and corrupts the natmod's IRAM text region, causing
# IllegalInstruction panics. 32 KB has comfortable headroom.
PLAYER_THREAD_STACK = 32 * 1024


def dbg(tag, *args):
    if DEBUG:
        try:
            print('[{}]'.format(tag), *args)
        except Exception:
            pass


# --- Load the bundled minimp3 natmod ---------------------------------------
import sys
if '/apps/andreacampanella_BadgeRadio' not in sys.path:
    sys.path.insert(0, '/apps/andreacampanella_BadgeRadio')
import mp3 as _mp3_natmod

# 1152 samples/frame * 2 channels * 2 bytes/sample = 4608 max
_MAX_PCM_BYTES = 1152 * 2 * 2

class _Mp3Adapter:
    """Adapter around the natmod's op-based protocol.

    Allocates output + info bytearrays once. Every decode call writes into
    those same buffers — the natmod never allocates per frame.
    """
    def __init__(self):
        self._output = bytearray(_MAX_PCM_BYTES)
        self._info   = bytearray(20)
        _mp3_natmod.output = self._output
        _mp3_natmod.info   = self._info
        self._output_mv = memoryview(self._output)
        dbg('DEC', 'adapter ready: output=', len(self._output),
            'info=', len(self._info))

    def init(self):
        _mp3_natmod.op = 0
        r = _mp3_natmod.process()
        dbg('DEC', 'init -> ', r)
        return r == 1

    def decode(self, buf):
        _mp3_natmod.op = 2
        _mp3_natmod.input = buf
        _mp3_natmod.process()
        fb, ch, hz, samp, pcm_bytes = struct.unpack_from('<iiiii', self._info, 0)
        return (self._output_mv[:pcm_bytes], fb, ch, hz, samp)

    @property
    def output_buf(self):
        return self._output

    def deinit(self):
        pass

mp3 = _Mp3Adapter()


from machine import I2S, Pin
import app
from events.input import Buttons, BUTTON_TYPES
from system.hexpansion.config import HexpansionConfig

I2S_ID  = 0
DEFAULT_PORT = 2
VALID_PORTS = (1, 2, 3, 4, 5, 6)

STATIONS_FILE = '/apps/andreacampanella_BadgeRadio/stations.json'
SETTINGS_FILE = '/apps/andreacampanella_BadgeRadio/settings.json'

SETTINGS_COMBO_MS = 1500

ABOUT_LINES = [
    "BadgeRadio",
    "v3.0.0",
    "",
    "You will need a suitable",
    "pcm5012 expansion to use this",
    "HMU on social @emuboy",
    "or check github for info",
    "github/andreacampanella/BadgeRadio",
]


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
            out = [(s['name'], s['url']) for s in data if 'name' in s and 'url' in s]
            dbg('SET', 'stations loaded:', len(out), 'entries')
            return out
    except Exception as e:
        dbg('SET', 'load_stations fallback:', repr(e))
    return [(s['name'], s['url']) for s in DEFAULT_STATIONS]


def load_settings():
    try:
        with open(SETTINGS_FILE) as f:
            s = json.load(f)
            dbg('SET', 'settings loaded:', s)
            return s
    except Exception as e:
        dbg('SET', 'no settings yet:', repr(e))
        return {}


def save_settings(d):
    try:
        with open(SETTINGS_FILE, 'w') as f:
            json.dump(d, f)
        dbg('SET', 'settings saved:', d)
    except Exception as e:
        dbg('SET', 'save failed:', repr(e))


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


SETTINGS_ITEMS = [
    ('Theme',  'theme_idx'),
    ('Port',   'port'),
    ('About',  None),
]


class BadgeRadio(app.App):
    def __init__(self):
        super().__init__()
        dbg('APP', '__init__: mem_free=', gc.mem_free())
        try:
            from system.eventbus import eventbus
            from system.patterndisplay.events import PatternDisable
            eventbus.emit(PatternDisable())
            dbg('APP', 'PatternDisable emitted')
        except Exception as e:
            dbg('APP', 'PatternDisable failed (ok):', repr(e))

        self.button_states = Buttons(self)
        self.stations = load_stations()
        s = load_settings()
        self.station_idx = min(s.get('station_idx', 0), max(0, len(self.stations) - 1))
        self.volume = max(0, min(100, s.get('volume', 70)))
        self.theme_idx = s.get('theme_idx', 0) % len(THEMES)
        self.port = s.get('port', DEFAULT_PORT)
        if self.port not in VALID_PORTS:
            self.port = DEFAULT_PORT
        splash_shown = s.get('splash_shown', False)

        self.status = 'idle'
        self.frames = 0
        self.rate = 0
        self.chans = 0
        self.error = ''

        self.screen = 'about' if not splash_shown else 'main'
        self._from_splash = not splash_shown
        self._settings_idx = 0

        self._thread_running = False
        self._thread_stop = False

        self._prev = {'CONFIRM': False, 'CANCEL': False, 'UP': False,
                      'DOWN': False, 'LEFT': False, 'RIGHT': False}
        self._draw_acc = 0
        self._combo_ms = 0
        self._combo_consumed = False

        self._last_status = ''
        dbg('APP', 'init done. port=', self.port, 'station=', self.station_idx,
            'theme=', self.theme_idx, 'screen=', self.screen)

    def _set_status(self, s):
        if s != self._last_status:
            dbg('STATUS', self._last_status, '->', s)
            self._last_status = s
        self.status = s

    def _save(self):
        save_settings({
            'station_idx':  self.station_idx,
            'volume':       self.volume,
            'theme_idx':    self.theme_idx,
            'port':         self.port,
            'splash_shown': True,
        })

    def _vol_num(self):
        return (self.volume * 256) // 100

    def _change_station(self, delta_idx):
        old = self.station_idx
        self.station_idx = (self.station_idx + delta_idx) % len(self.stations)
        dbg('UI', 'station', old, '->', self.station_idx,
            '(', self.stations[self.station_idx][0], ')')
        self._save()
        if self._thread_running:
            dbg('THR', 'station-change requested while playing; restarting')
            self._thread_stop = True
            asyncio.create_task(self._restart_after_stop())

    async def _restart_after_stop(self):
        while self._thread_running:
            await asyncio.sleep_ms(50)
        self._start_player()

    @property
    def theme(self):
        return THEMES[self.theme_idx]

    # ----------------------------------------------------------------
    # Player thread (32 KB stack, zero-alloc decode, periodic GC)
    # ----------------------------------------------------------------
    def _player_thread(self):
        dbg('THR', 'start, mem_free=', gc.mem_free())
        self._thread_running = True
        audio = None
        cur_rate = cur_ch = 0

        try:
            hc = HexpansionConfig(self.port)
            pin_bck = hc.pin[0]
            pin_lck = hc.pin[1]
            pin_din = hc.pin[2]
            dbg('HEX', 'port=', self.port,
                'BCK=', pin_bck, 'LCK=', pin_lck, 'DIN=', pin_din)
        except Exception as e:
            dbg('HEX', 'config failed:', repr(e))
            self.error = 'Bad port {}: {!r}'.format(self.port, e)
            self._set_status('error')
            self._thread_running = False
            return

        # Defensive I2S reset (recover from prior-crash leftover state)
        try:
            dbg('I2S', 'defensive init+deinit')
            _tmp = I2S(I2S_ID, sck=pin_bck, ws=pin_lck, sd=pin_din,
                       mode=I2S.TX, bits=16, format=I2S.STEREO,
                       rate=44100, ibuf=2048)
            _tmp.deinit()
            del _tmp
            dbg('I2S', 'defensive reset OK')
        except Exception as e:
            dbg('I2S', 'defensive reset failed (continuing):', repr(e))
        gc.collect()
        dbg('THR', 'pre-loop mem_free=', gc.mem_free())

        out_buf = mp3.output_buf
        wifi = network.WLAN(network.STA_IF)

        try:
            while not self._thread_stop:
                try:
                    self._set_status('connecting')
                    name, url = self.stations[self.station_idx]
                    host, port, path = parse_url(url)
                    rssi = wifi.status('rssi') if wifi.isconnected() else None
                    dbg('NET', 'connecting host=', host, 'port=', port,
                        'path=', path, 'rssi=', rssi)
                    ai = socket.getaddrinfo(host, port)[0][-1]
                    dbg('NET', 'resolved ->', ai)
                    sock = socket.socket()
                    sock.settimeout(10)
                    sock.connect(ai)
                    dbg('NET', 'TCP connected')
                    req = ('GET {} HTTP/1.0\r\nHost: {}\r\n'
                           'User-Agent: tildagon/radio\r\nIcy-MetaData: 0\r\n\r\n'
                           ).format(path, host).encode()
                    sock.send(req)
                    dbg('NET', 'GET sent (', len(req), 'bytes)')

                    buf = b''
                    while b'\r\n\r\n' not in buf:
                        chunk = sock.recv(256)
                        if not chunk:
                            raise OSError("server closed before headers")
                        buf += chunk
                    head, buf = buf.split(b'\r\n\r\n', 1)
                    dbg('NET', 'headers received:', len(head), 'bytes; first line:',
                        head.split(b'\r\n', 1)[0])
                    dbg('BUF', 'post-header buf=', len(buf))

                    while len(buf) < 4096:
                        chunk = sock.recv(2048)
                        if not chunk:
                            raise OSError("no data")
                        buf += chunk
                    dbg('BUF', 'primed buf=', len(buf))

                    self._set_status('playing')
                    self.frames = 0
                    last_diag_frame = 0
                    while not self._thread_stop:
                        if len(buf) < 2048:
                            chunk = sock.recv(2048)
                            if not chunk:
                                dbg('NET', 'recv empty; stream ended')
                                break
                            buf += chunk

                        pcm, consumed, ch, hz, samp = mp3.decode(buf)
                        buf = buf[consumed:]
                        if not pcm:
                            if self.frames == 0:
                                dbg('DEC', 'skipped', consumed,
                                    'bytes (ID3 / junk); buf now', len(buf))
                            continue

                        if hz != cur_rate or ch != cur_ch:
                            if audio is not None:
                                dbg('I2S', 'rate/ch change; deinit old')
                                audio.deinit()
                            dbg('I2S', 'init rate=', hz, 'ch=', ch)
                            audio = I2S(
                                I2S_ID,
                                sck=pin_bck, ws=pin_lck, sd=pin_din,
                                mode=I2S.TX, bits=16,
                                format=I2S.STEREO if ch == 2 else I2S.MONO,
                                rate=hz, ibuf=80000,
                            )
                            cur_rate, cur_ch = hz, ch
                            self.rate, self.chans = hz, ch
                            dbg('I2S', 'init OK')

                        # In-place volume on the natmod's own output bytearray.
                        vn = self._vol_num()
                        if vn != 256:
                            _apply_volume(out_buf, len(pcm), vn)
                        audio.write(pcm)
                        self.frames += 1

                        # Periodic GC to recover immutable-bytes churn
                        # (buf slicing / concat). Cheap relative to I2S
                        # buffering; pauses are inaudible.
                        if (self.frames % GC_EVERY_N_FRAMES) == 0:
                            gc.collect()

                        if self.frames - last_diag_frame >= 200:
                            last_diag_frame = self.frames
                            r = wifi.status('rssi') if wifi.isconnected() else None
                            dbg('THR', 'frames=', self.frames,
                                'buf=', len(buf), 'pcm=', len(pcm),
                                'mem=', gc.mem_free(), 'rssi=', r)
                    try:
                        sock.close()
                        dbg('NET', 'sock closed')
                    except Exception as e:
                        dbg('NET', 'sock close failed:', repr(e))
                    if self._thread_stop:
                        dbg('THR', 'stop requested')
                        break
                    self._set_status('reconnecting')
                    dbg('NET', 'will reconnect in 500ms')
                    time.sleep_ms(500)
                except Exception as e:
                    dbg('THR', 'inner exception:', repr(e))
                    import sys as _sys
                    _sys.print_exception(e)
                    self.error = repr(e)
                    self._set_status('error')
                    time.sleep_ms(2000)
        except BaseException as e:
            dbg('THR', 'OUTER exception:', repr(e))
            import sys as _sys
            _sys.print_exception(e)
        finally:
            if audio is not None:
                try:
                    audio.deinit()
                    dbg('I2S', 'deinit on exit')
                except Exception as e:
                    dbg('I2S', 'deinit failed:', repr(e))
            self._set_status('stopped')
            self._thread_running = False
            dbg('THR', 'exit, mem_free=', gc.mem_free())

    def _start_player(self):
        if self._thread_running:
            dbg('UI', 'play pressed but already running')
            return
        w = network.WLAN(network.STA_IF)
        if not w.isconnected():
            dbg('NET', 'play blocked: wifi not connected')
            self.error = 'WiFi not connected'
            self._set_status('error')
            return
        dbg('NET', 'wifi OK rssi=', w.status('rssi'), 'ip=', w.ifconfig()[0])
        try:
            ok = mp3.init()
            dbg('DEC', 'natmod init ok=', ok)
            if not ok:
                self.error = 'mp3.init() returned 0'
                self._set_status('error')
                return
        except Exception as e:
            dbg('DEC', 'natmod init failed:', repr(e))
            self.error = repr(e)
            self._set_status('error')
            return
        self._thread_stop = False
        try:
            _thread.stack_size(PLAYER_THREAD_STACK)
        except Exception as e:
            dbg('THR', 'stack_size failed:', repr(e))
        dbg('UI', 'starting player thread (stack=', PLAYER_THREAD_STACK, ')')
        _thread.start_new_thread(self._player_thread, ())

    def _stop_player(self):
        dbg('UI', 'stop requested')
        self._thread_stop = True

    async def _shutdown_and_minimise(self):
        dbg('UI', 'shutdown_and_minimise')
        self._stop_player()
        for _ in range(20):
            if not self._thread_running:
                break
            await asyncio.sleep_ms(50)
        dbg('UI', 'thread stopped, minimising')
        self.minimise()

    # ----------------------------------------------------------------
    # update() dispatch
    # ----------------------------------------------------------------
    def update(self, delta):
        bs = self.button_states
        up_now      = bs.get(BUTTON_TYPES['UP'])
        down_now    = bs.get(BUTTON_TYPES['DOWN'])
        left_now    = bs.get(BUTTON_TYPES['LEFT'])
        right_now   = bs.get(BUTTON_TYPES['RIGHT'])
        confirm_now = bs.get(BUTTON_TYPES['CONFIRM'])
        cancel_now  = bs.get(BUTTON_TYPES['CANCEL'])

        def edge(name, now):
            was = self._prev[name]
            self._prev[name] = now
            return now and not was

        cancel_edge  = edge('CANCEL',  cancel_now)
        confirm_edge = edge('CONFIRM', confirm_now)
        left_edge    = edge('LEFT',    left_now)
        right_edge   = edge('RIGHT',   right_now)

        if self.screen == 'main':
            return self._update_main(delta, up_now, down_now,
                                     left_edge, right_edge,
                                     confirm_edge, cancel_edge)
        elif self.screen == 'settings':
            up_edge   = edge('UP', up_now)
            down_edge = edge('DOWN', down_now)
            return self._update_settings(up_edge, down_edge,
                                         left_edge, right_edge,
                                         confirm_edge, cancel_edge)
        elif self.screen == 'about':
            self._prev['UP']   = up_now
            self._prev['DOWN'] = down_now
            return self._update_about(confirm_edge, cancel_edge)
        return False

    def _update_main(self, delta, up_now, down_now, left_edge, right_edge,
                     confirm_edge, cancel_edge):
        if cancel_edge:
            asyncio.create_task(self._shutdown_and_minimise())
            return True

        if up_now and down_now:
            self._combo_ms += delta
            if (not self._combo_consumed) and self._combo_ms >= SETTINGS_COMBO_MS:
                self._enter_settings()
                self._combo_consumed = True
            self._prev['UP'] = up_now
            self._prev['DOWN'] = down_now
            return True
        self._combo_ms = 0
        self._combo_consumed = False

        if up_now and not self._prev['UP'] and not down_now:
            self._prev['UP'] = up_now
            self._change_station(-1); return True
        if down_now and not self._prev['DOWN'] and not up_now:
            self._prev['DOWN'] = down_now
            self._change_station(+1); return True
        self._prev['UP'] = up_now
        self._prev['DOWN'] = down_now

        if left_edge:
            self.volume = max(0, self.volume - 5)
            dbg('UI', 'volume', '->', self.volume)
            self._save(); return True
        if right_edge:
            self.volume = min(100, self.volume + 5)
            dbg('UI', 'volume', '->', self.volume)
            self._save(); return True

        if confirm_edge:
            if not self._thread_running:
                dbg('UI', 'play pressed')
                self._start_player()
            else:
                self._stop_player()
            return True

        self._draw_acc += delta
        if self._draw_acc >= 250:
            self._draw_acc = 0
            return True
        return False

    def _enter_settings(self):
        dbg('UI', 'enter settings')
        if self._thread_running:
            self._stop_player()
        self.screen = 'settings'
        self._settings_idx = 0

    def _exit_settings(self):
        dbg('UI', 'exit settings')
        self.screen = 'main'
        self._save()

    def _update_settings(self, up_edge, down_edge, left_edge, right_edge,
                         confirm_edge, cancel_edge):
        if cancel_edge:
            self._exit_settings(); return True
        n = len(SETTINGS_ITEMS)
        if up_edge:
            self._settings_idx = (self._settings_idx - 1) % n; return True
        if down_edge:
            self._settings_idx = (self._settings_idx + 1) % n; return True

        label, attr = SETTINGS_ITEMS[self._settings_idx]
        if attr == 'theme_idx':
            if left_edge:
                self.theme_idx = (self.theme_idx - 1) % len(THEMES)
                dbg('UI', 'theme ->', THEMES[self.theme_idx]['name']); return True
            if right_edge:
                self.theme_idx = (self.theme_idx + 1) % len(THEMES)
                dbg('UI', 'theme ->', THEMES[self.theme_idx]['name']); return True
        elif attr == 'port':
            if left_edge or right_edge:
                idx = VALID_PORTS.index(self.port)
                step = -1 if left_edge else +1
                self.port = VALID_PORTS[(idx + step) % len(VALID_PORTS)]
                dbg('UI', 'port ->', self.port)
                return True
        elif label == 'About':
            if confirm_edge:
                dbg('UI', 'open about')
                self.screen = 'about'
                self._from_splash = False
                return True
        return False

    def _update_about(self, confirm_edge, cancel_edge):
        if confirm_edge or cancel_edge:
            if self._from_splash:
                dbg('UI', 'splash dismissed')
                self._from_splash = False
                self.screen = 'main'
                self._save()
            else:
                self.screen = 'settings'
            return True
        return False

    # ----------------------------------------------------------------
    # draw() dispatch
    # ----------------------------------------------------------------
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
        if self.screen == 'main':
            self._draw_main(ctx, t)
        elif self.screen == 'settings':
            self._draw_settings(ctx, t)
        elif self.screen == 'about':
            self._draw_about(ctx, t)
        ctx.restore()
        self.draw_overlays(ctx)

    def _draw_main(self, ctx, t):
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
        self._fit_text(ctx, prev_name, 220, 22, 14)
        ctx.rgb(*t['DIM_INK']).move_to(0, -38).text(prev_name)
        ctx.rgb(*t['HILITE']).rectangle(-120, -18, 240, 36).fill()
        self._fit_text(ctx, cur_name, 220, 26, 14)
        ctx.rgb(*t['HILITE_T']).move_to(0, 0).text(cur_name)
        self._fit_text(ctx, next_name, 220, 22, 14)
        ctx.rgb(*t['DIM_INK']).move_to(0, 30).text(next_name)
        bar_x = -70; bar_y = 58; bar_w = 140; bar_h = 10
        ctx.rgb(*t['LINE']).rectangle(bar_x - 1, bar_y - 1, bar_w + 2, bar_h + 2).stroke()
        ctx.rgb(*t['INK']).rectangle(bar_x, bar_y, (bar_w * self.volume) // 100, bar_h).fill()
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
            done = (pw * self._combo_ms) // SETTINGS_COMBO_MS
            if done > pw: done = pw
            ctx.rgb(*t['INK']).rectangle(-pw // 2, 80, done, 8).fill()
            ctx.font_size = 10
            ctx.rgb(*t['DIM_INK']).move_to(0, 96).text("hold ▲▼ for settings")
        elif self.status == 'error' and self.error:
            ctx.rgb(*t['INK']).move_to(0, 88).text(self.error[:22])
        elif self.rate:
            ctx.rgb(*t['DIM_INK']).move_to(0, 88).text(
                "{} Hz · {} ch  P{}".format(self.rate, self.chans, self.port))
        if not (self._combo_ms > 0 and not self._combo_consumed):
            ctx.font_size = 11
            ctx.rgb(*t['DIM_INK']).move_to(0, 105).text("▲▼ stn  ◀▶ vol")

    def _draw_settings(self, ctx, t):
        ctx.text_baseline = ctx.MIDDLE
        ctx.text_align = ctx.CENTER
        ctx.rgb(*t['BAR_BG']).rectangle(-120, -100, 240, 32).fill()
        ctx.rgb(*t['LINE']).rectangle(-120, -68, 240, 1).fill()
        ctx.font_size = 22
        ctx.rgb(*t['INK']).move_to(0, -84).text("Settings")
        y = -30
        ROW_H = 30
        for i, (label, attr) in enumerate(SETTINGS_ITEMS):
            highlighted = (i == self._settings_idx)
            if highlighted:
                ctx.rgb(*t['HILITE']).rectangle(-120, y - ROW_H // 2, 240, ROW_H).fill()
                ink = t['HILITE_T']
            else:
                ink = t['INK']
            ctx.font_size = 18
            ctx.text_align = ctx.LEFT
            ctx.rgb(*ink).move_to(-100, y).text(label)
            ctx.text_align = ctx.RIGHT
            if attr == 'theme_idx':
                val = THEMES[self.theme_idx]['name']
            elif attr == 'port':
                val = "{}".format(self.port)
            else:
                val = ">"
            ctx.rgb(*ink).move_to(100, y).text(val)
            y += ROW_H
        ctx.text_align = ctx.CENTER
        ctx.font_size = 11
        ctx.rgb(*t['DIM_INK']).move_to(0, 95).text("◀▶ change · ✓ open")
        ctx.rgb(*t['DIM_INK']).move_to(0, 108).text("✗ back")

    def _draw_about(self, ctx, t):
        ctx.text_baseline = ctx.MIDDLE
        ctx.text_align = ctx.CENTER
        n = len(ABOUT_LINES)
        line_h = 16
        total_h = n * line_h
        y = -(total_h // 2) + line_h // 2
        for line in ABOUT_LINES:
            if not line:
                y += line_h
                continue
            self._fit_text(ctx, line, max_w=220, base_size=15, min_size=10)
            ctx.rgb(*t['INK']).move_to(0, y).text(line)
            y += line_h
        ctx.font_size = 10
        ctx.rgb(*t['DIM_INK']).move_to(0, 108).text(
            "press any button" if self._from_splash else "✗ back")
        
__app_export__ = BadgeRadio