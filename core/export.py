import os
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable

from .ffmpeg import build_ffmpeg_command, build_audio_extract_command
from .paths import _log


class ExportRunner:
    """Run ffmpeg export jobs in a background thread pool.

    Callbacks:
        on_clip_done(path: str)
        on_all_done()
        on_error(msg: str)
    """

    def __init__(
        self,
        input_path: str,
        jobs: list[tuple[float, str, str | None, float]],
        short_side: int | None = None,
        image_sequence: bool = False,
        max_workers: int | None = None,
        encoder: str = "libx264",
        on_clip_done: Callable[[str], None] | None = None,
        on_all_done: Callable[[], None] | None = None,
        on_error: Callable[[str], None] | None = None,
    ):
        self._input = input_path
        self._jobs = jobs
        self._short_side = short_side
        self._image_sequence = image_sequence
        self._max_workers = max_workers
        self._encoder = encoder
        self._on_clip_done = on_clip_done
        self._on_all_done = on_all_done
        self._on_error = on_error
        self._cancel = False
        self._procs: list[subprocess.Popen] = []
        self._procs_lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def cancel(self):
        self._cancel = True
        with self._procs_lock:
            for proc in self._procs:
                try:
                    proc.kill()
                except OSError:
                    pass

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run_one(self, start: float, output: str,
                 portrait_ratio: str | None, crop_center: float) -> str:
        if self._cancel:
            raise RuntimeError("cancelled")
        if self._image_sequence:
            os.makedirs(output, exist_ok=True)
        cmd = build_ffmpeg_command(
            self._input, start, output,
            short_side=self._short_side,
            portrait_ratio=portrait_ratio,
            crop_center=crop_center,
            image_sequence=self._image_sequence,
            encoder=self._encoder,
        )
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        with self._procs_lock:
            self._procs.append(proc)
        try:
            _, stderr = proc.communicate(timeout=120)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise RuntimeError("ffmpeg timed out")
        finally:
            with self._procs_lock:
                self._procs.remove(proc)
        if self._cancel:
            raise RuntimeError("cancelled")
        if proc.returncode != 0:
            msg = stderr.decode(errors='replace')[-500:] if stderr else "ffmpeg failed"
            raise RuntimeError(msg)
        if self._image_sequence:
            audio_cmd = build_audio_extract_command(self._input, start, output)
            subprocess.run(audio_cmd, capture_output=True, text=True, timeout=60)
        return output

    def _run(self):
        cap = self._max_workers or (os.cpu_count() or 2)
        workers = min(len(self._jobs), cap)
        try:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(self._run_one, s, o, pr, cc): o
                    for s, o, pr, cc in self._jobs
                }
                for fut in as_completed(futures):
                    if self._cancel:
                        break
                    try:
                        path = fut.result()
                        if self._on_clip_done:
                            self._on_clip_done(path)
                    except Exception as e:
                        if "cancelled" not in str(e) and self._on_error:
                            self._on_error(str(e))
        except Exception as e:
            if self._on_error:
                self._on_error(str(e))
            return
        if self._cancel:
            return
        if self._on_all_done:
            self._on_all_done()
