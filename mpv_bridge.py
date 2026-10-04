# /// script
# dependencies = [
#     "sexpdata>=1.0.2",
#     "websocket-bridge-python>=0.0.2",
#     "mpv",
#     "PySide6",
#     "babelfish",
#     "subliminal",
# ]
# ///

import asyncio
import json
import os
import sys
import threading
from datetime import datetime

import sexpdata
import websocket_bridge_python

# This file is the entry point (uv run mpv_bridge.py); subtitle.py and
# mpv_player.py sit in the same directory. Run as a script the script dir is
# already sys.path[0]; inserting again guards a later `import` (tests, say).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mpv_player import RC_KEY, start_player, sub_log  # noqa: E402
from subtitle import SubtitleHover, download_subtitle, srt_path_for  # noqa: E402

# MOVIE = (
#     "/Volumes/NAS/004-tv-series/英文/Neagley/"
#     "Neagley.S01E02.2026.2160p.AMZN.WEB-DL.DDP5.1.Atmos.DV.HDR.H.265.mkv"
# )

# Subtitle file; leave it None to load no subtitles. It is derived from MOVIE
# here (the rule lives in subtitle.srt_path_for), so a new episode only means
# editing MOVIE in one place.
# SUB_FILE = srt_path_for(MOVIE)


# This file only assembles: the Emacs bridge (commands in, notifications out),
# the mpv_player hooks, and the entry point. Rendering lives in subtitle.py,
# the player lifecycle in mpv_player.py.


player = None
hover = None  # SubtitleHover, built in on_ready; draw_box uses it to emit
loop = None  # 1. Declare the global loop variable explicitly
loop_ready = threading.Event()  # player starts only once loop is set up


def handle_arg_types(arg):
    """Turn a Python value into something sexpdata dumps as Lisp."""
    if isinstance(arg, bool):
        return sexpdata.Symbol("t" if arg else "nil")
    if arg is None:
        return sexpdata.Symbol("nil")
    if isinstance(arg, (int, float, str)):
        return arg
    if isinstance(arg, (list, tuple)):
        return [sexpdata.Symbol("list")] + [handle_arg_types(a) for a in arg]
    if isinstance(arg, dict):
        plist = [sexpdata.Symbol("plist")]
        for k, v in arg.items():
            plist.append(sexpdata.Symbol(f":{k}"))
            plist.append(handle_arg_types(v))
        return plist
    return str(arg)


def report_future_error(fut):
    """Nobody awaits the run_coroutine_threadsafe Future.

    Its exceptions are silently swallowed, so report them here.
    """
    if fut.cancelled():
        return
    err = fut.exception()
    if err is not None:
        import traceback

        print("=== handle_time_update failed ===", flush=True)
        traceback.print_exception(type(err), err, err.__traceback__)


async def handle_time_update(value):
    # await video_info()
    await eval_in_emacs("mpv-run-time-update-functions", [value])
    print(f"當前位置: {value:.2f} 秒")


async def handle_hover_word(sub_text, word, x, y):
    """Mouse hovering a word.

    sub_text is the whole current subtitle line, word the one under the cursor.
    """
    sub_log(f"悬浮 {word!r} @ {x},{y}")
    print(f"{_now()}: Hover")
    await eval_in_emacs("mpv-definition-word", [sub_text, word, x, y])


async def handle_right_click_sub(sub_text, word, x, y):
    """Right-clicked a word.

    Same arguments as hovering; the Emacs side uses the same signature.
    """
    sub_log(f"右键 {word!r} @ {x},{y} 整句={sub_text[:60]!r}")
    await eval_in_emacs("mpv-explain-text", [sub_text, word, x, y])


def schedule(coro):
    """Throw the coroutine back onto the bridge's loop to run, with an error
    callback on the Future.

    mpv_player calls us synchronously (mpv thread / polling thread), but
    Emacs notifications must go through the loop: the hooks only call, the
    real scheduling is centralized in this one function.
    """
    fut = asyncio.run_coroutine_threadsafe(coro, loop)
    fut.add_done_callback(report_future_error)


class MpvHooks:
    """Everything mpv_player needs from the layer above is taken from here.

    mpv_player does not import this module (it would import e_mpv in a
    circle), so the wiring goes through this duck-typed object; all four
    methods are called synchronously on mpv_player's threads.
    """

    def on_ready(self, player_obj):
        """Called once the player is built and playing.

        Returns the render object, which mpv_player uses to start the polling
        thread.
        """
        global player, hover
        player = player_obj
        hover = SubtitleHover(player, loop)
        return hover

    def on_hover_word(self, sub_text, word, x, y):
        schedule(handle_hover_word(sub_text, word, x, y))

    def on_right_click(self, sub_text, word, x, y):
        schedule(handle_right_click_sub(sub_text, word, x, y))

    def on_time(self, pos):
        schedule(handle_time_update(pos))


def draw_box(text, x, y):
    """Draw a box just above the x y coordinates, with text inside it.

    x y is the midpoint directly below the box (slightly lower than the box
    itself). The landing point is lifted upward so the box does not cover
    the subtitle -- see SubtitleHover._box_bottom. The box's width and
    height adapt to text.

    An empty text removes the box -- an overlay, once added, stays around
    unless overwritten with the same id or removed explicitly, which is
    mpv's semantics, borrowed as the "off" switch.

    These are OSD coordinates, the same set mouse-pos uses, so a mouse
    position can serve as the anchor. Only a signal is emitted: QPainter
    must run on the Qt main thread, on_message on the bridge thread.
    """
    if hover is None:
        return
    try:
        hover.box.emit(str(text), int(float(x)), int(float(y)))
        print(f"{_now()} : Finish")
    except (TypeError, ValueError):
        import traceback

        traceback.print_exc()


def _prop(name, default=None):
    """Read an mpv property; any failure falls back to default.

    You cannot write player.command("get_property", name):
    python-mpv's command() packs the argument as MPV_FORMAT_STRING, while mpv's
    get_property command wants an OSD string, so the types never match and
    it always reports -4. _get_property uses the C API, types consistent.

    It fails in two ways: a nonexistent property raises AttributeError
    (-8), and a file not loaded yet returns None (PropertyUnavailable is
    swallowed by it itself); both fold into default.
    """
    try:
        value = player._get_property(name)
    except Exception:
        return default
    return default if value is None else value


def _file_loaded_waiter():
    """Subscribe to file-loaded, returning (waiter coroutine, unsubscribe);
    must be called before player.play().

    play() is asynchronous: it returns as soon as the command is sent, the
    actual stream open and demuxing happen on mpv's own thread. Until then
    video-params / duration / track-list are all property unavailable (only
    media-title reads right away, being filled in from the file name).
    file-loaded is the "now readable" signal.

    The callback runs on mpv's event thread and must not touch the asyncio
    Future (it is not thread safe), so it is handed back to the bridge's
    loop via call_soon_threadsafe.
    """
    loop = asyncio.get_running_loop()
    fut = loop.create_future()

    @player.event_callback("file-loaded")
    def _loaded(_event):
        loop.call_soon_threadsafe(lambda: not fut.done() and fut.set_result(True))

    async def wait(timeout=10.0):
        try:
            await asyncio.wait_for(fut, timeout)
            return True
        except asyncio.TimeoutError:
            print("等 file-loaded 超时，文件多半打不开", flush=True)
            return False

    return wait, _loaded.unregister_mpv_events


async def _play_and_report(file):
    """Switch to file, wait for it to load, attach the external subtitle, then
    print the file info.

    The whole thing runs in a create_task rather than being awaited
    directly in on_message: waiting for file-loaded takes up to several
    seconds, and awaiting it directly blocks the bridge's message loop,
    queueing other Emacs commands.
    """
    # Subscribe first, then play; never the reverse.
    wait_loaded, cancel = _file_loaded_waiter()
    try:
        player.play(file)
        if not await wait_loaded():
            return
        print("fuck")
        srt = srt_path_for(file)
        if srt:
            # Attach after loading: the external en.srt is a new
            # track, and mpv selects it automatically. It used to
            # read `if str:` -- str is a builtin type, always
            # truthy, so the subtitle was set to "<class 'str'>", a nonexistent
            # file: never actually attached.
            player.sub_file = srt
            sub_log(f"外挂字幕: {srt}")
        else:
            sub_log(f"没找到外挂字幕: {file}")
        await video_info()
    finally:
        cancel()


async def video_info():
    """Print the details of the currently playing file to mpv's buffer.

    on_message runs on the bridge thread, not the Qt main thread, so
    issuing mpv commands synchronously is safe (on the Qt main thread it
    segfaults; see the comment in mpv_player.start_player).
    """
    if player is None:
        print("播放器还没起来", flush=True)
        return None
    vp = _prop("video-params", {}) or {}
    ap = _prop("audio-params", {}) or {}
    dur = _prop("duration") or 0.0
    fps = _prop("container-fps") or 0.0
    # The bitrate can only be summed by hand from the mkv BPS tag of each track
    # in track-list: when mkv has no avg bitrate tag, video-bitrate /
    # audio-bitrate are property unavailable.
    bps = [
        int(t["metadata"]["BPS"]) // 1000
        for t in _prop("track-list", []) or []
        if t.get("type") in ("video", "audio") and t.get("metadata", {}).get("BPS")
    ]
    # The keys are the names mpv-vui.el looks up with plist-get. A plist
    # returns the first match for a key, so a repeated key silently shadows
    # the earlier value: the height used to be sent as "duration" too, which
    # made it unreachable.
    await eval_in_emacs(
        "mpv-update-video-info",
        [
            "title",
            _prop("media-title"),
            "path",
            _prop("path"),
            "duration",
            dur,
            "width",
            vp.get("w"),
            "height",
            vp.get("h"),
            "fps",
            fps,
        ],
    )


def _now():
    now = datetime.now()
    return now.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


# dispatch message received from Emacs.
async def on_message(message):
    try:
        info = json.loads(message)
        print(f"{_now()} : {info}")
        cmd = info[1][0].strip()
        if cmd == "plause":
            player.pause = True
        elif cmd == "resume":
            player.pause = False
        elif cmd == "play":
            # Don't play and fetch the info in place here: the file is not
            # loaded yet when player.play() returns; see the note in
            # _play_and_report.
            asyncio.create_task(_play_and_report(info[1][1]))
        elif cmd == "show-box":
            text = info[1][1]
            x = info[1][2]
            y = info[1][3]
            draw_box(text, x, y)
        elif cmd == "video-info":
            await video_info()
        elif cmd == "download-subtitle":
            draw_box("downloading subtitle...", 100, 100)
            download_subtitle(info[1][1])
            draw_box("downloaded subtitle.", 100, 100)
        else:
            print(f"not fount handler for {cmd}", flush=True)
    except Exception as _:
        import traceback

        print(traceback.format_exc())


async def main():
    global bridge, loop
    loop = asyncio.get_running_loop()
    bridge = websocket_bridge_python.bridge_app_regist(on_message)
    # Release the player only once loop and bridge are ready, or loop is None
    # on the first time-pos callback.
    loop_ready.set()
    await asyncio.gather(init(), bridge.start())


async def get_emacs_var(var_name: str):
    "Get Emacs variable and format it."
    var_value = await bridge.get_emacs_var(var_name)
    if isinstance(var_value, str):
        var_value = var_value.strip('"')
    print(f"{var_name} : {var_value}")
    if var_value == "null":
        return None
    return var_value


async def init():
    "Init User data."
    print("Init")


async def eval_in_emacs(method_name, args):
    args = [sexpdata.Symbol(method_name)] + list(map(handle_arg_types, args))  # type: ignore
    sexp = sexpdata.dumps(args)
    # print(sexp)
    await bridge.eval_in_emacs(sexp)


if __name__ == "__main__":
    # The websocket bridge runs off the main thread, leaving it to AppKit.
    threading.Thread(target=lambda: asyncio.run(main()), daemon=True).start()
    loop_ready.wait()
    start_player(None, None, loop, MpvHooks())
