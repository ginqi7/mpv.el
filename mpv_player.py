"""The mpv player's lifecycle: build the player, install the key bindings,
start the polling thread, run the Qt event loop.

This module knows neither subtitle rendering (SubtitleHover) nor the Emacs
bridge; whatever it needs from above arrives through the `hooks` argument --
dependencies run in one direction only:

    subtitle.py <- mpv_player.py <- mpv_bridge.py

`hooks` is duck typed; all four methods are called synchronously by this
module, each on its own thread:
    on_ready(player)                -> the render object, before the event loop
    on_hover_word(sub, word, x, y)  -> polling thread, when the word changed
    on_right_click(sub, word, x, y) -> mpv thread, on a right click on a sub
    on_time(pos)                    -> mpv thread, on a whole-second crossing
"""

import locale
import os
import shutil
import sys
import tempfile
import threading
import time

from mpv import MPV
from PySide6.QtWidgets import QApplication

# Status log for the subtitle chain. It spans three layers -- mpv / Qt /
# overlay -- and when it breaks none of them errors out: a bad bitmap, a
# misplaced box, a missing overlay all read from the outside as
# "subtitles are not showing". Only with this on can you see at a glance which
# link broke; the render side uses it too, hence it lives here.
DEBUG_SUB = True


def sub_log(*a):
    if DEBUG_SUB:
        print("[sub]", *a, flush=True)


# Hover-detection poll interval (ms). mpv only reports mouse coordinates, so
# the hit test has to poll.
POLL_MS = 40

# Minimum gap (seconds) between subtitle redraws after a size change. While the
# window is being resized osd-dimensions changes every frame, and one redraw
# means generating and writing out a 4 MB bitmap, so without a limit it
# stutters. The final redraw always lands once the drag is over, so the limit
# only costs feel mid-drag.
RELAYOUT_MIN_GAP = 0.15

# Key bindings. We hand mpv an entire replacement --input-conf file, so mpv's
# default config (f leaves fullscreen, space pauses, ...) is gone entirely;
# everything worth keeping is spelled out again here.
#
# How mouse keys travel back from mpv: mpv bindings can only be attached to
# mpv's own commands and cannot fire a Python callback (calling bindkey via
# command() errors with -4), so we detour through user-data: a click sets it to
# 1 and we clear it once observed. Clearing is mandatory because mpv does not
# notify again when the same value is reassigned -- only the first click would
# be recognized. See mouse_observer in start_player.
RC_KEY = "right-click"
LC_KEY = "left-click"

# The wheel jumps by subtitle rather than by a fixed number of seconds:
# sub-seek counts "subtitle entries", not seconds -- sub-seek 1 is the next
# entry, -1 the previous one, 0 the start of the current one. mpv seeks on the
# subtitle track directly; the subtitle file is still loaded as usual (just
# sub_visibility=no, so it does not draw it itself).
#
# The left button is not bound to cycle pause but only sets a flag: a click on
# the progress bar must become a seek and a click elsewhere a pause -- only
# decidable once the two are judged in Python, so no verdict is drawn here (see
# handle_left_click).
#
# s cycles the subtitle track (not sub-visibility). sub_visibility=no only
# switches off mpv's own drawing; our SubtitleHover keeps drawing from
# player.sub_text, so "subtitles off" does not really change the picture --
# what matters is switching tracks: external en.srt <-> the Chinese track
# embedded in the mkv. cycle sub rotates between "no subtitles" and every
# subtitle track; in practice it is three states: en.srt -> none -> Chinese
# (Simplified) -> en.srt -> ... To jump straight back and forth between the two
# tracks you have to pick the track yourself in Python (cycle sub cannot do
# it).
RC_BINDING = f"""mbtn_right set user-data/{RC_KEY} 1
mbtn_left set user-data/{LC_KEY} 1
wheel_up sub-seek 1
wheel_down sub-seek -1
s cycle sub
"""

# Key names log_rc_bindings cross-checks at startup (mpv normalizes
# mbtn_*/wheel_* to upper case)
RC_KEYS_TO_LOG = ("MBTN_RIGHT", "MBTN_LEFT", "WHEEL_UP", "WHEEL_DOWN", "s")


# Geometry of the OSD bar. The defaults come from the mpv 0.41 man page
# (--osd-bar-w/h, --osd-bar-align-x/y, --osd-bar-outline-size); the real values
# are always read from the options, so a changed default can never be
# miscomputed. The copy kept here is only a fallback when a read fails.
OSD_BAR_DEFAULTS = {
    "w": 75.0,
    "h": 3.125,
    "align-x": 0.0,
    "align-y": 0.5,
    "outline-size": 0.5,
}


def _osd_bar_opt(player, key):
    try:
        return float(player[f"osd-bar-{key}"])
    except Exception:
        return OSD_BAR_DEFAULTS[key]


def _osd_bar_rect(player):
    """(left, top, w, h) of the OSD bar in OSD coordinates, or None if
    unavailable.

    Where it lands is decided entirely by the osd-bar-* options: align-x/y
    are relative positions in -1..1 (-1 is top/far left, 1 is bottom/far
    right), w/h are percentages.
    """
    try:
        dims = player.osd_dimensions
    except Exception:
        return None
    if not dims or not dims.get("w") or not dims.get("h"):
        # The VO is not up yet: osd_dimensions is all 0.
        return None
    w = dims["w"] * _osd_bar_opt(player, "w") / 100
    h = dims["h"] * _osd_bar_opt(player, "h") / 100
    cx = dims["w"] * (_osd_bar_opt(player, "align-x") + 1) / 2
    cy = dims["h"] * (_osd_bar_opt(player, "align-y") + 1) / 2
    return cx - w / 2, cy - h / 2, w, h


def hit_osd_bar(player, x, y):
    """Whether the click at (x, y) landed on the OSD progress bar that pops up
    on seek.

    That bar is a piece of ASS text rendered by libass; mpv core has no
    notion of "which OSD element was hit", so the bar itself is not
    clickable. But where it lands is predictable, so we can compute it
    ourselves -- cleaner than fighting OSC: OSC's click handling is
    `MBTN_LEFT -> osc/__keybinding5` with priority -1 while the binding
    here is 15, and for the same key the higher priority wins outright, so
    even with OSC shown, clicking its bar does nothing at all.

    The coordinates are OSD coordinates, the same set as mouse-pos, so they
    compare directly.
    """
    rect = _osd_bar_rect(player)
    if rect is None:
        return False
    left, top, w, h = rect
    # the outline makes the bar slightly larger than the rect, so grow the hit
    # area too, or the last few pixels never hit
    pad = _osd_bar_opt(player, "outline-size") * 2
    return left - pad <= x <= left + w + pad and top - pad <= y <= top + h + pad


def osd_bar_fraction(player, x):
    """Convert an x coordinate on the OSD bar into playback progress in 0..1.

    0 when the VO is not ready.
    """
    rect = _osd_bar_rect(player)
    if rect is None:
        return 0.0
    left, _top, w, _h = rect
    return min(1.0, max(0.0, (x - left) / w)) if w else 0.0


# Between two polls time-pos normally advances by only POLL_MS/1000 seconds. A
# jump larger than that counts as a seek having happened (wheel sub-seek, a
# progress bar click, one sent over from Emacs -- anything that moves the time
# point discontinuously) and is used as the signal that the OSD bar popped up.
SEEK_JUMP_MIN = 1.0


class OsdBarVisibility:
    """Whether the OSD progress bar is showing at this moment.

    mpv has no readable "is the bar showing now" property: --osd-on-seek
    only decides whether it shows on seek, how long it stays is
    --osd-duration, and mpv reports neither (osd-level is an option value,
    not live state, and properties like osd-text / osd-visible do not exist
    at all). So we keep the books ourselves: the polling thread notes the
    expiry on a time-pos jump, the click side uses it.

    Only a bar that really is showing counts as a bar click, otherwise a
    click in that area stays pause/play -- otherwise clicking empty space
    would mysteriously jump the position.

    The polling thread writes it and the click thread reads it -- an
    ordinary attribute: assignment is atomic in Python, a slightly stale
    read is off by at most one poll (40 ms), which does not matter.
    """

    def __init__(self, player):
        self.player = player
        # A monotonic timestamp; 0 means the bar has never popped up.
        self.deadline = 0.0
        self._last_pos = None

    def visible(self):
        # If osd-on-seek has no bar in it, a seek never pops one up, so it must
        # never be judged a bar hit. A failed read counts as False too: better
        # that this click falls back to "pause".
        try:
            if "bar" not in str(self.player["osd-on-seek"]):
                return False
        except Exception:
            return False
        return time.monotonic() < self.deadline

    def poll(self, pos):
        """Called once per poll; pos is the current time-pos."""
        if pos is None:
            return
        if self._last_pos is not None and abs(pos - self._last_pos) > SEEK_JUMP_MIN:
            self.deadline = time.monotonic() + self._duration_ms() / 1000
        self._last_pos = pos

    def _duration_ms(self):
        try:
            return float(self.player["osd-duration"])
        except Exception:
            return 1000.0


def start_player(path, sub_file, loop, hooks):
    """Main thread: Qt supplies NSApplication, so mpv's VO gets a window."""
    app = QApplication(sys.argv)
    # Constructing QApplication resets the locale, so it has to be set again
    # after that and before creating the MPV; otherwise libass refuses to work
    # and mpv's VO segfaults (exit 139).
    locale.setlocale(locale.LC_NUMERIC, "C")

    options = {
        "border": "no",
        "title_bar": "no",
        "osd_bar": "yes",
        # Start out fullscreen. This is a startup option, so handing it to
        # MPV(**options) on the command line is safest; to leave fullscreen at
        # runtime use f (or player.command("set", "fullscreen", "no")).
        "fullscreen": "yes",
        # We render the subtitles ourselves, so switch off mpv's, otherwise
        # they double up
        "sub_visibility": "no",
    }
    if sub_file:
        options["sub_files"] = sub_file

    # Key bindings can only be given before startup: --input-conf is a startup
    # option, and mpv's bindkey command cannot be invoked dynamically via
    # command() (parsing just errors with -4). A file is more dependable than
    # the command line.
    conf_dir = tempfile.mkdtemp(prefix="mpvsub-conf-")
    conf_path = os.path.join(conf_dir, "input.conf")
    with open(conf_path, "w") as f:
        f.write(RC_BINDING)
    options["input_conf"] = conf_path

    player = MPV(log_handler=print, loglevel="debug", **options)

    def log_rc_bindings():
        """After startup, print the mouse key bindings to confirm input.conf
        really was read in.

        mpv normalizes mbtn_* to upper case, and --input-conf replaces the
        default config wholesale (it does not append), so these bindings
        can fail to load for any number of reasons while mpv reports no
        error at all. Look at these lines first when a key does not work.

        The only way to read them is player._get_property:
        command("get_property", ...) in python-mpv always errors with -4
        (the argument is packed as STRING, mpv wants an OSD string).
        """
        try:
            binds = player._get_property("input-bindings") or []
            for key in RC_KEYS_TO_LOG:
                hit = [b for b in binds if isinstance(b, dict) and b.get("key") == key]
                sub_log(f"{key} 绑定 {len(hit)} 条: {hit}")
        except Exception:
            import traceback

            traceback.print_exc()

    def clear_flag(key):
        """Clear a mouse key's flag back to the empty string. See the
        RC_BINDING notes.

        Only its own: when the left and right flags are set at the same
        time (a double click, say), clearing both would swallow the other
        click.
        """
        try:
            player.command("set", f"user-data/{key}", "")
        except Exception:
            import traceback

            traceback.print_exc()

    # The observer is registered further down, but hover is only built by hooks
    # after play(). Give it a None for now, so the closure always reads a bound
    # name and never hits UnboundLocalError.
    hover = None
    osd_bar = OsdBarVisibility(player)

    @player.property_observer("user-data")
    def mouse_observer(_name, value):
        """Single entry point for left/right clicks: see which flag was set,
        then handle them apart.

        This callback runs on mpv's own event thread and must never wait on
        mpv synchronously here (the reply to command() is dispatched by
        that same thread, so waiting synchronously is waiting forever), so
        the real handling is always handed off to a separate thread, see
        clear_flag.
        """
        # Printed unconditionally: every change to user-data fires the callback
        # (mpv itself stuffs things like osc keys in there), and when the mouse
        # keys do not work this one line tells you whether
        # "the VO never delivered the click" or
        # "it was delivered but did not land".
        sub_log(f"user-data 变动: {value}")
        value = value or {}
        if value.get(RC_KEY) == "1":
            threading.Thread(target=on_right_click, daemon=True).start()
        if value.get(LC_KEY) == "1":
            threading.Thread(target=handle_left_click, daemon=True).start()

    def handle_left_click():
        """Left click: if the bar is showing and the click is on it, seek
        there; otherwise pause/play.

        Why the round trip is unavoidable: the input.conf bindings do the
        same thing for both keys (set the flag), and "seek" versus "pause"
        depends on where the mouse landed and whether the bar is showing,
        which can only be decided back in Python.
        """
        # Clear the flag unconditionally first: it must be reset even when this
        # click is not handled, or mpv will not notify on the next one
        clear_flag(LC_KEY)
        try:
            pos = player.mouse_pos or {}
            # mouse-pos is a node map with keys x/y/hover. When hover is false
            # the coordinates are stale (the VO only updates them while the
            # pointer is over the window); don't judge then, treat it as a
            # plain click.
            if pos.get("hover"):
                # Already OSD coordinates.
                x, y = float(pos["x"]), float(pos["y"])
            else:
                x = y = None
            # While the bar is not showing that area is ordinary and a click is
            # still a pause, so both conditions have to hold before it counts
            # as a click on the bar.
            if x is not None and osd_bar.visible() and hit_osd_bar(player, x, y):
                duration = player.duration
                if not duration:
                    sub_log("进度条点击：拿不到 duration，忽略")
                    return
                frac = osd_bar_fraction(player, x)
                player.command("seek", frac * duration, "absolute", "exact")
                sub_log(f"进度条点击 {frac:.3f} -> {frac * duration:.1f} 秒")
                return
            player.pause = not player.pause
            sub_log(f"左键（没点中可见的进度条）-> pause={player.pause}")
        except Exception:
            import traceback

            traceback.print_exc()

    def on_right_click():
        """A right click landed on a subtitle: report that line to Emacs."""
        clear_flag(RC_KEY)
        try:
            if hover is None:
                return
            target = hover.cursor_target()  # same lookup path as hover
            if target is None:
                sub_log("右键不在字幕上，忽略")
                return
            sub_log(
                f"右键命中字幕: {target['sub'][:60]!r} @ {target['x']},{target['y']} "
                f"词={target['word']!r}"
            )
            hooks.on_right_click(
                target["sub"], target["word"], target["x"], target["y"]
            )
        except Exception:
            import traceback

            traceback.print_exc()

    # time-pos changes every frame, so only report on whole-second crossings.
    last = -1.0

    @player.property_observer("time-pos")
    def time_observer(_name, value):
        nonlocal last
        if value is None or loop is None:
            return
        if int(value) == int(last):
            return
        last = value
        hooks.on_time(value)

    # player.play(path)
    threading.Thread(target=log_rc_bindings, daemon=True).start()

    # The render object is built by the layer above: it must know
    # SubtitleHover, this module does not, and the two join only through this
    # return value. The call is before app.exec_(), on the Qt main thread, so a
    # QObject is fine.
    hover = hooks.on_ready(player)
    threading.Thread(
        target=_start_hover_watch,
        args=(hover, player, loop, hooks, osd_bar),
        daemon=True,
    ).start()

    try:
        app.exec_()
    finally:
        # each bitmap file is 4 MB; not cleaning them up on exit leaves them
        # piling up in the temp dir
        shutil.rmtree(hover._tmpdir, ignore_errors=True)
        shutil.rmtree(conf_dir, ignore_errors=True)


def _start_hover_watch(hover, player, loop, hooks, osd_bar):
    """Background thread: poll the mouse, decide hits, switch highlighting,
    pause/resume, notify Emacs.

    Seeks are noticed here too, so that osd_bar knows whether the OSD
    progress bar popped up (mpv does not report it, see OsdBarVisibility).
    It rides on the existing poll, no extra thread.
    """
    last_sub = object()
    last_anchor = None
    last_hit = -2
    relayout_dirty = False
    last_relayout = 0.0
    while True:
        time.sleep(POLL_MS / 1000)
        try:
            osd_bar.poll(player.time_pos)
            sub = player.sub_text or ""
            dims = player.osd_dimensions
            anchor_key = (dims["w"], dims["h"])

            if anchor_key != last_anchor:
                last_anchor = anchor_key
                hover.anchor()  # only math, safe to call on this thread
                # Both the bitmap and the rects have to be recomputed.
                relayout_dirty = True

            if sub != last_sub:
                print(sub)
                last_sub = sub
                sub_log(f"sub-text {'有' if sub else '空'}: {sub[:60]!r}")
                # Signal across threads; the main thread lays out and redraws.
                # _on_set_sub clears the rects first, so until it has run
                # hit_test always returns -1 and never judges against the
                # previous one
                hover.set_sub.emit(sub)
                last_hit = -2

            if relayout_dirty and sub:
                # While the window is dragged the size changes every frame and
                # one redraw writes out a 4 MB bitmap, so rate-limit it. The
                # condition does not check whether the size is still changing,
                # so the last one lands after the drag.
                now = time.monotonic()
                if now - last_relayout >= RELAYOUT_MIN_GAP:
                    relayout_dirty = False
                    last_relayout = now
                    # A signal across threads; the main thread redraws.
                    hover.relayout.emit()

            # Both the hit test and how to get
            # "whole sentence + word + coordinates" live in cursor_target, and
            # the right click callback goes through it too, so the two cannot
            # disagree on geometry
            target = hover.cursor_target()
            hit = target["index"] if target else -1
            if hit != last_hit:
                last_hit = hit
                hover.hit = hit
                if hit >= 0:
                    player.pause = True
                    hover._paused_by_hover = True
                    hooks.on_hover_word(
                        target["sub"], target["word"], target["x"], target["y"]
                    )
                elif hover._paused_by_hover:
                    # Only undo the pause we set ourselves, never override a
                    # manual one
                    player.pause = False
                    hover._paused_by_hover = False
                    # Resuming means the mouse has left the subtitle, so the
                    # floating box still hanging there (the result of a right
                    # click lookup) is meaningless now
                    hover.remove_box()
                if last_sub == sub:
                    # A signal across threads; the main thread redraws.
                    hover.set_hit.emit(hit)
        except Exception:
            import traceback

            traceback.print_exc()
            time.sleep(0.5)
