"""The terminal banner: the SprintBaton logo (brand/sprintbaton-primary.svg)
drawn in half-block characters — the baton, the wordmark typed out of it, and
the cursor block that rides ahead of the typing and settles where the logo has
it. Cosmetic only, stdlib only.

`show_banner` picks one of four outcomes from the stream and environment:
nothing (not a terminal), the plain figlet text (a terminal that cannot draw
the logo), the finished logo as a still frame, or the animation.
"""
import os
import shutil
import sys
import time

# Figlet "slant" rendering of "SprintBaton": the fallback for a terminal that
# cannot draw the logo (no 24-bit colour, NO_COLOR, or too narrow).
FIGLET = r"""
   _____            _       __  ____        __
  / ___/____  _____(_)___  / /_/ __ )____ _/ /_____  ____
  \__ \/ __ \/ ___/ / __ \/ __/ __  / __ `/ __/ __ \/ __ \
 ___/ / /_/ / /  / / / / / /_/ /_/ / /_/ / /_/ /_/ / / / /
/____/ .___/_/  /_/_/ /_/\__/_____/\__,_/\__/\____/_/ /_/
    /_/
"""

# Brand colours, from brand/sprintbaton-primary.svg.
BG = (0xB3, 0x47, 0x58)
LINE = (0xC5, 0x9A, 0xA8)      # #d4ddea at 55% over the background
TEXT = (0xD4, 0xDD, 0xEA)
CURSOR = (0x92, 0xBE, 0x44)
BATON = ((0xB0, 0x66, 0x0F), (0xF4, 0xB2, 0x5B), (0xFF, 0xE6, 0xBD), (0xE2, 0x84, 0x13))

# Pixel font, 9 rows: cap height 0-6, x-height 2-6, descender 7-8. "Sprint" is
# the regular weight (4 wide) and "Baton" the bold one (5 wide), as in the logo.
_THIN = {
    "S": (".###", "#...", "#...", ".##.", "...#", "...#", "###."),
    "p": ("", "", "###.", "#..#", "#..#", "#..#", "###.", "#...", "#..."),
    "r": ("", "", "#.##", "##..", "#...", "#...", "#..."),
    "i": (".#..", "", "##..", ".#..", ".#..", ".#..", "###."),
    "n": ("", "", "###.", "#..#", "#..#", "#..#", "#..#"),
    "t": (".#..", ".#..", "####", ".#..", ".#..", ".#..", "..##"),
}
_BOLD = {
    "B": ("####.", "##.##", "##.##", "####.", "##.##", "##.##", "####."),
    "a": ("", "", ".###.", "...##", ".####", "##.##", ".####"),
    "t": (".##..", ".##..", "#####", ".##..", ".##..", ".##..", "..###"),
    "o": ("", "", ".###.", "##.##", "##.##", "##.##", ".###."),
    "n": ("", "", "####.", "##.##", "##.##", "##.##", "##.##"),
}
_WORD = [(c, _THIN, 4) for c in "Sprint"] + [(c, _BOLD, 5) for c in "Baton"]

WIDTH, _HEIGHT = 79, 24        # pixels; two pixel rows per terminal row
ROWS = _HEIGHT // 2
_LOGO_Y = 5                    # top of the 14px logo band
_BATON_X, _TEXT_X, _TEXT_Y = 2, 13, _LOGO_Y + 3

# Seconds per frame.
_SLIDE_FRAME = 0.035
_TYPE_FRAME = 0.092
_BLINK_FRAME = 0.35


def _frame(typed: int, cursor_on: bool, baton_dx: int = 0) -> list[list[tuple]]:
    px = [[BG] * WIDTH for _ in range(_HEIGHT)]
    for y in (2, 21):
        px[y] = [LINE] * WIDTH
    # The baton: a 4px bar leaning 19 degrees, with rounded ends.
    for y in range(14):
        x0 = _BATON_X + baton_dx + round((13 - y) * 5 / 13)
        for i in (range(4) if 0 < y < 13 else range(1, 3)):
            if 0 <= x0 + i < WIDTH:
                px[_LOGO_Y + y][x0 + i] = BATON[i]
    x = _TEXT_X
    for ch, font, width in _WORD[:typed]:
        for gy, row in enumerate(font[ch]):
            for gx, bit in enumerate(row):
                if bit == "#":
                    px[_TEXT_Y + gy][x + gx] = TEXT
        x += width + 1
    if cursor_on:
        for y in range(_TEXT_Y + 1, _TEXT_Y + 9):
            for cx in range(x + 1, x + 4):
                px[y][cx] = CURSOR
    return px


def _render(px: list[list[tuple]]) -> str:
    out = []
    for r in range(ROWS):
        line, last = [], None
        for top, bot in zip(px[2 * r], px[2 * r + 1]):
            if (top, bot) != last:
                line.append("\x1b[38;2;%d;%d;%dm\x1b[48;2;%d;%d;%dm" % (top + bot))
                last = (top, bot)
            line.append("▀")
        out.append("".join(line) + "\x1b[0m")
    return "\n".join(out) + "\n"


def supports_logo(stream=None, env=None) -> bool:
    """Whether the stream is a terminal that can draw the logo: wide enough and
    24-bit colour. Few terminals advertise 24-bit colour reliably (COLORTERM is
    not forwarded by tmux or over ssh), so this refuses the known-incapable
    ones rather than requiring the advertisement."""
    stream = stream or sys.stdout
    env = os.environ if env is None else env
    if not stream.isatty() or env.get("NO_COLOR"):
        return False
    if shutil.get_terminal_size().columns < WIDTH:
        return False
    if env.get("COLORTERM", "").lower() in ("truecolor", "24bit"):
        return True
    return (env.get("TERM", "") not in ("", "dumb", "linux")
            and env.get("TERM_PROGRAM") != "Apple_Terminal")


def style(text: str, rgb: tuple | None = None, *, bold: bool = False,
          stream=None, env=None) -> str:
    """`text` in a brand colour, or unchanged where the logo is not drawn."""
    if not supports_logo(stream, env):
        return text
    codes = ("\x1b[1m" if bold else "") + ("\x1b[38;2;%d;%d;%dm" % rgb if rgb else "")
    return f"{codes}{text}\x1b[0m" if codes else text


def show_banner(*, animate: bool = False, stream=None, env=None,
                sleep=time.sleep) -> None:
    """Print the banner. Not a terminal: nothing. A terminal that cannot draw
    the logo: the figlet text. Otherwise the logo — typed out when `animate`,
    else the finished frame at once."""
    stream = stream or sys.stdout
    if not stream.isatty():
        return
    if not supports_logo(stream, env):
        stream.write(FIGLET + "\n")
        stream.flush()
        return
    final = _frame(len(_WORD), True)
    if not animate:
        stream.write(_render(final))
        stream.flush()
        return

    first = True

    def show(px, pause):
        nonlocal first
        stream.write(("" if first else "\x1b[%dA" % ROWS) + _render(px))
        stream.flush()
        first = False
        sleep(pause)

    stream.write("\x1b[?25l")              # hide the real cursor
    try:
        # The baton runs in from the left...
        for dx in (-10, -8, -6, -4, -3, -2, -1, 0):
            show(_frame(0, False, dx), _SLIDE_FRAME)
        # ...the word is typed straight out of it at an even pace, the cursor
        # riding ahead...
        for n in range(len(_WORD) + 1):
            show(_frame(n, True), _TYPE_FRAME)
        # ...and after a few blinks it settles into the logo.
        for on in (False, True, False, True, False, True):
            show(_frame(len(_WORD), on), _BLINK_FRAME)
    finally:
        stream.write("\x1b[0m\x1b[?25h")
        stream.flush()
