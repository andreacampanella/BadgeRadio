"""BadgeRadio UI — themes, constants, draw functions.

Draw functions take (ctx, theme, app) and read state off `app`. Keeping
them as functions (not methods on App) keeps the coordinator class small
and lets us split screens cleanly by concern.
"""

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


def about_lines(version):
    return [
        "BadgeRadio",
        "v" + version,
        "",
        "You will need a suitable",
        "pcm5102 expansion to use this",
        "HMU on social @emuboy",
        "or check github for info",
        "github/andreacampanella/BadgeRadio",
    ]


def fit_text(ctx, text, max_w, base_size, min_size=12):
    """Shrink font_size until text fits in max_w, but not below min_size."""
    size = base_size
    ctx.font_size = size
    while size > min_size and int(ctx.text_width(text)) > max_w:
        size -= 1
        ctx.font_size = size
    return size


def neighbour_station(stations, idx, delta):
    """Return (name, url) of the station in the given direction. Dividers
    (empty URL) are included — they act as visible genre markers on the
    main screen."""
    n = len(stations)
    if n == 0:
        return ("", "")
    return stations[(idx + (1 if delta >= 0 else -1)) % n]


# ----------------------------------------------------------------
# Screens
# ----------------------------------------------------------------
def draw_main(ctx, t, app):
    ctx.rgb(*t['BAR_BG']).rectangle(-120, -100, 240, 32).fill()
    ctx.rgb(*t['LINE']).rectangle(-120, -68, 240, 1).fill()

    ctx.text_baseline = ctx.MIDDLE
    ctx.text_align = ctx.CENTER
    ctx.font_size = 22
    ctx.rgb(*t['INK']).move_to(0, -84).text("BadgeRadio")

    prev_name = neighbour_station(app.stations, app.station_idx, -1)[0]
    cur_name  = app.stations[app.station_idx][0]
    next_name = neighbour_station(app.stations, app.station_idx, +1)[0]

    fit_text(ctx, prev_name, 220, 22, 14)
    ctx.rgb(*t['DIM_INK']).move_to(0, -38).text(prev_name)

    ctx.rgb(*t['HILITE']).rectangle(-120, -18, 240, 36).fill()
    fit_text(ctx, cur_name, 220, 26, 14)
    ctx.rgb(*t['HILITE_T']).move_to(0, 0).text(cur_name)

    fit_text(ctx, next_name, 220, 22, 14)
    ctx.rgb(*t['DIM_INK']).move_to(0, 30).text(next_name)

    bar_x, bar_y, bar_w, bar_h = -70, 58, 140, 10
    ctx.rgb(*t['LINE']).rectangle(bar_x - 1, bar_y - 1, bar_w + 2, bar_h + 2).stroke()
    ctx.rgb(*t['INK']).rectangle(bar_x, bar_y, (bar_w * app.volume) // 100, bar_h).fill()
    ctx.font_size = 12
    ctx.text_align = ctx.LEFT
    ctx.rgb(*t['INK']).move_to(-100, 63).text("VOL")
    ctx.text_align = ctx.RIGHT
    ctx.rgb(*t['INK']).move_to(100, 63).text("{}%".format(app.volume))

    ctx.text_align = ctx.CENTER
    if app.combo_ms > 0 and not app.combo_consumed:
        pw = 100
        ctx.rgb(*t['LINE']).rectangle(-pw // 2 - 1, 79, pw + 2, 10).stroke()
        done = (pw * app.combo_ms) // app.SETTINGS_COMBO_MS
        if done > pw:
            done = pw
        ctx.rgb(*t['INK']).rectangle(-pw // 2, 80, done, 8).fill()
        ctx.font_size = 10
        ctx.rgb(*t['DIM_INK']).move_to(0, 96).text("hold ▲▼ for settings")
    ctx.font_size = 11
    ctx.rgb(*t['DIM_INK']).move_to(0, 105).text("▲▼ browse  ✓ play")


def draw_play(ctx, t, app):
    ctx.rgb(*t['BAR_BG']).rectangle(-120, -100, 240, 32).fill()
    ctx.rgb(*t['LINE']).rectangle(-120, -68, 240, 1).fill()

    ctx.text_baseline = ctx.MIDDLE
    ctx.text_align = ctx.CENTER
    ctx.font_size = 20
    glyph = '\u25B6' if app.player.status == 'playing' else '\u25A0'
    label = TITLE_LABEL.get(app.player.status, 'Radio')
    ctx.rgb(*t['INK']).move_to(0, -84).text('{}  {}'.format(glyph, label))

    cur_name = app.stations[app.station_idx][0]
    fit_text(ctx, cur_name, 220, 22, 14)
    ctx.rgb(*t['INK']).move_to(0, -46).text(cur_name)

    now_playing = app.player.now_playing
    if now_playing:
        ctx.font_size = 12
        ctx.rgb(*t['DIM_INK']).move_to(0, -22).text("♪ NOW PLAYING")
        title = now_playing
        ctx.font_size = 15
        if int(ctx.text_width(title)) <= 220:
            ctx.rgb(*t['INK']).move_to(0, -2).text(title)
        else:
            mid = len(title) // 2
            split = title.rfind(' ', 0, mid + 8)
            if split < 0 or split < mid - 12:
                split = title.find(' ', mid)
            if split < 0:
                fit_text(ctx, title, 220, 15, 9)
                ctx.rgb(*t['INK']).move_to(0, -2).text(title)
            else:
                line1, line2 = title[:split], title[split + 1:]
                fit_text(ctx, line1, 220, 14, 10)
                ctx.rgb(*t['INK']).move_to(0, -8).text(line1)
                fit_text(ctx, line2, 220, 14, 10)
                ctx.rgb(*t['INK']).move_to(0, 10).text(line2)
    elif app.player.status == 'error' and app.player.error:
        fit_text(ctx, app.player.error, 220, 12, 9)
        ctx.rgb(*t['INK']).move_to(0, -2).text(app.player.error)
    elif app.player.rate:
        ctx.font_size = 12
        ctx.rgb(*t['DIM_INK']).move_to(0, 0).text(
            "{} Hz · {} ch  P{}".format(app.player.rate, app.player.chans, app.port))

    bar_x, bar_y, bar_w, bar_h = -70, 42, 140, 10
    ctx.rgb(*t['LINE']).rectangle(bar_x - 1, bar_y - 1, bar_w + 2, bar_h + 2).stroke()
    ctx.rgb(*t['INK']).rectangle(bar_x, bar_y, (bar_w * app.volume) // 100, bar_h).fill()
    ctx.font_size = 12
    ctx.text_align = ctx.LEFT
    ctx.rgb(*t['INK']).move_to(-100, 47).text("VOL")
    ctx.text_align = ctx.RIGHT
    ctx.rgb(*t['INK']).move_to(100, 47).text("{}%".format(app.volume))

    ctx.text_align = ctx.CENTER
    ctx.font_size = 10
    ctx.rgb(*t['DIM_INK']).move_to(0, 76).text("▲▼ station · ◀▶ vol")
    stop_or_play = "stop" if app.player.status == 'playing' else "play"
    ctx.rgb(*t['DIM_INK']).move_to(0, 90).text(
        "✓ {} · ✗ back".format(stop_or_play))
    if app.player.rate and now_playing:
        ctx.rgb(*t['DIM_INK']).move_to(0, 104).text(
            "{} Hz · {} ch".format(app.player.rate, app.player.chans))


def draw_settings(ctx, t, app):
    ctx.text_baseline = ctx.MIDDLE
    ctx.text_align = ctx.CENTER
    ctx.rgb(*t['BAR_BG']).rectangle(-120, -100, 240, 32).fill()
    ctx.rgb(*t['LINE']).rectangle(-120, -68, 240, 1).fill()
    ctx.font_size = 22
    ctx.rgb(*t['INK']).move_to(0, -84).text("Settings")

    y = -30
    ROW_H = 30
    for i, (label, attr) in enumerate(SETTINGS_ITEMS):
        highlighted = (i == app.settings_idx)
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
            val = THEMES[app.theme_idx]['name']
        elif attr == 'port':
            val = "{}".format(app.port)
        else:
            val = ">"
        ctx.rgb(*ink).move_to(100, y).text(val)
        y += ROW_H

    ctx.text_align = ctx.CENTER
    ctx.font_size = 11
    ctx.rgb(*t['DIM_INK']).move_to(0, 95).text("◀▶ change · ✓ next")
    ctx.rgb(*t['DIM_INK']).move_to(0, 108).text("✗ back")


def draw_about(ctx, t, app, lines):
    ctx.text_baseline = ctx.MIDDLE
    ctx.text_align = ctx.CENTER
    n = len(lines)
    line_h = 16
    total_h = n * line_h
    y = -(total_h // 2) + line_h // 2
    for line in lines:
        if not line:
            y += line_h
            continue
        fit_text(ctx, line, max_w=220, base_size=15, min_size=10)
        ctx.rgb(*t['INK']).move_to(0, y).text(line)
        y += line_h
    ctx.font_size = 10
    ctx.rgb(*t['DIM_INK']).move_to(0, 108).text(
        "press any button" if app.from_splash else "✗ back")
