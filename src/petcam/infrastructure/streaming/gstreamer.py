#!/usr/bin/env python3
"""Publish a camera's native H.264 byte stream through GStreamer RTSP Server."""

from __future__ import annotations

import subprocess
import threading
import time

from ..camera.capture import build_capture_command

RTSP_PORT = 8554
RTSP_PATH = "/petcam"
CAPTURE_READ_SIZE = 65536

# Keep Annex-B input as a byte stream. h264parse frames complete access units;
# the bounded downstream queue drops whole access units when a client is slow.
RTSP_PIPELINE = (
    '( appsrc name=source is-live=true format=time do-timestamp=true '
    'min-latency=0 block=true max-bytes=1048576 '
    'caps="video/x-h264,stream-format=(string)byte-stream" '
    '! h264parse '
    '! video/x-h264,stream-format=(string)byte-stream,alignment=(string)au '
    '! queue max-size-buffers=30 max-size-bytes=0 max-size-time=0 leaky=downstream '
    '! rtph264pay name=pay0 pt=96 config-interval=-1 aggregate-mode=none )'
)


class GStreamerError(RuntimeError):
    """Raised when GStreamer, RTSP, or camera capture cannot run."""


def _load_gstreamer():
    try:
        import gi

        gi.require_version("Gst", "1.0")
        gi.require_version("GstRtspServer", "1.0")
        from gi.repository import GLib, Gst, GstRtspServer
    except (ImportError, ValueError) as error:
        raise GStreamerError(
            "GStreamer RTSP support is missing. Install python3-gi, "
            "gir1.2-gst-rtsp-server-1.0, gstreamer1.0-plugins-good, "
            "and gstreamer1.0-plugins-bad."
        ) from error
    Gst.init(None)
    return GLib, Gst, GstRtspServer


def validate_runtime() -> None:
    """Fail early when a required GStreamer element or RTSP binding is absent."""
    _glib, gst, rtsp_server = _load_gstreamer()
    if rtsp_server.RTSPServer is None:
        raise GStreamerError("The GStreamer RTSP server binding is unavailable.")
    missing = [
        name for name in ("appsrc", "h264parse", "queue", "rtph264pay")
        if gst.ElementFactory.find(name) is None
    ]
    if missing:
        raise GStreamerError(
            "Missing GStreamer elements: " + ", ".join(missing)
            + ". Install the GStreamer base, good, and bad plugin packages."
        )
    try:
        pipeline = gst.parse_launch(RTSP_PIPELINE)
        pipeline.set_state(gst.State.NULL)
    except Exception as error:
        raise GStreamerError(f"The GStreamer RTSP pipeline is invalid: {error}") from error


class GStreamerRtspServer:
    """An in-process GStreamer RTSP server with one shared live media pipeline."""

    def __init__(self, port: int = RTSP_PORT, path: str = RTSP_PATH, on_media_configure=None):
        self.GLib, self.Gst, self.GstRtspServer = _load_gstreamer()
        self._media_configured_callback = on_media_configure
        validate_runtime()
        self._lock = threading.RLock()
        self._appsrc = None
        self._media_bus = None
        self._error = ""
        self._clients: set[int] = set()
        self._context = self.GLib.MainContext.default()
        self._source_id = 0

        self.server = self.GstRtspServer.RTSPServer()
        self.server.set_service(str(port))
        self.factory = self.GstRtspServer.RTSPMediaFactory()
        self.factory.set_shared(True)
        self.factory.set_launch(RTSP_PIPELINE)
        self.factory.connect("media-configure", self._on_media_configure)
        self.server.get_mount_points().add_factory(path, self.factory)
        self.server.connect("client-connected", self._on_client_connected)

        self._source_id = self.server.attach(self._context)
        if not self._source_id:
            raise GStreamerError(f"Unable to bind the GStreamer RTSP server to port {port}.")

    def iterate(self) -> None:
        """Poll RTSP/GLib sources without blocking the foreground terminal loop."""
        self._context.iteration(False)

    @property
    def error(self) -> str:
        with self._lock:
            return self._error

    @property
    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)

    def _set_error(self, message: str) -> None:
        with self._lock:
            if not self._error:
                self._error = message

    def _on_client_connected(self, _server, client) -> None:
        client_id = id(client)
        with self._lock:
            self._clients.add(client_id)
        client.connect("closed", self._on_client_closed, client_id)

    def _on_client_closed(self, _client, client_id: int) -> None:
        with self._lock:
            self._clients.discard(client_id)

    def _on_media_configure(self, _factory, media) -> None:
        pipeline = media.get_element()
        appsrc = pipeline.get_by_name("source")
        if appsrc is None:
            self._set_error("GStreamer RTSP media pipeline has no appsrc element.")
            return
        bus = pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_media_message)
        media.connect("unprepared", self._on_media_unprepared, appsrc, bus)
        with self._lock:
            self._appsrc = appsrc
            self._media_bus = bus
        if self._media_configured_callback is not None:
            self._media_configured_callback(appsrc)

    def _on_media_unprepared(self, _media, appsrc, bus) -> None:
        with self._lock:
            if self._appsrc is appsrc:
                self._appsrc = None
            if self._media_bus is bus:
                self._media_bus = None
        try:
            appsrc.set_property("block", False)
            appsrc.emit("end-of-stream")
        except Exception:
            pass
        try:
            bus.remove_signal_watch()
        except Exception:
            pass

    def _on_media_message(self, _bus, message) -> None:
        if message.type == self.Gst.MessageType.ERROR:
            error, debug = message.parse_error()
            detail = str(error)
            if debug:
                detail += f" ({debug})"
            self._set_error(f"GStreamer media pipeline failed: {detail}")

    def push(self, chunk: bytes) -> None:
        """Push raw Annex-B bytes into the active shared appsrc, if a client prepared it."""
        if not chunk:
            return
        with self._lock:
            appsrc = self._appsrc
        if appsrc is None:
            return
        buffer = self.Gst.Buffer.new_allocate(None, len(chunk), None)
        buffer.fill(0, chunk)
        result = appsrc.emit("push-buffer", buffer)
        if result not in (self.Gst.FlowReturn.OK, self.Gst.FlowReturn.FLUSHING):
            raise GStreamerError(f"GStreamer appsrc rejected camera data: {result!r}.")

    def stop(self) -> None:
        with self._lock:
            appsrc = self._appsrc
            bus = self._media_bus
            self._appsrc = None
            self._media_bus = None
        if appsrc is not None:
            try:
                appsrc.set_property("block", False)
                appsrc.emit("end-of-stream")
            except Exception:
                pass
        if bus is not None:
            try:
                bus.remove_signal_watch()
            except Exception:
                pass
        if self._source_id:
            self.GLib.source_remove(self._source_id)
            self._source_id = 0


class GStreamerSession:
    """Own camera capture and the RTSP server for the lifetime of the foreground UI."""

    def __init__(self, camera_config: dict):
        self.command = build_capture_command(camera_config)
        self.server: GStreamerRtspServer | None = None
        self.capture: subprocess.Popen | None = None
        self.reader: threading.Thread | None = None
        self._stop = threading.Event()
        self._first_data = threading.Event()
        self._lock = threading.Lock()
        self._error = ""
        self._capture_starting = False
        self._bytes_received = 0
        self._last_sample_bytes = 0
        self._last_sample_at = time.monotonic()
        self._bytes_per_second = 0.0

    def start(self) -> None:
        try:
            self.server = GStreamerRtspServer(on_media_configure=self._start_capture)
        except Exception:
            self.stop()
            raise

    def _start_capture(self, _appsrc) -> None:
        """Start camera output only once the RTSP media pipeline is ready to consume it."""
        with self._lock:
            if self._stop.is_set() or self.capture is not None or self._capture_starting:
                return
            self._capture_starting = True

        try:
            process = subprocess.Popen(
                self.command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )
            with self._lock:
                self._capture_starting = False
                should_stop = self._stop.is_set()
                if not should_stop:
                    self.capture = process
                    self.reader = threading.Thread(
                        target=self._read_capture, name="petcam-camera-capture", daemon=True
                    )
                    reader = self.reader
            if should_stop:
                process.terminate()
                process.wait(timeout=5)
                if process.stdout is not None:
                    process.stdout.close()
                return
            reader.start()
        except Exception as error:
            with self._lock:
                self._capture_starting = False
                self._error = str(error)


    def _read_capture(self) -> None:
        assert self.capture is not None and self.capture.stdout is not None
        try:
            while not self._stop.is_set():
                chunk = self.capture.stdout.read(CAPTURE_READ_SIZE)
                if not chunk:
                    break
                with self._lock:
                    self._bytes_received += len(chunk)
                self._first_data.set()
                if self.server is not None:
                    self.server.push(chunk)
            if not self._stop.is_set():
                with self._lock:
                    if not self._error:
                        self._error = "Camera capture stopped producing H.264 data."
        except (OSError, GStreamerError) as error:
            if not self._stop.is_set():
                with self._lock:
                    self._error = str(error)

    def snapshot(self) -> dict:
        now = time.monotonic()
        with self._lock:
            elapsed = max(now - self._last_sample_at, 0.001)
            current = self._bytes_received
            self._bytes_per_second = (current - self._last_sample_bytes) / elapsed
            self._last_sample_bytes = current
            self._last_sample_at = now
            error = self._error
            total = self._bytes_received
            rate = self._bytes_per_second
        if error:
            state = "error"
        elif self._first_data.is_set():
            state = "ready"
        elif self.capture is not None or self._capture_starting:
            state = "starting"
        else:
            state = "waiting for client"
        return {
            "state": state,
            "message": error,
            "clients": self.server.client_count if self.server else 0,
            "capture_bytes_per_second": rate,
            "capture_bytes_total": total,
        }

    def iterate(self) -> None:
        if self.server is not None:
            self.server.iterate()

    def stop(self) -> None:
        self._stop.set()
        if self.capture is not None and self.capture.poll() is None:
            self.capture.terminate()
            try:
                self.capture.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.capture.kill()
                self.capture.wait(timeout=2)
        if self.server is not None:
            self.server.stop()
        if self.reader is not None and self.reader.is_alive():
            self.reader.join(timeout=3)
        if self.capture is not None and self.capture.stdout is not None:
            self.capture.stdout.close()
        self.server = None
