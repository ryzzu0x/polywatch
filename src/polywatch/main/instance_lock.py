"""Single-instance lock aplikasi (T-027, ADR-014).

`fcntl.flock` eksklusif non-blocking pada file lock (path dari Settings
`LOCK_FILE`, default direktori temp). flock dilepas otomatis oleh OS saat
proses mati — aman untuk crash, tidak butuh pembersihan manual.

Mengapa ini penting: dua instance yang berjalan bersamaan menyebabkan
dua HealthMonitor (alert ganda ke owner) dan polling Telegram saling
mengganggu (`TelegramConflictError: terminated by other getUpdates
request`).

Lock kedua gagal dengan `InstanceLockError` — caller keluar dengan exit
code non-zero TANPA menyentuh Telegram/DB.
"""

from __future__ import annotations

import errno
import fcntl
import logging
import os

logger = logging.getLogger("polywatch.instance_lock")


class InstanceLockError(RuntimeError):
    """Instance lain sedang berjalan — lock eksklusif gagal didapat."""


class InstanceLock:
    """flock eksklusif non-blocking pada satu file, dilepas saat shutdown."""

    def __init__(self, path: str) -> None:
        self._path = path
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> None:
        """Ambil lock; raise `InstanceLockError` bila sudah dipegang proses lain.

        File dibuat bila belum ada. `flock(LOCK_EX | LOCK_NB)` — proses
        kedua mendapat BlockingIOError (EWOULDBLOCK) → error jelas,
        bukan hang.
        """
        if self._fd is not None:
            return
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                logger.error(
                    "instance lain sedang berjalan; keluar tanpa menyentuh Telegram/DB",
                    extra={"event": "another_instance_running", "lock_file": self._path},
                )
                raise InstanceLockError(
                    f"another polywatch instance holds lock {self._path}"
                ) from exc
            raise
        os.truncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        self._fd = fd
        logger.info(
            "instance lock dipegang",
            extra={"event": "instance_lock_acquired", "lock_file": self._path, "pid": os.getpid()},
        )

    def release(self) -> None:
        """Lepas lock (idempoten; dipanggil saat shutdown rapi)."""
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
        logger.info(
            "instance lock dilepas",
            extra={"event": "instance_lock_released", "lock_file": self._path},
        )

    def __enter__(self) -> InstanceLock:
        self.acquire()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


__all__ = ["InstanceLock", "InstanceLockError"]
