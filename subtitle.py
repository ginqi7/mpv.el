"""Subtitles: where the file is, how the text is split, and how it all gets
rendered.

mpv only exposes sub-text (plain text); libass's typeset result is not
exposed and there are no per-word bounding boxes, so hover can't be done
against mpv's own subtitles. We take rendering over: measure each word with
QFontMetricsF, lay it out ourselves, paint a bitmap with alpha, and put it
over mpv as an OSD overlay -- we own the geometry, and only then does hit
testing line up.

Dependencies run one way only: mpv_player.py <- this file <- mpv_bridge.py.
This module touches exactly one mpv object, the player (reads sub-text /
mouse-pos / osd-dimensions, sends overlay-add), and doesn't care how the
player was started.
"""

import os
import re
import time
import tempfile
import threading

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QImage, QPainter, QPen
from PySide6.QtWidgets import QApplication

from mpv_player import sub_log
from pathlib import Path


def download_subtitle(url):
    """Subtitle download function executed in multiple processes"""
    from babelfish import Language
    from subliminal import (
        download_best_subtitles,
        region,
        save_subtitles,
        scan_video,
    )

    try:
        # Configure Cache
        region.configure(
            "dogpile.cache.dbm",
            arguments={"filename": "cachefile.dbm"},
            replace_existing_backend=True,
        )

        # Scan Video
        video = scan_video(url)

        # Download the best subtitles.
        subtitles = download_best_subtitles([video], {Language("eng")})

        # Save subtitles to disk
        save_subtitles(video, subtitles[video])
        print("Subtitle download complete.")

    except Exception as e:
        print(f"Error downloading subtitles: {e}")


def srt_path_for(video_path):
    """ """
    SUB_EXTENSIONS = {".srt", ".ass", ".vtt", ".sub", ".ssa", ".idx"}
    video = Path(video_path)
    video_stem = video.stem  # stem name, e.g. "Movie.2023"
    target_dir = video.parent
    matched_subtitles = []

    # walk every file in the target directory
    for file in target_dir.iterdir():
        # 1. must be a file, and carry a subtitle extension
        if file.is_file() and file.suffix.lower() in SUB_EXTENSIONS:
            # 2. check the subtitle name starts with the video's stem
            # e.g. both "Movie.2023.srt" and "Movie.2023.zh-CN.ass" match
            if file.name.startswith(video_stem):
                matched_subtitles.append(file)

    return matched_subtitles[0] if matched_subtitles else None


def tokenize(sub_text):
    """Split the subtitle into [[token, ...], ...], cutting on spaces.

    Subtitles are all English, so splitting on spaces is enough. If we ever
    want Chinese subtitles this has to switch to per-character splitting,
    otherwise a whole Chinese sentence becomes one giant token and hovering
    over it is meaningless.
    """
    lines = []
    for line in sub_text.split("\n"):
        toks = line.split()
        if toks:
            lines.append(toks)
    return lines


# max render width of the subtitle bitmap, in CSS pixels. Anything wider
# renders at this width and is scaled up by mpv, capping the memory cost of
# every repaint -- full-width 4K is 18 MB per repaint.
MAX_RENDER_W = 1920

# mpv's overlay id range is [0, 63]; reusing the same id is an update. Three
# layers: the subtitle strip (only moves when the line changes), the
# highlighted word (only moves when hover picks another word), and the floating
# box. The highlight gets its own layer because it updates two orders of
# magnitude more often, while the whole strip is 3 MB.
OVERLAY_ID = 1  # subtitle strip
BOX_ID = 2  # the floating box draw_box draws, independent of the strip
HIT_ID = 3  # the one word currently highlighted

# draw_box's appearance. Every size is given as a ratio of the font size, so it
# looks the same when the font size changes with resolution. The box is only a
# hint next to the mouse, one step smaller than the subtitle: BOX_FONT_SCALE is
# relative to the subtitle font size, so <1 means smaller.
BOX_FONT_SCALE = 0.62
BOX_PAD = 0.5  # padding around the text, a ratio of the font size
# how far the bottom lifts off the anchor, so it doesn't cover the word under
# the cursor
BOX_LIFT = 0.6
BOX_STRIP_GAP = 10  # gap left when lifted above the strip, in OSD pixels
BOX_BG = "#000000c8"  # translucent black background
BOX_EDGE = "#ffd54f"  # border, the same yellow as the hover highlight
BOX_FG = "#ffffff"
# Markdown emphasis rides on colour rather than on weight. The box font is a
# kai face with no bold, and Qt would only fake one (see _font); a smeared
# synthetic weight is worse than no weight. Italic is synthesised too, but a
# slant stays legible where the weight would not.
BOX_BOLD = BOX_EDGE
BOX_CODE_FAMILY = ["Menlo", "PingFang SC", "Arial"]
# The inline markdown the box renders. Bold comes before italic in the
# alternation so ** wins on **x**.
BOX_INLINE_MD = re.compile(r"(\*\*.+?\*\*|\*[^*\n]+?\*|`[^`\n]+?`)")
# A heading is the one piece of block structure the box does take, because it
# is a property of the whole line and costs nothing: the line just gets a
# bigger font. Lists and fenced code are still absent -- those need a layout
# to hang an indent on, and there isn't one here.
# The space after the #'s is required, so "#1" and "#tag" stay literal.
BOX_HEADING = re.compile(r"^(#{1,6})[ \t]+(.*)$")
# how much bigger each level is, h1 through h6. A heading also drops its
# #'s: they are the syntax, not the text.
BOX_HEADING_SCALES = (1.7, 1.5, 1.35, 1.22, 1.12, 1.05)

# the render area reserves height for "at most this many lines", and the font
# size is derived from the video height instead -- otherwise a two-line
# subtitle overflows the strip and gets clipped (deriving the font size back
# from the strip height would let the line count drive it).
MAX_SUB_LINES = 2
SUB_FONT_RATIO = 0.06  # single line height as a percentage of video height
SUB_LINE_H = 1.45  # line height / font size, a value tuned for PingFang SC

HIT_BG = "#ffd54f"  # the word the hover hits is highlighted in this color
# how much the hit area is widened past the glyph, to make it easier to hit
HIT_PAD = 0.12

# fonts. The strip and the box use two sets: a serif for the body, a kai for
# the hint box, so the two read as levels in a card. The fallback families
# cover characters that have no glyph -- Alegreya has no CJK, so Chinese in a
# subtitle falls back character by character.
SUB_FONT_FAMILY = ["Alegreya", "PingFang SC", "Helvetica Neue", "Arial"]
BOX_FONT_FAMILY = ["仓耳今楷04", "PingFang SC", "Arial"]


# the ring: draw the text in black at two radii first, then the white body on
# top, which amounts to a black outline around the subtitle. Video backgrounds
# vary in brightness; without the outline the subtitle is unreadable over
# bright frames.
_RING = [
    (dx * r, dy * r)
    for r in (2, 1)
    for dx in (-1, 0, 1)
    for dy in (-1, 0, 1)
    if (dx, dy) != (0, 0)
]


def box_runs(line):
    """Split one line of draw_box's text into (text, style) runs.

    style is "" plain, "b" bold, "i" italic, "c" code, and the markers
    themselves are dropped. Everything BOX_INLINE_MD does not match is
    carried through untouched, so a lone '*' or a half-written '**' costs
    nothing and still comes out as the characters it was.
    """
    runs = []
    for part in BOX_INLINE_MD.split(line):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            runs.append((part[2:-2], "b"))
        elif (
            part.startswith("*")
            and part.endswith("*")
            and len(part) > 2
            # "****" matches the italic alternative with "**" as its content,
            # which is not italic markup at all. A real italic body never
            # starts or ends on a star, so require that.
            and not part[1:-1].startswith("*")
            and not part[1:-1].endswith("*")
        ):
            runs.append((part[1:-1], "i"))
        elif part.startswith("`") and part.endswith("`") and len(part) > 2:
            runs.append((part[1:-1], "c"))
        else:
            runs.append((part, ""))
    return runs or [("", "")]


def box_line(line):
    """Split one line of draw_box's text into (scale, runs).

    scale is the font multiplier the whole line is drawn at -- 1.0 for
    ordinary text, more for a markdown heading -- and runs are box_runs's
    (text, style) pairs. Keeping the two apart is what lets a heading hold
    **bold** inside it without the bold having to pick a size of its own.
    """
    m = BOX_HEADING.match(line)
    if m:
        return BOX_HEADING_SCALES[len(m.group(1)) - 1], box_runs(m.group(2))
    return 1.0, box_runs(line)


class SubtitleHover(QObject):
    """Subtitle rendering plus mouse hit testing.

    mpv only exposes sub-text (plain text); libass's typeset result is not
    exposed and there are no per-word bounding boxes, so hover can't be
    done against mpv's own subtitles. We take rendering over: measure each
    word with QFontMetricsF, lay it out ourselves, paint a bitmap with
    alpha, and put it over mpv as an OSD overlay -- we own the geometry,
    and only then does hit testing line up.

    Rendering was HTML in a QWebEngineView plus grab(), but grab() can't
    get alpha: by default the image is opaque (the overlay is a white slab
    over the frame) and WA_TranslucentBackground makes the content vanish.
    A QPainter onto a transparent-filled QImage works fine.
    """

    # new subtitle text -> main thread lays out and repaints
    set_sub = Signal(str)
    set_hit = Signal(int)  # highlight token #n -> main thread repaints
    # window resized -> main thread repaints with new geometry
    relayout = Signal()
    bitmap = Signal(bytes, int, int)  # BGRA pixels -> worker does overlay-add
    # draw_box's text/x/y -> main thread draws the box
    box = Signal(str, int, int)

    def __init__(self, player, loop):
        super().__init__()
        self.player = player
        self.loop = loop
        self.tokens = []
        self.lines = []
        self.rects = []
        self.hit = -1
        self.css_w = 0
        self.css_h = 0
        # display size of the render area in the OSD = css * scale
        self.disp_w = 0
        self.disp_h = 0
        # _paint needs it before anchor() first runs, so seed it
        self.font_size = 0
        self.scale = 1.0
        self.origin = (0, 0)
        self._lock = threading.Lock()
        self._paused_by_hover = False
        # every repaint gets a brand new filename. mpv mmaps the bitmap file,
        # and rewriting it with "wb" truncates the file to 0; the hole the old
        # mapping points at, read on the next frame, is a SIGBUS. Old files are
        # unlinked only after the overlay has swapped out -- unlink does not
        # disturb a mapping that is already established.
        self._tmpdir = tempfile.mkdtemp(prefix="mpvsub-")
        self._seq = 0
        # overlay id -> the bitmap file that overlay is using now
        self._cur = {}
        # highlight index already painted, to avoid repainting
        self._painted_hit = None

        self.set_sub.connect(self._on_set_sub)
        self.set_hit.connect(self._on_set_hit)
        self.relayout.connect(self._on_relayout)
        self.bitmap.connect(self._on_bitmap)
        self.box.connect(self._on_box)

    # ------------------------------------------------------------- layout

    def _font(self, size=None, box=False):
        """The strip uses Alegreya (serif), the floating box uses 仓耳今楷04
        (kai).

        The family name 仓耳今楷04 is registered under exactly these five
        characters and W04 is its style name (Family: 仓耳今楷04 / Style: W04);
        putting anything else in setFamilies fails to match and silently
        falls back to the default font.

        Subtitle strings mix Chinese and English: Alegreya has only Latin
        glyphs, so Chinese falls back to PingFang SC character by
        character. The fallback list has to stay -- we can't write Alegreya
        alone.
        """
        f = QFont()
        f.setFamilies(BOX_FONT_FAMILY if box else SUB_FONT_FAMILY)
        f.setPixelSize(size or self.font_size or 42)
        # The kai face has no bold, so the box is not bolded (Qt would
        # just fake it).
        if not box:
            f.setWeight(QFont.Weight.DemiBold)
        return f

    def _fit_font_size(self):
        """Scale the font to the widest line.

        English sentences are much longer than Chinese, so a fixed font size
        runs off screen.
        """
        fs = self.font_size or 42
        if not self.css_w or not self.lines:
            return fs
        for _ in range(4):
            fm = QFontMetricsF(self._font(fs))
            gap = fs * 0.25
            widest = max(
                (
                    sum(fm.horizontalAdvance(t) for t in line)
                    + gap * max(0, len(line) - 1)
                    for line in self.lines
                ),
                default=0,
            )
            if widest <= self.css_w:
                return fs
            fs = max(12, int(fs * self.css_w / widest))
        return fs

    def _layout(self, fs):
        """Measure each word and lay it out ourselves. Returns [[(token, x, y,
        w, h), ...], ...], one group per line.

        We don't use QTextDocument's fragment rects: PySide6 doesn't expose
        formats(). Measuring ourselves is more direct anyway -- the spacing
        between words is ours to choose, so the rects and the actual draw
        positions necessarily agree and hit testing can't drift.
        """
        fm = QFontMetricsF(self._font(fs))
        gap = fs * 0.25
        lh = fm.height()
        rows = []
        for line in self.lines:
            widths = [fm.horizontalAdvance(t) for t in line]
            rows.append((line, widths, sum(widths) + gap * max(0, len(line) - 1)))
        y = max(0.0, (self.css_h - lh * len(rows)) / 2)
        laid = []
        for line, widths, lw in rows:
            x = (self.css_w - lw) / 2
            items = []
            for t, w in zip(line, widths):
                items.append((t, x, y, w, lh))
                x += w + gap
            laid.append(items)
            y += lh
        return laid

    def _paint(self, hit):
        """Paint the current subtitle into a transparent-background bitmap,
        recording each word's rect along the way.

        This layer draws only the text, not the highlight: the highlight
        gets a layer of its own (HIT_ID), because hover changing a word
        happens far more often than repainting the whole line -- 3 MB vs a
        few dozen KB.
        """
        fs = self._fit_font_size()
        dpr = 2.0 if QApplication.primaryScreen().devicePixelRatio() > 1 else 1.0
        w = max(1, round(self.css_w * dpr))
        h = max(1, round(self.css_h * dpr))
        img = QImage(w, h, QImage.Format_ARGB32_Premultiplied)
        img.setDevicePixelRatio(dpr)
        # fill the whole image transparent first; this step is what makes alpha
        # work
        img.fill(Qt.transparent)
        pt = QPainter(img)
        pt.setRenderHint(QPainter.Antialiasing, True)
        pt.setRenderHint(QPainter.TextAntialiasing, True)
        f = self._font(fs)
        pt.setFont(f)
        fm = QFontMetricsF(f)
        pad = fs * HIT_PAD
        baseline = fm.ascent()
        rects = []
        idx = 0
        for items in self._layout(fs):
            for t, x, y, tw, th in items:
                pt.setBrush(Qt.NoBrush)
                pt.setPen(QColor("#000000"))
                for dx, dy in _RING:
                    pt.drawText(QPointF(x + dx, y + baseline + dy), t)
                pt.setPen(QColor("#ffffff"))
                pt.drawText(QPointF(x, y + baseline), t)
                rects.append({"t": t, "x": x - pad, "y": y, "w": tw + 2 * pad, "h": th})
                idx += 1
        pt.end()
        with self._lock:
            self.rects = rects
        # premultiplied can't go to mpv directly, blending needs
        # non-premultiplied alpha
        argb = img.convertToFormat(QImage.Format_ARGB32)
        # Format_ARGB32 is BGRA in memory on a little-endian machine, mpv's fmt
        data = bytes(argb.constBits())
        self._painted_hit = hit
        self._paint_hit(fs, hit, rects)
        sub_log(
            f"paint hit={hit} 字号={fs} 位图={argb.width()}x{argb.height()} "
            f"bytes={len(data)} 词={len(rects)}"
        )
        self.bitmap.emit(data, argb.width(), argb.height())

    def _paint_hit(self, fs, hit, rects):
        """Paint the highlighted word into a small bitmap on its own overlay
        layer.

        Besides the yellow background this layer must draw the word again
        (black ring + white text), or the yellow covers the white and you
        get "white text turned yellow", the same as not seeing it. It also
        means the layer looks the same above or below the strip, so we
        don't bet on mpv's overlay order.
        """
        if hit < 0 or hit >= len(rects):
            self._mpv("overlay-remove", id=HIT_ID)
            self._cur.pop(HIT_ID, None)
            return
        r = rects[hit]
        f = self._font(fs)
        fm = QFontMetricsF(f)
        # margin for the ring and the rounded-corner antialiasing, or edges get
        # clipped flat
        m = fs * 0.1
        cw, ch = r["w"] + 2 * m, r["h"] + 2 * m
        dpr = 2.0 if QApplication.primaryScreen().devicePixelRatio() > 1 else 1.0
        iw, ih = max(1, round(cw * dpr)), max(1, round(ch * dpr))

        img = QImage(iw, ih, QImage.Format_ARGB32_Premultiplied)
        img.setDevicePixelRatio(dpr)
        img.fill(Qt.transparent)
        pt = QPainter(img)
        pt.setRenderHint(QPainter.Antialiasing, True)
        pt.setRenderHint(QPainter.TextAntialiasing, True)
        pt.setFont(f)
        pt.setPen(Qt.NoPen)
        pt.setBrush(QColor(HIT_BG))
        pt.drawRoundedRect(QRectF(m, m, r["w"], r["h"]), 5, 5)
        # The word's top-left sits one pad right of the yellow bg (the
        # rects' x is padded).
        at = QPointF(m + fs * HIT_PAD, m + fm.ascent())
        pt.setBrush(Qt.NoBrush)
        pt.setPen(QColor("#000000"))
        for dx, dy in _RING:
            pt.drawText(at + QPointF(dx, dy), r["t"])
        pt.setPen(QColor("#ffffff"))
        pt.drawText(at, r["t"])
        pt.end()

        argb = img.convertToFormat(QImage.Format_ARGB32)
        # rects are CSS coords relative to the render area, overlay-add wants
        # OSD
        ox = int(round(self.origin[0] + (r["x"] - m) * self.scale))
        oy = int(round(self.origin[1] + (r["y"] - m) * self.scale))
        data = bytes(argb.constBits())
        sub_log(
            f"高亮 {r['t']!r} 位图={argb.width()}x{argb.height()} "
            f"bytes={len(data)} 位置=({ox},{oy})"
        )
        # the display size has to be scaled too, the same factor as the strip
        # -- otherwise a window wider than MAX_RENDER_W leaves the yellow at
        # the original word's OSD size, too small to cover the magnified glyph.
        self._blit(
            data,
            iw,
            ih,
            int(round(cw * self.scale)),
            int(round(ch * self.scale)),
            ox,
            oy,
            HIT_ID,
        )

    # ------------------------------------------------------------- signals

    def _on_set_sub(self, text):
        self.lines = tokenize(text)
        self.tokens = [t for line in self.lines for t in line]
        self.hit = -1
        with self._lock:
            # leaving the old rects hit-tests this frame against the previous
            # line
            self.rects = []
        if not self.tokens:
            # no subtitle, so take the overlay down, or the last line hangs
            # there
            sub_log("字幕为空 -> overlay-remove")
            self._painted_hit = None
            self._mpv("overlay-remove", id=OVERLAY_ID)
            self._mpv("overlay-remove", id=HIT_ID)
            self._cur.pop(HIT_ID, None)
            return
        sub_log(f"排版 {len(self.tokens)} 个 token: {self.tokens[:6]}")
        self._paint(-1)

    def _on_set_hit(self, index):
        if not self.tokens:
            return
        if index == self._painted_hit:
            # a set_hit(-1) follows a line change; that one needn't repaint
            return
        self.hit = index
        # only repaint the highlight layer, the strip stays put. Changing the
        # hovered word is the highest-frequency action; the 3 MB bitmap is
        # drawn once per line change.
        with self._lock:
            rects = list(self.rects)
        start = time.perf_counter()
        self._paint_hit(self._fit_font_size(), index, rects)
        end = time.perf_counter()
        print(f"耗时: {end - start:.6f} 秒")
        self._painted_hit = index

    def _on_relayout(self):
        """The window resized; repaint the current subtitle with the new
        geometry.

        It has to repaint: anchor() changed the font size, strip height,
        center and origin, but the bitmap on screen and the rects still
        come from the old size. Without it the mouse coords convert with
        the new geometry and compare against old rects, so hover is
        guaranteed to be offset.
        """
        if not self.tokens:
            return  # no subtitle means no overlay to update
        sub_log(f"按新几何重画 字号={self.font_size} css={self.css_w}x{self.css_h}")
        self._paint(self.hit)

    # -------------------------------------------------------------- box

    def _box_bottom(self, y, fs, bh):
        """Where the box's bottom edge should land. Returns OSD coordinates.

        The bottom edge doesn't sit on y -- that would put the box under
        the cursor, covering the very line the mouse points at. Normally
        lift it by BOX_LIFT to clear the cursor; if it's still on the strip
        (the anchor is near the subtitle, or the box is tall), lift the
        whole thing above it.
        """
        bottom = y - fs * BOX_LIFT
        strip_top = self.origin[1]
        strip_bot = strip_top + self.css_h * self.scale
        # the box's vertical span [bottom-bh, bottom] meeting the strip is in
        # the way
        if not (bottom - bh < strip_bot and bottom > strip_top):
            return bottom
        lifted = strip_top - BOX_STRIP_GAP
        # Lifting only ever moves the box up, so it has to be checked for
        # actually fitting: a box taller than the space above the strip
        # would go off the top, and the caller's max(0, ...) would then pin
        # it to y=0 -- the screen top, further from the mouse than not
        # lifting at all. In that case keep it above the cursor instead. Its
        # bottom edge is already above the anchor, so it still clears the
        # strip in every case where the lift was wanted.
        if lifted - bh < 0:
            return bottom
        return lifted

    def _on_box(self, text, x, y):
        """Draw an auto-sized box directly above (x, y), containing text.

        The bottom edge is lifted first, see _box_bottom -- mostly so it
        doesn't cover the subtitle.

        A \\n in text is taken as a real line break. The QPainter drawText
        overload taking a QPointF is the single-line version, where \\n is
        just an ordinary character, so we split the lines here and draw
        them one by one.

        Inline markdown is rendered: **bold**, *italic* and `code` become runs
        drawn side by side (see box_runs), and a leading "#" makes the
        whole line bigger (see box_line). Lists and fenced code are not
        rendered.

        The coordinates are OSD ones, the same space as mouse-pos and the
        strip, so the mouse position can be passed straight in as the
        anchor. The box uses its own overlay id and won't displace the
        strip.
        """
        text = str(text)
        # split into lines. \\r\\n and a lone \\r have to be recognised too,
        # otherwise text from Windows leaves an invisible character at the end
        # of the line and widens that line. The blank lines are dropped before
        # the split into runs: a blank line would otherwise survive as one
        # empty run and cost a whole line of height for nothing.
        lines = [ln.replace("\r", "") for ln in text.split("\n")]
        lines = [ln for ln in lines if ln] or [""]
        rows = [box_line(ln) for ln in lines]
        if not text.strip():
            self._mpv("overlay-remove", id=BOX_ID)
            self._cur.pop(BOX_ID, None)
            return
        if not self.css_w:
            # OSD dimensions aren't ready yet (no video), wait for the next
            # round
            return

        fs = max(10, int((self.font_size or 42) * BOX_FONT_SCALE))
        f = self._font(fs, box=True)
        # One font per (row scale, run style), built on demand and kept: a box
        # has a handful of rows and each is drawn nine times over for the
        # ring, so rebuilding these per run would be the whole cost of the
        # draw. Only code changes face and italic leans; bold is a colour,
        # see BOX_BOLD.
        faces = {}
        metrics = {}
        pens = {st: QColor(BOX_BOLD if st == "b" else BOX_FG) for st in ("", "b", "i", "c")}

        def face(scale, style):
            key = (scale, style)
            if key not in faces:
                fnt = QFont(f)
                if scale != 1.0:
                    fnt.setPixelSize(max(1, round(fs * scale)))
                if style == "i":
                    fnt.setItalic(True)
                elif style == "c":
                    fnt.setFamilies(BOX_CODE_FAMILY)
                faces[key] = fnt
                metrics[key] = QFontMetricsF(fnt)
            return faces[key]

        def metric(scale, style):
            face(scale, style)  # fills both dicts
            return metrics[(scale, style)]

        def advance(scale, style, text):
            return metric(scale, style).horizontalAdvance(text)

        pad = fs * BOX_PAD
        # A heading is a taller row, so the rows no longer share one line
        # height and bh is a sum rather than a multiple. Each row's height is
        # the *plain* face at that row's scale: a row keeps one baseline
        # whichever styles sit on it, so a taller code face must not push the
        # rows apart.
        laid = []
        for scale, row in rows:
            fm = metric(scale, "")
            laid.append((scale, row, fm, fm.height()))
        # the size is entirely the text's: width from the widest line, height
        # the sum of the row heights
        bw = (
            max(
                sum(advance(scale, st, t) for t, st in row)
                for scale, row, _, _ in laid
            )
            + 2 * pad
        )
        bh = sum(lh for _, _, _, lh in laid) + 2 * pad
        dpr = 2.0 if QApplication.primaryScreen().devicePixelRatio() > 1 else 1.0
        iw = max(1, round(bw * dpr))
        ih = max(1, round(bh * dpr))

        img = QImage(iw, ih, QImage.Format_ARGB32_Premultiplied)
        img.setDevicePixelRatio(dpr)
        img.fill(Qt.transparent)
        pt = QPainter(img)
        pt.setRenderHint(QPainter.Antialiasing, True)
        pt.setRenderHint(QPainter.TextAntialiasing, True)
        pt.setFont(f)
        pt.setPen(QPen(QColor(BOX_EDGE), max(1.0, fs * 0.04)))
        pt.setBrush(QColor(BOX_BG))
        pt.drawRoundedRect(QRectF(0, 0, bw, bh), fs * 0.25, fs * 0.25)
        # the ring: as for the subtitle, black text at several offsets first,
        # then the white body. The box background is already translucent black,
        # so the edge only serves to "jump out of" it.
        #
        # The offsets are the outer loop so the font and pen are set once per
        # offset instead of once per run -- a markdown line is a handful of
        # runs and the ring has eight offsets. The advance inside a pass is
        # always the run's unshifted width, or the offsets would accumulate
        # down the line instead of ringing it.
        #
        # The cursors are cx and cy, never x and y: those are the anchor this
        # method was called with, and the placement below still needs them.
        black = QColor("#000000")
        pt.setBrush(Qt.NoBrush)
        cy = pad
        for scale, row, fm, lh in laid:
            baseline = cy + fm.ascent()
            for dx, dy in _RING:
                cx = pad
                for t, st in row:
                    pt.setFont(face(scale, st))
                    pt.setPen(black)
                    pt.drawText(QPointF(cx + dx, baseline + dy), t)
                    cx += advance(scale, st, t)
            cx = pad
            for t, st in row:
                pt.setFont(face(scale, st))
                pt.setPen(pens[st])
                pt.drawText(QPointF(cx, baseline), t)
                cx += advance(scale, st, t)
            cy += lh
        pt.end()

        argb = img.convertToFormat(QImage.Format_ARGB32)
        # (x, y) is the midpoint below the box: centered, but the bottom lifts
        # first
        ox = int(round(x - bw / 2))
        bot = self._box_bottom(y, fs, bh)
        oy = int(round(bot - bh))
        # off the top: stick to the edge, off-anchor beats invisible
        oy_clamped = max(0, oy)
        # unconditional, like the other probes in this file: the box landing
        # somewhere it shouldn't is a geometry question, and every term in it
        # (bh in CSS against a bottom in OSD, strip_top, the scale) has to be
        # on screen at once to say which one is wrong
        dims = self.player.osd_dimensions
        print(
            f"[box] osd={dims.get('w')}x{dims.get('h')} scale={self.scale:.3f} "
            f"fs={fs} 行数={len(rows)} 尺寸={bw:.0f}x{bh:.0f} "
            f"锚点=({x:.0f},{y:.0f}) strip_top={self.origin[1]} "
            f"strip_bot={self.origin[1] + self.css_h * self.scale:.0f} "
            f"bottom={bot:.0f} oy原始={oy} oy钳位={oy_clamped} "
            f"可用空间={bot:.0f}",
            flush=True,
        )
        oy = oy_clamped
        sub_log(f"box {text!r} 尺寸={bw:.0f}x{bh:.0f} 锚点=({x},{y}) 左上=({ox},{oy})")
        self._blit(
            bytes(argb.constBits()),
            iw,
            ih,
            int(round(bw)),
            int(round(bh)),
            ox,
            oy,
            BOX_ID,
        )

    def remove_box(self):
        """Take down the floating box (BOX_ID).

        No box is a no-op: mpv doesn't error on a missing id.
        """
        if BOX_ID not in self._cur:
            return
        sub_log("悬浮移开 -> overlay-remove box")
        self._mpv("overlay-remove", id=BOX_ID)
        self._cur.pop(BOX_ID, None)

    # -------------------------------------------------------------- mpv

    def _mpv(self, name, **kw):
        """overlay-* can only go from a worker thread: calling mpv
        synchronously on
        """

        def send():
            try:
                self.player.command(name, **kw)
            except Exception:
                import traceback

                traceback.print_exc()

        threading.Thread(target=send, daemon=True).start()

    def _on_bitmap(self, data, w, h):
        """The strip's bitmap -> overlay. Position and display size are
        computed for the whole strip.

        dw/dh use disp_w/disp_h (already scaled) rather than css_w/css_h:
        when the window is wider than MAX_RENDER_W the bitmap is rendered
        at MAX_RENDER_W, so mpv has to stretch it back to the full width,
        or the strip covers only the left 1920 px and the subtitle isn't
        centered.
        """
        self._blit(data, w, h, self.disp_w, self.disp_h, *self.origin, OVERLAY_ID)

    def _blit(self, data, w, h, dw, dh, ox, oy, oid):
        """Send one bitmap to mpv as an overlay. The same oid is an update; a
        different oid hangs separately.

        w/h are pixel counts, dw/dh the display size, ox/oy the top-left.
        x/y must be integers: pass a float and mpv rejects the whole
        command.
        """
        # the filename is made on the main thread: every call spawns a worker
        # to write it, so the sequence number has to be fixed here or two
        # threads race for it
        self._seq += 1
        path = os.path.join(self._tmpdir, f"ov{oid}-{self._seq}.bgra")

        def send():
            try:
                with open(path, "wb") as f:
                    f.write(data)
                self.player.command(
                    "overlay-add",
                    id=oid,
                    x=ox,
                    y=oy,
                    file=path,
                    offset=0,
                    fmt="bgra",
                    w=w,
                    h=h,
                    stride=w * 4,
                    dw=dw,
                    dh=dh,
                )
                sub_log(
                    f"overlay-add 已发 id={oid} w={w} h={h} "
                    f"显示尺寸={dw}x{dh} 位置=({ox},{oy})"
                )
                old = self._cur.get(oid)
                self._cur[oid] = path
                if old:
                    # the overlay has already swapped this image out
                    os.unlink(old)
            except Exception:
                import traceback

                traceback.print_exc()

        threading.Thread(target=send, daemon=True).start()

    # ------------------------------------------------------ hit testing

    def hit_test(self, mx, my):
        """The hit token's index; mx/my are already in CSS pixel space."""
        with self._lock:
            rects = self.rects
        for i, r in enumerate(rects):
            if not r["w"]:
                continue
            if r["x"] <= mx <= r["x"] + r["w"] and r["y"] <= my <= r["y"] + r["h"]:
                return i
        return -1

    def cursor_target(self):
        """The subtitle target under the mouse on this tick; None if nothing
        hit.

        Both the hover poll and the right-click callback need the same
        three things -- the full line, the word under the cursor and the
        coordinates -- and the conversion (mouse-pos is OSD, rects are
        render-area CSS, so an origin and then a scale in between) is the
        step most easily got wrong. Copied into both, they'd eventually
        disagree, so it lives here.

        Returns {"sub": the full line, "word": the word hit, "index": the
        token index, "x": OSD x, "y": OSD y}. Coordinates are always OSD,
        which is what the box side wants.
        """
        sub = self.player.sub_text or ""
        if not sub or not self.tokens:
            return None
        mp = self.player.mouse_pos or {}
        x, y = mp.get("x", 0), mp.get("y", 0)
        index = self.hit_test(
            (x - self.origin[0]) / self.scale,
            (y - self.origin[1]) / self.scale,
        )
        if index < 0 or index >= len(self.tokens):
            return None
        return {
            "sub": sub,
            "word": self.tokens[index],
            "index": index,
            "x": x,
            "y": y,
        }

    def anchor(self):
        """Reposition the render area for the current OSD dimensions, subtitle
        at the bottom.

        It only does arithmetic, touching no GUI object, so it can be
        called directly from the polling thread.
        """
        dims = self.player.osd_dimensions
        osd_w, osd_h = dims["w"], dims["h"]
        if not osd_w or not osd_h:
            # the video hasn't started, OSD is 0x0; wait for the next round
            return
        css_w = min(osd_w, MAX_RENDER_W)
        # when render width < OSD width, mpv does the scaling
        self.scale = osd_w / css_w
        # the font size comes from the video height, not from the strip height,
        # so it looks the same at any resolution
        self.font_size = max(16, int(SUB_FONT_RATIO * osd_h / self.scale))
        self.css_w = css_w
        self.css_h = int(self.font_size * SUB_LINE_H * MAX_SUB_LINES)
        # the display size scales the render area back out to the whole frame:
        # width = osd_w, height = css_h * scale. Without it a
        # wider-than-MAX_RENDER_W window leaves the strip on the left, subtitle
        # off-center.
        self.disp_w = osd_w
        self.disp_h = int(round(self.css_h * self.scale))
        # must be integers: overlay-add's x/y only take ints, 928.0 is rejected
        self.origin = (
            0,
            int(round(osd_h - self.disp_h)),
        )
        sub_log(
            f"anchor osd={osd_w}x{osd_h} css={self.css_w}x{self.css_h} "
            f"显示={self.disp_w}x{self.disp_h} "
            f"scale={self.scale:.3f} font={self.font_size} origin={self.origin}"
        )
