"""BadgeRadio audio player.

Owns the player thread, the ring buffer, the minimp3 adapter, the viper
volume routine, ICY metadata parsing, and I2S output. Exposes a small
API for the App: play(name, url) / stop() / set_volume() / set_port() /
plus readable state: status, error, rate, chans, now_playing, is_running.
"""
import socket
import network
import time
import gc
import struct
import _thread
import sys
import asyncio
import micropython
from machine import I2S

# --- Load the bundled minimp3 natmod ---------------------------------------
if '/apps/andreacampanella_BadgeRadio' not in sys.path:
    sys.path.insert(0, '/apps/andreacampanella_BadgeRadio')
import mp3 as _mp3_natmod

from system.hexpansion.config import HexpansionConfig
from stations import parse_url

I2S_ID = 0
DEFAULT_PORT = 2
VALID_PORTS = (1, 2, 3, 4, 5, 6)

# Player thread stack. 16 KB overflows and corrupts the natmod's IRAM text
# region, causing IllegalInstruction panics. 32 KB has comfortable headroom.
PLAYER_THREAD_STACK = 32 * 1024

# Ring buffer for the network stream — fixed bytearray + head/tail indices.
RING_SIZE = 8192

# 1152 samples/frame * 2 channels * 2 bytes/sample = 4608 max
_MAX_PCM_BYTES = 1152 * 2 * 2

DEBUG = False


def _dbg(tag, *args):
    if DEBUG:
        try:
            print('[{}]'.format(tag), *args)
        except Exception:
            pass


class _Mp3Adapter:
    """Adapter around the natmod's op-based protocol. Allocates output +
    info bytearrays once. Every decode call writes into those same buffers.
    Init is idempotent — the real op=0 runs at construction, subsequent
    calls just return the cached result.
    """
    def __init__(self):
        self._output = bytearray(_MAX_PCM_BYTES)
        self._info   = bytearray(20)
        _mp3_natmod.output = self._output
        _mp3_natmod.info   = self._info
        self._output_mv = memoryview(self._output)
        _mp3_natmod.op = 0
        self._init_ok = (_mp3_natmod.process() == 1)

    def init(self):
        return self._init_ok

    def decode(self, buf):
        _mp3_natmod.op = 2
        _mp3_natmod.input = buf
        _mp3_natmod.process()
        fb, ch, hz, samp, pcm_bytes = struct.unpack_from('<iiiii', self._info, 0)
        return (self._output_mv[:pcm_bytes], fb, ch, hz, samp)

    @property
    def output_buf(self):
        return self._output


_mp3 = _Mp3Adapter()


@micropython.viper
def _apply_volume(buf: ptr8, n_bytes: int, vol_num: int):
    # vol_num is in Q8 (0..256). Positive-magnitude and sign are split so
    # the >> 8 is always applied to a non-negative value — viper's signed
    # shift is implementation-defined on Xtensa and can silently corrupt
    # negative samples otherwise.
    p = ptr16(buf)
    n = n_bytes >> 1
    i = 0
    while i < n:
        u = int(p[i])
        if u >= 32768:
            m = 65536 - u
            m = (m * vol_num) >> 8
            if m > 32768:
                m = 32768
            p[i] = (65536 - m) & 0xFFFF
        else:
            m = (u * vol_num) >> 8
            if m > 32767:
                m = 32767
            p[i] = m
        i += 1


def _parse_icy_meta(meta_bytes, player):
    """Extract StreamTitle from an ICY metadata block, update player.now_playing.
    Format:  StreamTitle='Artist - Track';StreamUrl='...';   (null-padded)"""
    try:
        text = meta_bytes.rstrip(b'\x00').decode('utf-8', 'replace')
    except Exception:
        return
    marker = "StreamTitle='"
    i = text.find(marker)
    if i < 0:
        return
    j = text.find("';", i + len(marker))
    if j < 0:
        return
    title = text[i + len(marker):j].strip()
    if title != player.now_playing:
        player.now_playing = title
        _dbg('ICY', 'now_playing=', title)


class Player:
    """Audio player. Owns one background thread when playing.

    Public read-only state (updated from the player thread):
        status      : 'idle' | 'connecting' | 'playing' | 'reconnecting' | 'error' | 'stopped'
        error       : str (last error message)
        rate, chans : int (0 until the first frame decodes)
        now_playing : str (from ICY metadata, '' if none)
        frames      : int (frames since last connect)
        is_running  : bool

    Control:
        set_volume(0..100), set_port(1..6), set_debug(bool)
        play(name, url)   — start; if already running, stop and restart
        stop()            — signal thread to exit
    """
    def __init__(self, port=DEFAULT_PORT, debug=False):
        global DEBUG
        DEBUG = debug
        self.port = port
        # Public state
        self.status = 'idle'
        self.error = ''
        self.rate = 0
        self.chans = 0
        self.now_playing = ''
        self.frames = 0
        # Config
        self._volume = 70
        self._current_name = ''
        self._current_url = ''
        # Thread management
        self._thread_running = False
        self._thread_stop = False
        self._restart_pending = False
        # For status change detection
        self._last_status = ''

    # ---------- public API ------------------------------------------------
    @property
    def is_running(self):
        return self._thread_running

    def set_volume(self, v):
        self._volume = max(0, min(100, int(v)))

    def set_port(self, p):
        if p in VALID_PORTS:
            self.port = p

    def set_debug(self, on):
        global DEBUG
        DEBUG = bool(on)

    def play(self, name, url):
        """Start (or restart) playback. Non-blocking."""
        self._current_name = name
        self._current_url = url
        if self._thread_running:
            self._thread_stop = True
            if not self._restart_pending:
                self._restart_pending = True
                asyncio.create_task(self._await_and_start())
        else:
            self._start_thread()

    def stop(self):
        """Signal the player thread to exit. Non-blocking; the thread may
        take up to ~250 ms to actually stop (DMA drain)."""
        self._thread_stop = True

    # ---------- internals -------------------------------------------------
    async def _await_and_start(self):
        while self._thread_running:
            await asyncio.sleep_ms(50)
        self._restart_pending = False
        self._start_thread()

    def _start_thread(self):
        w = network.WLAN(network.STA_IF)
        if not w.isconnected():
            self.error = 'WiFi not connected'
            self._set_status('error')
            return
        if not _mp3.init():
            self.error = 'mp3 init failed'
            self._set_status('error')
            return
        self._thread_stop = False
        try:
            _thread.stack_size(PLAYER_THREAD_STACK)
        except Exception:
            pass
        # Fragmented heap after a prior crash can cause "can't create
        # thread". Two collects + short pause lets MicroPython reclaim the
        # previous thread's stack.
        gc.collect(); gc.collect()
        time.sleep_ms(200)
        try:
            _thread.start_new_thread(self._thread, ())
        except Exception as e:
            self.error = 'thread start: {!r}'.format(e)
            self._set_status('error')

    def _set_status(self, s):
        if s != self._last_status:
            _dbg('STATUS', self._last_status, '->', s)
            self._last_status = s
        self.status = s

    def _vol_num(self):
        # Squared taper — sounds more natural than linear.
        v = self._volume
        return (v * v * 256) // 10000

    # ---------- the player thread ----------------------------------------
    def _thread(self):
        _dbg('THR', 'start, mem_free=', gc.mem_free())
        self._thread_running = True
        audio = None
        cur_rate = cur_ch = 0

        try:
            hc = HexpansionConfig(self.port)
            pin_bck = hc.pin[0]
            pin_lck = hc.pin[1]
            pin_din = hc.pin[2]
        except Exception as e:
            self.error = 'Bad port {}: {!r}'.format(self.port, e)
            self._set_status('error')
            self._thread_running = False
            return

        # Defensive I2S reset (recover from prior-crash leftover state).
        try:
            _tmp = I2S(I2S_ID, sck=pin_bck, ws=pin_lck, sd=pin_din,
                       mode=I2S.TX, bits=16, format=I2S.STEREO,
                       rate=44100, ibuf=2048)
            _tmp.deinit()
            del _tmp
        except Exception:
            pass

        gc.collect()

        ring = bytearray(RING_SIZE)
        rmv = memoryview(ring)

        out_buf = _mp3.output_buf
        wifi = network.WLAN(network.STA_IF)
        consec_errors = 0

        try:
            while not self._thread_stop:
                sock = None
                try:
                    # Wifi recovery: wait up to 60 s if wifi is down.
                    if not wifi.isconnected():
                        self._set_status('reconnecting')
                        self.error = 'WiFi lost'
                        deadline = time.ticks_add(time.ticks_ms(), 60000)
                        while not wifi.isconnected():
                            if self._thread_stop:
                                break
                            if time.ticks_diff(deadline, time.ticks_ms()) < 0:
                                raise OSError('wifi still down after 60s')
                            time.sleep_ms(500)
                        if self._thread_stop:
                            break
                        self.error = ''

                    self._set_status('connecting')
                    self.now_playing = ''
                    host, port, path = parse_url(self._current_url)
                    ai = socket.getaddrinfo(host, port)[0][-1]

                    sock = socket.socket()
                    sock.settimeout(10)
                    sock.connect(ai)

                    req = ('GET {} HTTP/1.0\r\nHost: {}\r\n'
                           'User-Agent: tildagon/radio\r\nIcy-MetaData: 1\r\n\r\n'
                           ).format(path, host).encode()
                    sock.send(req)

                    # Header parse — one-shot allocation is fine, not hot path.
                    hbuf = b''
                    while b'\r\n\r\n' not in hbuf:
                        chunk = sock.recv(256)
                        if not chunk:
                            raise OSError("server closed before headers")
                        hbuf += chunk
                        if len(hbuf) > 8192:
                            raise OSError("header too large")
                    head, leftover = hbuf.split(b'\r\n\r\n', 1)

                    status_line = head.split(b'\r\n', 1)[0]
                    if b' 200' not in status_line:
                        raise OSError('bad status: {}'.format(
                            status_line[:60].decode('ascii', 'ignore')))

                    metaint = 0
                    for line in head.split(b'\r\n')[1:]:
                        if line[:12].lower() == b'icy-metaint:':
                            try:
                                metaint = int(line[12:].strip())
                            except ValueError:
                                pass
                            break
                    _dbg('NET', 'headers OK; metaint=', metaint,
                         'leftover=', len(leftover))

                    # Seed the ring with any bytes past the header.
                    r_head = 0
                    r_tail = len(leftover)
                    if r_tail:
                        rmv[0:r_tail] = leftover
                    leftover = None
                    hbuf = None

                    _al = [metaint - r_tail if metaint > 0 else 0]

                    def icy_recv_into(mv, want, _al=_al, self=self):
                        """Fill mv[:want] with pure audio, stripping ICY
                        metadata blocks. Returns bytes written."""
                        written = 0
                        while written < want:
                            if metaint > 0 and _al[0] <= 0:
                                lb = sock.recv(1)
                                if not lb:
                                    return written
                                mlen = lb[0] * 16
                                if mlen:
                                    meta = b''
                                    while len(meta) < mlen:
                                        piece = sock.recv(mlen - len(meta))
                                        if not piece:
                                            return written
                                        meta += piece
                                    _parse_icy_meta(meta, self)
                                _al[0] = metaint
                                continue
                            w = want - written
                            if metaint > 0 and w > _al[0]:
                                w = _al[0]
                            chunk = sock.recv(w)
                            if not chunk:
                                return written
                            n = len(chunk)
                            mv[written:written + n] = chunk
                            written += n
                            if metaint > 0:
                                _al[0] -= n
                        return written

                    def refill(target):
                        """Ensure at least `target` bytes are in the ring."""
                        nonlocal r_head, r_tail
                        while (r_tail - r_head) < target:
                            space = RING_SIZE - r_tail
                            if space < 2048:
                                remaining = r_tail - r_head
                                if remaining > 0:
                                    rmv[0:remaining] = rmv[r_head:r_tail]
                                r_head = 0
                                r_tail = remaining
                                space = RING_SIZE - r_tail
                            want = 2048 if space >= 2048 else space
                            n = icy_recv_into(rmv[r_tail:r_tail + want], want)
                            if n == 0:
                                return False
                            r_tail += n
                        return True

                    if not refill(4096):
                        raise OSError("no data")

                    self._set_status('playing')
                    self.frames = 0
                    consec_errors = 0

                    while not self._thread_stop:
                        if (r_tail - r_head) < 2048:
                            if not refill(2048):
                                break

                        pcm, consumed, ch, hz, samp = _mp3.decode(rmv[r_head:r_tail])
                        r_head += consumed
                        if not pcm:
                            if consumed == 0:
                                if not refill((r_tail - r_head) + 2048):
                                    break
                            continue

                        if hz != cur_rate or ch != cur_ch:
                            if audio is not None:
                                audio.deinit()
                            # Aim for ~300 ms of buffering, clamped. I2S ibuf
                            # comes from DMA-safe RAM which is more limited
                            # than general heap — 96 KB OOMs on ESP32-S3.
                            # 48 KB is the practical upper bound.
                            ibuf_bytes = hz * ch * 2 * 300 // 1000
                            if ibuf_bytes < 20000: ibuf_bytes = 20000
                            if ibuf_bytes > 48000: ibuf_bytes = 48000
                            _dbg('I2S', 'init rate=', hz, 'ch=', ch,
                                 'ibuf=', ibuf_bytes)
                            audio = I2S(
                                I2S_ID,
                                sck=pin_bck, ws=pin_lck, sd=pin_din,
                                mode=I2S.TX, bits=16,
                                format=I2S.STEREO if ch == 2 else I2S.MONO,
                                rate=hz, ibuf=ibuf_bytes,
                            )
                            cur_rate, cur_ch = hz, ch
                            self.rate, self.chans = hz, ch

                        vn = self._vol_num()
                        if vn == 0:
                            self.frames += 1
                        else:
                            if vn != 256:
                                _apply_volume(out_buf, len(pcm), vn)
                            audio.write(pcm)
                            self.frames += 1

                        # Yield the GIL so the main asyncio loop can service
                        # button events. 1 ms is inaudible (DMA ~250 ms).
                        time.sleep_ms(1)

                    if sock is not None:
                        try:
                            sock.close()
                        except Exception:
                            pass
                        sock = None

                    if self._thread_stop:
                        break

                    self._set_status('reconnecting')
                    for _ in range(10):
                        if self._thread_stop: break
                        time.sleep_ms(50)

                except Exception as e:
                    _dbg('THR', 'inner exception:', repr(e))
                    if DEBUG:
                        sys.print_exception(e)
                    if sock is not None:
                        try:
                            sock.close()
                        except Exception:
                            pass
                        sock = None
                    msg = str(e)
                    self.error = msg if msg else repr(e)
                    self._set_status('error')
                    # Exponential backoff: 2 s → 30 s.
                    consec_errors += 1
                    delay_ms = min(2000 * (1 << min(consec_errors - 1, 4)), 30000)
                    for _ in range(delay_ms // 100):
                        if self._thread_stop: break
                        time.sleep_ms(100)
                    gc.collect()

        except BaseException as e:
            _dbg('THR', 'OUTER exception:', repr(e))
            if DEBUG:
                sys.print_exception(e)
        finally:
            if audio is not None:
                try:
                    audio.deinit()
                except Exception:
                    pass
            self._set_status('stopped')
            self._thread_running = False
            _dbg('THR', 'exit, mem_free=', gc.mem_free())
