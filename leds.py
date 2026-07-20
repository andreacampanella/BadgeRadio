"""LED animation for the BadgeRadio play screen.

Front ring (12 LEDs, indices 0–11): slow green comet chase during playback.
Back ring  (6 LEDs, indices 13–18):  slow green triangle pulse during playback.
Both rings share amber pulse (connecting) and dim red (error) states.
"""
import time

_FRONT_LEDS = tuple(range(0, 12))
_BACK_LEDS  = tuple(range(13, 19))

# Guarded import so the app still runs on firmwares without tildagonos.
try:
    from tildagonos import tildagonos as _leds
    _OK = True
except Exception:
    _leds = None
    _OK = False


class LEDController:
    def __init__(self):
        self._last_ms = 0
        self._last_back = None
        self._last_front = None
        if _OK:
            try:
                _leds.set_led_power(True)
            except Exception:
                pass

    def update(self, screen, status):
        """Call every UI tick with the current screen name and player status.
        Throttles internally to 20 Hz and only writes when colours change."""
        if not _OK:
            return
        now = time.ticks_ms()
        if time.ticks_diff(now, self._last_ms) < 50:
            return
        self._last_ms = now

        back, front = self._compute(now, screen, status)
        if back == self._last_back and front == self._last_front:
            return
        self._last_back = back
        self._last_front = list(front)

        try:
            for i in _BACK_LEDS:
                _leds.leds[i] = back
            for i in _FRONT_LEDS:
                _leds.leds[i] = front[i]
            _leds.leds.write()
        except Exception:
            pass

    def off(self):
        """Blank all LEDs. Call on app minimise so the pattern app gets a
        clean slate when it resumes."""
        if not _OK:
            return
        try:
            for i in _BACK_LEDS:
                _leds.leds[i] = (0, 0, 0)
            for i in _FRONT_LEDS:
                _leds.leds[i] = (0, 0, 0)
            _leds.leds.write()
        except Exception:
            pass

    @staticmethod
    def _compute(now, screen, status):
        back = (0, 0, 0)
        front = [(0, 0, 0)] * 12
        if screen != 'play':
            return back, front

        if status == 'playing':
            # Back: 3 s green triangle, peak 48/255.
            phase = now % 3000
            b = (phase * 48 // 1500) if phase < 1500 \
                else ((3000 - phase) * 48 // 1500)
            back = (0, b, 0)
            # Front: comet chase — one step per 500 ms → full lap in 6 s.
            pos = (now // 500) % 12
            for i in range(12):
                dist = (pos - i) % 12
                if dist == 0:
                    front[i] = (0, 48, 0)
                elif dist == 1:
                    front[i] = (0, 16, 0)
                elif dist == 2:
                    front[i] = (0, 4, 0)
        elif status == 'error':
            back = (24, 0, 0)
            front = [(24, 0, 0)] * 12
        elif status in ('connecting', 'reconnecting'):
            phase = now % 1500
            b = (phase * 32 // 750) if phase < 750 \
                else ((1500 - phase) * 32 // 750)
            col = (b, b // 2, 0)
            back = col
            front = [col] * 12

        return back, front
