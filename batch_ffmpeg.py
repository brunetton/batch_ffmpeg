#!/usr/bin/env python3
"""Batch-encode video files with ffmpeg, one after another, in a GTK 4 window.

Usage:
  batch_ffmpeg.py [options] <file>... [-- <ffmpeg-option>...]

Arguments:
  <file>            Input video files, processed sequentially in the given order.
  <ffmpeg-option>   Everything after the first "--" is forwarded to ffmpeg verbatim.

Options:
  -h --help             Show this help and exit.
  -s SUF, --suffix=SUF  Suffix inserted before the extension of each output file
                        [default: _out].
  -q, --quiet           Do not print the global text progress line.

Examples:
  batch_ffmpeg.py clip1.mp4 clip2.mov -- -c:v libx264 -crf 20 -preset slow

Progress is reported twice:

  * a GTK 4 window (PyGObject + libadwaita) carrying two progress bars: the upper
    one follows the current file, the lower one the whole batch, and the header
    names the file being encoded. Its "Cancel all" button aborts everything, kills
    ffmpeg and deletes the half-written output file; closing the window asks for
    confirmation and cancels the same way;
  * one batch line, rewritten in place on stdout:

        [2/5] clip2.mov  [##########--------------------]  34%

A file that fails does not stop the batch: the window names it and shows the exact
ffmpeg command that was run, with the full log behind an "ffmpeg log" expander. The
command and the whole log are printed on stderr too.

ffmpeg options are applied as *output* options. -progress, -stats_period and
-nostats are injected automatically when you do not provide them yourself.

Requires GTK 4 and libadwaita for PyGObject (Debian/Ubuntu: python3-gi,
gir1.2-gtk-4.0, gir1.2-adw-1).
"""

import math
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

from docopt import docopt

try:
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    # PyGObject builds `gi.repository` at runtime, so the symbols cannot be
    # resolved statically.
    from gi.repository import Adw, Gio, GioUnix, GLib, GLibUnix, Gtk  # type: ignore
except (ImportError, ValueError) as error:  # pragma: no cover - environment check
    print(
        "batch_ffmpeg.py: GTK 4 and libadwaita (PyGObject) are required.\n"
        f"  {error}\n"
        "  Debian/Ubuntu: apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1",
        file=sys.stderr,
    )
    raise SystemExit(2) from error

GLib.set_prgname("batch_ffmpeg")

PROGRESS_OPTION = "-progress"
PROGRESS_VALUE = "pipe:1"
STATS_PERIOD_OPTION = "-stats_period"
STATS_PERIOD_VALUE = "0.3"
NOSTATS_OPTION = "-nostats"

DEFAULT_SUFFIX = "_out"
BAR_WIDTH = 30
TERMINATE_GRACE = 3.0

# Both the terminal and the window show the whole captured log; cap it so that a
# pathological log cannot flood the terminal or make the window unusable.
ERROR_LOG_BYTES = 65536

# Gio.ApplicationFlags.NON_UNIQUE, so that two batches can run side by side and the
# app never needs a session D-Bus (it works over plain SSH).
APP_ID = "dev.bruno.BatchFfmpeg"
WINDOW_TITLE = "Batch ffmpeg"
CANCEL_DIALOG_KEEP = "keep"
CANCEL_DIALOG_CANCEL = "cancel"

# Width of the column holding the "File" / "Total" caption, wide enough for the
# longest of them so that both bars start on the same x.
CAPTION_WIDTH = 64

# Width of the column holding the percentage, wide enough for "100 %" so that the
# bars keep the same length all along.
PERCENT_WIDTH = 52

# How often the process exit is re-checked once its output stream ended, and how
# often an indeterminate bar is pulsed.
EXIT_POLL_MS = 20
PULSE_INTERVAL_MS = 150


# --------------------------------------------------------------------------- CLI


def split_argv(argv):
    """Split argv on the first "--" into (own_args, ffmpeg_options).

    docopt cannot do this itself: handed the full argv it stores the separator
    and every following token inside <file>. Everything to the right of the
    separator is forwarded to ffmpeg without any interpretation.
    """
    if "--" in argv:
        index = argv.index("--")
        return list(argv[:index]), list(argv[index + 1 :])
    return list(argv), []


def option_present(options, name):
    """True if `name` is used as an option, as `-x value` or as `-x=value`."""
    return any(token == name or token.startswith(name + "=") for token in options)


def option_value(options, name):
    """Return the value given to `name`, or None when it is absent or bare."""
    for index, token in enumerate(options):
        if token == name:
            if index + 1 < len(options):
                return options[index + 1]
            return None
        if token.startswith(name + "="):
            return token.split("=", 1)[1]
    return None


def plan_piping(options):
    """Decide which piping options to inject.

    Returns (inject_flags, warnings). Only options the user did not provide are
    injected, so an explicit user value always wins.
    """
    inject = {
        "progress": not option_present(options, PROGRESS_OPTION),
        "stats_period": not option_present(options, STATS_PERIOD_OPTION),
        "nostats": not option_present(options, NOSTATS_OPTION),
    }

    warnings = []
    if not inject["progress"]:
        value = option_value(options, PROGRESS_OPTION)
        if value != PROGRESS_VALUE:
            warnings.append(
                "warning: your '{opt} {value}' overrides the progress pipe, so the "
                "progress bars cannot be updated (expected '{expected}').".format(
                    opt=PROGRESS_OPTION, value=value, expected=PROGRESS_VALUE
                )
            )
    return inject, warnings


def die(message, code=2):
    print(f"batch_ffmpeg.py: {message}", file=sys.stderr)
    raise SystemExit(code)


# --------------------------------------------------------------------------- paths


def output_path(input_path, suffix):
    """`clip.mov` -> `clip_out.mov` (same directory, same extension)."""
    return input_path.with_name(input_path.stem + suffix + input_path.suffix)


def probe_duration(path):
    """Return the duration in seconds, or None when it cannot be determined."""
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=nw=1:nk=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    try:
        seconds = float(result.stdout.strip())
    except ValueError:
        return None

    if not math.isfinite(seconds) or seconds <= 0:
        return None
    return seconds


# --------------------------------------------------------------------------- commands


def build_ffmpeg_command(user_options, input_path, output_path, inject):
    command = ["ffmpeg", "-nostdin", "-hide_banner", "-y", "-loglevel", "error"]

    if inject["progress"]:
        command += [PROGRESS_OPTION, PROGRESS_VALUE]
    if inject["stats_period"]:
        command += [STATS_PERIOD_OPTION, STATS_PERIOD_VALUE]
    if inject["nostats"]:
        command += [NOSTATS_OPTION]

    command += ["-i", str(input_path)]
    command += list(user_options)
    command.append(str(output_path))
    return command


# --------------------------------------------------------------------------- progress


def clamp_fraction(fraction):
    """Clamp a progress fraction into 0..1."""
    return min(1.0, max(0.0, fraction))


def render_bar(fraction, width=BAR_WIDTH):
    filled = int(min(1.0, max(0.0, fraction)) * width)
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def render_progress(fraction, unknown=False):
    """Return (bar, percent) for a fraction, or the indeterminate markers.

    The percentage is floored, never rounded: the terminal line should not read
    100 % while the file is still being written. This is display-only -- the GTK
    bars take the raw fraction.
    """
    if unknown:
        return "[" + "?" * BAR_WIDTH + "]", " ??%"
    fraction = min(1.0, max(0.0, fraction))
    return render_bar(fraction), f"{math.floor(fraction * 100):3d}%"


def format_line(index, total, name, total_fraction, unknown=False):
    """The terminal line: position in the batch, current file and total progress."""
    bar, percent = render_progress(total_fraction, unknown)
    return f"[{index}/{total}] {name}  {bar} {percent}"


class ProgressReporter:
    """The global progress line, rewritten in place on stdout."""

    def __init__(self, enabled):
        self.enabled = enabled
        self._wrote = False

    def show(self, text):
        if not self.enabled:
            return
        sys.stdout.write("\r" + text + "\033[K")
        sys.stdout.flush()
        self._wrote = True

    def finish(self):
        if self.enabled and self._wrote:
            sys.stdout.write("\n")
            sys.stdout.flush()
            self._wrote = False


class BatchWindow(Adw.ApplicationWindow):
    """One window for the whole batch, with a bar per progress level.

    The upper bar follows the current file, the lower one the whole batch. The
    header names the file being encoded and carries the "Cancel all" button; when a
    file fails, a card under the bars names it and shows the exact ffmpeg command
    with the captured log behind a "ffmpeg log" expander. The card is not dismissed
    when the next file starts -- the batch carries on, and the failure stays
    readable until another one replaces it. The two detail labels under the bars are
    reserved for a future time-remaining estimate and stay hidden for now.
    """

    def __init__(self, on_cancel, on_close_request):
        super().__init__(title=WINDOW_TITLE, default_width=560, default_height=250)
        self._on_cancel = on_cancel
        self._on_close_request = on_close_request
        self._close_confirmed = False
        self._pulse_source = 0

        self.title_widget = Adw.WindowTitle(title=WINDOW_TITLE, subtitle="starting")
        self.cancel_button = Gtk.Button(label="Cancel all")
        self.cancel_button.add_css_class("destructive-action")
        self.cancel_button.connect("clicked", lambda *_: self._on_cancel())

        header = Adw.HeaderBar()
        header.set_title_widget(self.title_widget)
        header.pack_end(self.cancel_button)

        self.position_label = Gtk.Label(xalign=0.0, label="starting")
        self.position_label.add_css_class("dim-label")

        file_row, self.file_bar, self.file_percent = self._progress_row("File")
        total_row, self.total_bar, self.total_percent = self._progress_row("Total")
        self.file_detail = self._detail_label()
        self.total_detail = self._detail_label()

        self.failure_title = Gtk.Label(xalign=0.0, wrap=True, label="")
        self.failure_command = Gtk.Label(
            xalign=0.0, wrap=True, selectable=True, label=""
        )
        self.failure_command.add_css_class("monospace")
        self.failure_log = Gtk.Label(xalign=0.0, wrap=True, selectable=True, label="")
        self.failure_log.add_css_class("monospace")
        log_view = Gtk.ScrolledWindow(
            propagate_natural_height=True,
            max_content_height=140,
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            child=self.failure_log,
        )
        failure_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        failure_box.add_css_class("card")
        failure_box.set_margin_top(10)
        failure_box.set_margin_bottom(10)
        failure_box.set_margin_start(10)
        failure_box.set_margin_end(10)
        failure_box.append(self.failure_title)
        # The command stays visible: it is what makes a failure reproducible, and a
        # selection in the label can be pasted straight into a shell.
        failure_box.append(self.failure_command)
        failure_box.append(Gtk.Expander(label="ffmpeg log", child=log_view))
        self.failure_revealer = Gtk.Revealer(
            transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN,
            child=failure_box,
        )

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for widget in (
            self.position_label,
            file_row,
            self.file_detail,
            total_row,
            self.total_detail,
            self.failure_revealer,
        ):
            content.append(widget)
        content.set_margin_top(18)
        content.set_margin_bottom(18)
        content.set_margin_start(18)
        content.set_margin_end(18)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        # The clamp keeps the rows readable when the window is maximized (the rows
        # stretch, the labels stay close to them).
        toolbar.set_content(
            Adw.Clamp(maximum_size=520, tightening_threshold=420, child=content)
        )
        self.set_content(toolbar)

        self.connect("close-request", self._on_close_requested)

    @staticmethod
    def _progress_row(caption):
        """A caption, a bar and a percentage, all three on the bar's centre line."""
        caption_label = Gtk.Label(label=caption, xalign=0.5, valign=Gtk.Align.CENTER)
        caption_label.set_size_request(CAPTION_WIDTH, -1)

        bar = Gtk.ProgressBar(show_text=False, hexpand=True, valign=Gtk.Align.CENTER)

        percent_label = Gtk.Label(xalign=1.0, valign=Gtk.Align.CENTER, label="0 %")
        percent_label.set_size_request(PERCENT_WIDTH, -1)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        row.append(caption_label)
        row.append(bar)
        row.append(percent_label)
        return row, bar, percent_label

    @staticmethod
    def _detail_label():
        """A muted line under a bar, empty until there is something to say."""
        label = Gtk.Label(xalign=0.0, label="")
        label.add_css_class("dim-label")
        label.set_visible(False)
        return label

    # -- updates

    def set_item(self, index, total, name):
        self.title_widget.set_subtitle(name)
        self.position_label.set_text(f"{index} / {total}")

    def set_file_fraction(self, fraction):
        # While the bar pulses there is no fraction to show, and a pulse frame would
        # fight a fraction update: leave the text at "?? %".
        if self._pulse_source:
            return
        self._set_bar(self.file_bar, self.file_percent, fraction)

    def set_total_fraction(self, fraction):
        self._set_bar(self.total_bar, self.total_percent, fraction)

    def set_pulsing(self, pulsing):
        """A file with no known duration has no fraction to show: pulse the bar."""
        if pulsing and not self._pulse_source:
            self.file_percent.set_text("?? %")
            self.file_bar.pulse()
            self._pulse_source = GLib.timeout_add(PULSE_INTERVAL_MS, self._pulse)
        elif not pulsing and self._pulse_source:
            GLib.source_remove(self._pulse_source)
            self._pulse_source = 0
            self.file_bar.set_fraction(0.0)
            self.file_percent.set_text("0 %")

    def show_failure(self, name, command, log):
        """Reveal the failure card. It is left up until another failure replaces it."""
        self.failure_title.set_text(f"ffmpeg failed on {name}")
        self.failure_command.set_text(command)
        self.failure_log.set_text(log or "(no output)")
        self.failure_revealer.set_reveal_child(True)

    def finish(self):
        """The batch is over: there is nothing left to cancel."""
        self.set_pulsing(False)
        self.cancel_button.set_sensitive(False)

    def ask_cancel(self):
        """Confirm before losing the partial output, then cancel if asked to."""
        dialog = Adw.AlertDialog(
            heading="Cancel the batch?",
            body="Encoding stops now and the partial output file is deleted.",
        )
        dialog.add_response(CANCEL_DIALOG_KEEP, "Continue encoding")
        dialog.add_response(CANCEL_DIALOG_CANCEL, "Cancel the batch")
        dialog.set_response_appearance(
            CANCEL_DIALOG_CANCEL, Adw.ResponseAppearance.DESTRUCTIVE
        )
        dialog.set_default_response(CANCEL_DIALOG_KEEP)
        dialog.set_close_response(CANCEL_DIALOG_KEEP)
        dialog.choose(self, None, self._on_cancel_answer)

    # -- internals

    @staticmethod
    def _set_bar(bar, percent_label, fraction):
        fraction = clamp_fraction(fraction)
        bar.set_fraction(fraction)
        percent_label.set_text(f"{round(fraction * 100)} %")

    def _pulse(self):
        self.file_bar.pulse()
        return GLib.SOURCE_CONTINUE

    def _on_cancel_answer(self, dialog, result):
        if dialog.choose_finish(result) == CANCEL_DIALOG_CANCEL:
            self._close_confirmed = True
            self._on_cancel()

    def _on_close_requested(self, *_args):
        # Returning True keeps the window open; the runner decides, because only
        # it knows whether a file is in flight.
        if self._close_confirmed:
            return False
        return self._on_close_request()


# --------------------------------------------------------------------------- runtime


def terminate_async(process, grace=TERMINATE_GRACE):
    """SIGTERM now, SIGKILL after a grace period, without blocking the main loop."""
    if process is None or process.poll() is not None:
        return
    process.terminate()

    def escalate():
        if process.poll() is None:
            process.kill()
        return GLib.SOURCE_REMOVE

    GLib.timeout_add(int(grace * 1000), escalate)


def remove_partial(path):
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def close_stdout(process):
    if process is None or process.stdout is None:
        return
    try:
        process.stdout.close()
    except OSError:
        pass


def read_stderr_log(sink, limit=ERROR_LOG_BYTES):
    """The whole captured ffmpeg log, cut short after `limit` bytes."""
    try:
        sink.flush()
        sink.seek(0)
        data = sink.read(limit + 1)
    except OSError:
        return ""
    text = data.decode("utf-8", "replace").strip()
    if len(data) > limit:
        return text + "\n[... log truncated ...]"
    return text


def format_command(command):
    """Render an argv so it can be pasted back into a shell."""
    return shlex.join(command)


def report_failure(path, command, log):
    """Print the failing file, the command that ran and the whole log on stderr.

    The command sits on a line of its own, unindented, so that copying it from the
    terminal gives something that runs as-is.
    """
    print(f"batch_ffmpeg.py: ffmpeg failed on {path}", file=sys.stderr)
    print(command, file=sys.stderr)
    print("--- ffmpeg log ---", file=sys.stderr)
    print(log or "(no output)", file=sys.stderr)


class BatchRunner:
    """Drives the batch, one ffmpeg child at a time, from the GLib main loop.

    Nothing here blocks: ffmpeg's `-progress` stream is read incrementally with
    `Gio.DataInputStream.read_line_async()`, so the window stays responsive while
    a file is encoded. Cancelling is a single code path, shared by the Cancel
    button, the window close request and Ctrl+C.
    """

    def __init__(
        self, app, window, reporter, inputs, durations, user_options, inject, suffix
    ):
        self.app = app
        self.window = window
        self.reporter = reporter
        self.inputs = inputs
        self.durations = durations
        self.user_options = user_options
        self.inject = inject
        self.suffix = suffix
        self.total = len(inputs)

        self.index = 0
        self.succeeded = 0
        self.failures = []
        self.done = False
        self.cancelled = False

        # The first item is overwritten by _begin_item() before anything reads it;
        # starting from it keeps these attributes plain `Path`s.
        self.source = inputs[0]
        self.target = output_path(inputs[0], suffix)
        self.command = build_ffmpeg_command(
            user_options, inputs[0], self.target, inject
        )
        self.duration_us = None
        self.process = None
        self.stream = None
        self.stderr_sink = None
        self.exit_source = 0
        self.ending = False

    # -- lifecycle

    def start(self):
        # Ctrl+C goes through the same door as the Cancel button. The handler
        # returns SOURCE_CONTINUE so the source stays installed and a second
        # Ctrl+C works too; because of it, KeyboardInterrupt is never raised.
        GLibUnix.signal_add(GLib.PRIORITY_HIGH, signal.SIGINT, self._on_interrupt)
        self._begin_item()

    def _begin_item(self):
        self.ending = False
        self.source = self.inputs[self.index]
        self.target = output_path(self.source, self.suffix)
        duration = self.durations[self.index]
        self.duration_us = int(duration * 1_000_000) if duration else None

        self.window.set_item(self.index + 1, self.total, self.source.name)
        self.window.set_pulsing(duration is None)
        self._show(0.0)

        self.stderr_sink = tempfile.TemporaryFile(mode="w+b")
        self.command = build_ffmpeg_command(
            self.user_options, self.source, self.target, self.inject
        )
        self.process = subprocess.Popen(
            self.command,
            stdout=subprocess.PIPE,
            stderr=self.stderr_sink,
        )
        stdout = self.process.stdout
        assert stdout is not None  # stdout=subprocess.PIPE above
        # close_fd=False: the descriptor belongs to Popen.stdout and is closed by
        # close_stdout() when the item ends.
        stream = Gio.DataInputStream.new(
            GioUnix.InputStream.new(stdout.fileno(), False)
        )
        self.stream = stream
        stream.read_line_async(GLib.PRIORITY_DEFAULT, None, self._on_line)

    # -- progress

    def _on_line(self, stream, result):
        line, _length = stream.read_line_finish(result)
        if not line:
            # read_line_finish() hands back empty bytes (not None) at end of file.
            self._arm_exit_poll()
            return
        if not self.cancelled:
            self._consume(line.decode("utf-8", "replace"))
        # Keep draining while cancelling, so that EOF is still seen.
        stream.read_line_async(GLib.PRIORITY_DEFAULT, None, self._on_line)

    def _consume(self, text):
        key, _, value = text.strip().partition("=")
        if key == "out_time_us" and self.duration_us:
            try:
                elapsed_us = int(value)
            except ValueError:
                return
            self._show(min(1.0, max(0.0, elapsed_us / self.duration_us)))
        elif key == "progress" and value == "end":
            self._show(1.0)

    def _show(self, file_fraction, unknown=None):
        """The single place that feeds both bars and the terminal line.

        `unknown` defaults to "this file has no measurable duration", which is what
        the terminal line reports as `??%`.
        """
        if unknown is None:
            unknown = self.duration_us is None
        total_fraction = (self.index + file_fraction) / self.total
        self.window.set_file_fraction(file_fraction)
        self.window.set_total_fraction(total_fraction)
        self.reporter.show(
            format_line(
                self.index + 1, self.total, self.source.name, total_fraction, unknown
            )
        )

    # -- end of an item

    def _arm_exit_poll(self):
        if not self.exit_source:
            self.exit_source = GLib.timeout_add(EXIT_POLL_MS, self._poll_exit)

    def _poll_exit(self):
        """ffmpeg's stream ended: wait for the process itself, without blocking.

        GLib's child watch is deliberately not used: it reaps the child, which
        makes Popen.wait() raise ChildProcessError.
        """
        if self.ending:
            return GLib.SOURCE_REMOVE
        if self.process is not None and self.process.poll() is None:
            return GLib.SOURCE_CONTINUE
        self.exit_source = 0
        self.ending = True
        self._end_item(self.process is not None and self.process.returncode == 0)
        return GLib.SOURCE_REMOVE

    def _end_item(self, succeeded):
        self.window.set_pulsing(False)
        if not self.cancelled:
            self._show(1.0, unknown=False)

        log = "" if succeeded or self.cancelled else read_stderr_log(self.stderr_sink)
        close_stdout(self.process)
        if self.stderr_sink is not None:
            self.stderr_sink.close()
            self.stderr_sink = None
        self.stream = None
        self.process = None

        if self.cancelled:
            remove_partial(self.target)
            self.reporter.finish()
            print("Cancelled. The batch was stopped.")
            self._quit(130)
            return

        if succeeded:
            self.succeeded += 1
        else:
            self.failures.append(self.source)
            # End the progress line first, so the error text does not land on top
            # of it.
            self.reporter.finish()
            command = format_command(self.command)
            report_failure(self.source, command, log)
            self.window.show_failure(self.source.name, command, log)

        self.index += 1
        if self.index >= self.total:
            self._complete()
        else:
            self._begin_item()

    def _complete(self):
        self.reporter.finish()
        print(f"Done: {self.succeeded}/{self.total} succeeded.")
        code = 0
        if self.failures:
            print(f"Failed ({len(self.failures)}):")
            for path in self.failures:
                print(f"  - {path}")
            code = 1
        self._quit(code)

    def _quit(self, code):
        self.done = True
        self.app.exit_code = code
        self.window.finish()
        self.window.close()
        self.app.quit()

    # -- cancelling

    def cancel(self):
        """Kill ffmpeg, drop the partial output, stop the batch."""
        if self.done or self.cancelled:
            return
        self.cancelled = True
        self.window.finish()
        terminate_async(self.process)
        # ffmpeg's stream reaches EOF as it dies; polling the process is the
        # backstop for a child that is wedged and never writes again.
        self._arm_exit_poll()

    def _on_interrupt(self):
        """Ctrl+C is treated exactly like the Cancel button."""
        self.cancel()
        return GLib.SOURCE_CONTINUE


class BatchApplication(Adw.Application):
    """The GTK application: one window, one runner, one batch."""

    def __init__(self, inputs, durations, user_options, inject, suffix, quiet):
        # NON_UNIQUE, so two batches can run side by side and no session D-Bus is
        # needed (the app works from a plain SSH shell).
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.NON_UNIQUE)
        self.inputs = inputs
        self.durations = durations
        self.user_options = user_options
        self.inject = inject
        self.suffix = suffix
        self.quiet = quiet
        self.exit_code = 0
        self.window = None
        self.runner = None

    def do_activate(self):
        self.window = BatchWindow(
            on_cancel=self._cancel,
            on_close_request=self._on_close_request,
        )
        reporter = ProgressReporter(enabled=sys.stdout.isatty() and not self.quiet)
        self.runner = BatchRunner(
            self,
            self.window,
            reporter,
            self.inputs,
            self.durations,
            self.user_options,
            self.inject,
            self.suffix,
        )
        self.add_window(self.window)
        self.window.present()
        self.runner.start()

    def _cancel(self):
        if self.runner is not None:
            self.runner.cancel()

    def _on_close_request(self):
        """False lets the window close; True keeps it up while confirming."""
        if self.runner is None or self.runner.done or self.window is None:
            return False
        self.window.ask_cancel()
        return True


# --------------------------------------------------------------------------- main


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    own_args, user_options = split_argv(argv)
    args = docopt(__doc__, argv=own_args)

    suffix = args["--suffix"] or DEFAULT_SUFFIX
    quiet = bool(args["--quiet"])
    inputs = [Path(name) for name in args["<file>"]]

    for program in ("ffmpeg", "ffprobe"):
        if shutil.which(program) is None:
            die(f"required program not found in PATH: {program}")

    missing = [str(path) for path in inputs if not path.is_file()]
    if missing:
        die("not a file: " + ", ".join(missing))

    inject, warnings = plan_piping(user_options)
    for warning in warnings:
        print(warning, file=sys.stderr)
    if not user_options:
        print(
            "warning: no ffmpeg options given, ffmpeg will use its defaults.",
            file=sys.stderr,
        )

    # Every duration is probed before the window appears: they drive each file's
    # bar, and probing inside the main loop would freeze the UI for the length of
    # the ffprobe call. It also means the whole batch is measurable from the
    # start, which is what a batch-wide time estimate would need.
    durations = [probe_duration(path) for path in inputs]

    application = BatchApplication(
        inputs, durations, user_options, inject, suffix, quiet
    )
    # A GApplication cannot carry an exit status back to its caller, so it travels
    # on the instance and is returned once the main loop is over.
    application.run([])
    return application.exit_code


if __name__ == "__main__":
    sys.exit(main())
