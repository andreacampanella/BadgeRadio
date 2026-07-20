"""BadgeRadio — Tildagon internet-radio app (coordinator).

This file wires together the modules:
    player.Player          — audio pipeline
    leds.LEDController     — front + back LED animation
    ui.*                   — themes, screens, draw functions
    stations.*             — station list + settings persistence

State ownership:
    - App owns: station list, current index, volume, port, theme, screen
    - Player owns: playback state (status, error, rate, chans, now_playing)
    - App reads player state; player reads volume/port on start.
"""
import asyncio
import gc
import sys

import app as _app
from events.input import Buttons, BUTTON_TYPES
from system.eventbus import eventbus

# Ensure sibling modules load from the app's own directory.
if '/apps/andreacampanella_BadgeRadio' not in sys.path:
    sys.path.insert(0, '/apps/andreacampanella_BadgeRadio')

from stations import load_stations, load_settings, save_settings
from player import Player, VALID_PORTS, DEFAULT_PORT
from leds import LEDController
from ui import (
    THEMES, SETTINGS_ITEMS,
    about_lines,
    draw_main, draw_play, draw_settings, draw_about,
)

VERSION = "3.8.0"
SETTINGS_COMBO_MS = 1500


class BadgeRadio(_app.App):
    # Exposed to ui.draw_main for the settings combo progress bar.
    SETTINGS_COMBO_MS = SETTINGS_COMBO_MS

    def __init__(self):
        super().__init__()
        # Disable the default LED pattern app so we own the ring.
        try:
            from system.patterndisplay.events import PatternDisable
            eventbus.emit(PatternDisable())
        except Exception:
            pass

        self.button_states = Buttons(self)

        # --- persisted state ---------------------------------------------
        self.stations = load_stations()
        s = load_settings()
        self.station_idx = min(s.get('station_idx', 0),
                               max(0, len(self.stations) - 1))
        # Skip past dividers (empty URL) at boot.
        while self.stations and not self.stations[self.station_idx][1]:
            self.station_idx = (self.station_idx + 1) % len(self.stations)
            if self.station_idx == 0:
                break
        self.volume = max(0, min(100, s.get('volume', 70)))
        self.theme_idx = s.get('theme_idx', 0) % len(THEMES)
        self.port = s.get('port', DEFAULT_PORT)
        if self.port not in VALID_PORTS:
            self.port = DEFAULT_PORT
        splash_shown = s.get('splash_shown', False)

        # --- transient state ---------------------------------------------
        self.screen = 'about' if not splash_shown else 'main'
        self.from_splash = not splash_shown
        self.settings_idx = 0
        self.combo_ms = 0
        self.combo_consumed = False
        self._draw_acc = 0
        self._prev = {'CONFIRM': False, 'CANCEL': False, 'UP': False,
                      'DOWN': False, 'LEFT': False, 'RIGHT': False}

        # --- components ---------------------------------------------------
        self.player = Player(self.port, debug=False)
        self.player.set_volume(self.volume)
        self.leds = LEDController()

    # ==================================================================
    # Persistence
    # ==================================================================
    def _save(self):
        save_settings({
            'station_idx':  self.station_idx,
            'volume':       self.volume,
            'theme_idx':    self.theme_idx,
            'port':         self.port,
            'splash_shown': True,
        })

    # ==================================================================
    # Player wiring
    # ==================================================================
    def _play_current(self):
        name, url = self.stations[self.station_idx]
        self.player.set_port(self.port)
        self.player.play(name, url)

    def _change_station(self, delta, skip_dividers=False):
        """Rotate station_idx by delta. On the main (browse) screen we
        show dividers as genre markers; on the play screen we skip past
        them since they can't be played."""
        n = len(self.stations)
        if n == 0:
            return
        step = 1 if delta >= 0 else -1
        for _ in range(n):
            self.station_idx = (self.station_idx + step) % n
            if not skip_dividers or self.stations[self.station_idx][1]:
                break
        self._save()
        if self.player.is_running:
            self._play_current()

    # ==================================================================
    # UI: theme accessor for draw()
    # ==================================================================
    @property
    def theme(self):
        return THEMES[self.theme_idx]

    # ==================================================================
    # Lifecycle
    # ==================================================================
    async def _shutdown_and_minimise(self):
        self.player.stop()
        self.leds.off()
        try:
            from system.patterndisplay.events import PatternEnable
            eventbus.emit(PatternEnable())
        except Exception:
            pass
        self.minimise()

    # ==================================================================
    # update() — button handling + invariants
    # ==================================================================
    def update(self, delta):
        # Invariant: player only runs on the 'play' screen.
        if self.player.is_running and self.screen != 'play':
            self.player.stop()

        # LEDs animate every tick.
        self.leds.update(self.screen, self.player.status)

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
        elif self.screen == 'play':
            up_edge   = edge('UP', up_now)
            down_edge = edge('DOWN', down_now)
            return self._update_play(delta, up_edge, down_edge,
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
            self.combo_ms += delta
            if (not self.combo_consumed) and self.combo_ms >= SETTINGS_COMBO_MS:
                self.screen = 'settings'
                self.settings_idx = 0
                self.combo_consumed = True
            self._prev['UP'] = up_now
            self._prev['DOWN'] = down_now
            return True
        self.combo_ms = 0
        self.combo_consumed = False

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
            self.player.set_volume(self.volume)
            self._save(); return True
        if right_edge:
            self.volume = min(100, self.volume + 5)
            self.player.set_volume(self.volume)
            self._save(); return True
        if confirm_edge:
            if not self.stations[self.station_idx][1]:
                return True  # divider — no playback
            self._play_current()
            self.screen = 'play'
            return True

        self._draw_acc += delta
        if self._draw_acc >= 250:
            self._draw_acc = 0
            return True
        return False

    def _update_play(self, delta, up_edge, down_edge, left_edge, right_edge,
                     confirm_edge, cancel_edge):
        if cancel_edge:
            self.player.stop()
            self.screen = 'main'
            return True
        if confirm_edge:
            if self.player.is_running:
                self.player.stop()
            else:
                self._play_current()
            return True
        if up_edge:
            self._change_station(-1, skip_dividers=True); return True
        if down_edge:
            self._change_station(+1, skip_dividers=True); return True
        if left_edge:
            self.volume = max(0, self.volume - 5)
            self.player.set_volume(self.volume)
            self._save(); return True
        if right_edge:
            self.volume = min(100, self.volume + 5)
            self.player.set_volume(self.volume)
            self._save(); return True
        self._draw_acc += delta
        if self._draw_acc >= 250:
            self._draw_acc = 0
            return True
        return False

    def _update_settings(self, up_edge, down_edge, left_edge, right_edge,
                         confirm_edge, cancel_edge):
        if cancel_edge:
            self.screen = 'main'
            self._save()
            return True
        n = len(SETTINGS_ITEMS)
        if up_edge:
            self.settings_idx = (self.settings_idx - 1) % n; return True
        if down_edge:
            self.settings_idx = (self.settings_idx + 1) % n; return True

        label, attr = SETTINGS_ITEMS[self.settings_idx]
        if attr == 'theme_idx':
            if left_edge:
                self.theme_idx = (self.theme_idx - 1) % len(THEMES)
                self._save(); return True
            if right_edge or confirm_edge:
                self.theme_idx = (self.theme_idx + 1) % len(THEMES)
                self._save(); return True
        elif attr == 'port':
            if left_edge or right_edge or confirm_edge:
                idx = VALID_PORTS.index(self.port)
                step = -1 if left_edge else +1
                self.port = VALID_PORTS[(idx + step) % len(VALID_PORTS)]
                self.player.set_port(self.port)
                self._save(); return True
        elif label == 'About':
            if confirm_edge:
                self.screen = 'about'
                self.from_splash = False
                return True
        return False

    def _update_about(self, confirm_edge, cancel_edge):
        if confirm_edge or cancel_edge:
            if self.from_splash:
                self.from_splash = False
                self.screen = 'main'
                self._save()
            else:
                self.screen = 'settings'
            return True
        return False

    # ==================================================================
    # draw() — dispatch
    # ==================================================================
    def draw(self, ctx):
        t = self.theme
        ctx.save()
        ctx.rgb(*t['BG']).rectangle(-120, -120, 240, 240).fill()
        if self.screen == 'main':
            draw_main(ctx, t, self)
        elif self.screen == 'play':
            draw_play(ctx, t, self)
        elif self.screen == 'settings':
            draw_settings(ctx, t, self)
        elif self.screen == 'about':
            draw_about(ctx, t, self, about_lines(VERSION))
        ctx.restore()
        self.draw_overlays(ctx)


__app_export__ = BadgeRadio
