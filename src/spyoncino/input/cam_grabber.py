from multiprocessing import Manager, Process
import logging
import cv2
import time
from datetime import datetime

_logger = logging.getLogger(__name__)


def _trim_buffer(rolling_buffer, maxlen):
    try:
        while len(rolling_buffer) > maxlen:
            rolling_buffer.pop(0)
    except (IndexError, TypeError):
        pass


def _update_buffer_size(memory_seconds, fps, buffer_maxlen, rolling_buffer):
    new_size = int(memory_seconds * fps.value)
    if new_size != buffer_maxlen.value:
        buffer_maxlen.value = new_size
        _trim_buffer(rolling_buffer, new_size)


def _grab_worker(
    source,
    cam_id,
    memory_seconds,
    running,
    connected,
    width,
    height,
    fps,
    rolling_buffer,
    buffer_maxlen,
):
    grab_capture = None

    while running.value:
        if grab_capture is None:
            try:
                grab_capture = cv2.VideoCapture(source)
                width.value = int(grab_capture.get(cv2.CAP_PROP_FRAME_WIDTH))
                height.value = int(grab_capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
                fps_value = int(grab_capture.get(cv2.CAP_PROP_FPS))
                fps.value = fps_value if fps_value > 0 else 10
                _update_buffer_size(memory_seconds, fps, buffer_maxlen, rolling_buffer)
                connected.value = True
            except Exception as e:
                print(f"Error initializing grab capture: {e}")
                grab_capture = None
                width.value = height.value = fps.value = 0
                connected.value = False

        if connected.value:
            _update_buffer_size(memory_seconds, fps, buffer_maxlen, rolling_buffer)

            ret, frame = grab_capture.read()
            if not ret:
                grab_capture.release()
                grab_capture = None
                connected.value = False
            elif frame is not None:
                rolling_buffer.append(
                    {"camera_id": cam_id, "timestamp": datetime.now(), "frame": frame}
                )
                _trim_buffer(rolling_buffer, buffer_maxlen.value)

        time.sleep(1 / fps.value if fps.value > 0 else 0.1)

    if grab_capture is not None:
        grab_capture.release()
    connected.value = width.value = height.value = fps.value = 0


class CamGrabber:
    def __init__(
        self,
        cam_id: str = None,
        type: str = None,
        source: str = None,
        memory_seconds: int = 1,
    ):
        self._manager = Manager()

        self.cam_id = cam_id
        self.type = type
        self.source = source
        self.memory_seconds = memory_seconds

        self._running = self._manager.Value("b", False)
        self._connected = self._manager.Value("b", False)
        self._width = self._manager.Value("i", 0)
        self._height = self._manager.Value("i", 0)
        self._fps = self._manager.Value("i", 0)
        self._buffer_maxlen = self._manager.Value("i", 0)

        self._grab_process = None

        self._buffer_maxlen.value = int(memory_seconds * 30)
        self._rolling_buffer = self._manager.list()

        self._last_seen_timestamp = None

        self._start()

    def __del__(self):
        try:
            if hasattr(self, "_running") and self._running.value:
                self._stop()
            if hasattr(self, "_manager") and self._manager is not None:
                self._manager.shutdown()
        except Exception:
            _logger.debug("CamGrabber __del__ cleanup failed", exc_info=True)

    def _start(self):
        if not self._running.value:
            self._running.value = True
            self._grab_process = Process(
                target=_grab_worker,
                args=(
                    self.source,
                    self.cam_id,
                    self.memory_seconds,
                    self._running,
                    self._connected,
                    self._width,
                    self._height,
                    self._fps,
                    self._rolling_buffer,
                    self._buffer_maxlen,
                ),
            )
            self._grab_process.daemon = True
            self._grab_process.start()

    def _stop(self):
        try:
            if hasattr(self, "_running") and self._running.value:
                self._running.value = False
                if hasattr(self, "_grab_process") and self._grab_process is not None:
                    self._grab_process.join(timeout=1.0)
                    self._grab_process = None
        except Exception:
            _logger.debug("CamGrabber _stop cleanup failed", exc_info=True)

    def snap(self):
        """
        Get the latest frame from the buffer.
        Returns a reference to the frame (not a copy).
        If you need to modify the frame, copy it first: frame.copy()
        """
        try:
            return self._rolling_buffer[-1]
        except (IndexError, TypeError):
            return None

    def record(self):
        """
        Return all frames in the buffer.
        Returns references to frames (not copies) for performance.
        If you need to modify frames, copy them first: [fi.copy() for fi in frames]
        """
        try:
            return list(self._rolling_buffer)
        except (TypeError, AttributeError):
            return []

    def capture(self):
        return self.snap(), self.record()

    @property
    def buffer_size(self):
        """Get current buffer size without copying frames."""
        try:
            return len(self._rolling_buffer)
        except (TypeError, AttributeError):
            return 0

    @property
    def running(self):
        return self._running.value

    @property
    def connected(self):
        return self._connected.value

    @property
    def width(self):
        return self._width.value

    @property
    def height(self):
        return self._height.value

    @property
    def fps(self):
        return self._fps.value

    def stream(self):
        while self._running.value:
            try:
                latest_frame = self._rolling_buffer[-1]
                latest_timestamp = latest_frame["timestamp"]

                if (
                    self._last_seen_timestamp is None
                    or latest_timestamp > self._last_seen_timestamp
                ):
                    yield (self.snap(), self.record())
                    self._last_seen_timestamp = latest_timestamp
            except (IndexError, TypeError):
                pass
            time.sleep(0.001)
