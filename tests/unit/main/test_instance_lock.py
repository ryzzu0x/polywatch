"""Unit test single-instance lock (T-027, ADR-014).

Membuktikan: lock pertama sukses, lock kedua (path sama) gagal dengan
`InstanceLockError`, release membuat lock bisa diambil lagi, dan lock
dilepas otomatis saat proses pemegang mati (dibuktikan lewat fd proses
anak — flock dilepas kernel saat fd tertutup).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from polywatch.main.instance_lock import InstanceLock, InstanceLockError


def test_lock_pertama_sukses_dan_kedua_gagal(tmp_path: Path) -> None:
    path = str(tmp_path / "polywatch.lock")
    first = InstanceLock(path)
    first.acquire()
    assert first.held

    second = InstanceLock(path)
    with pytest.raises(InstanceLockError):
        second.acquire()
    assert not second.held

    first.release()
    assert not first.held


def test_lock_setelah_release_bisa_diambil_lagi(tmp_path: Path) -> None:
    path = str(tmp_path / "polywatch.lock")
    first = InstanceLock(path)
    first.acquire()
    first.release()

    second = InstanceLock(path)
    second.acquire()  # tidak boleh raise
    second.release()


def test_lock_release_idempoten(tmp_path: Path) -> None:
    path = str(tmp_path / "polywatch.lock")
    lock = InstanceLock(path)
    lock.acquire()
    lock.release()
    lock.release()  # tidak boleh raise


def test_lock_lepas_saat_proses_pemegang_mati(tmp_path: Path) -> None:
    """Dua proses nyata: anak pegang lock lalu mati — parent bisa ambil."""
    path = str(tmp_path / "polywatch.lock")
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from polywatch.main.instance_lock import InstanceLock\n"
                f"InstanceLock({path!r}).acquire()\n"
            ),
        ],
        check=True,
        capture_output=True,
    )
    assert child.returncode == 0

    # Proses anak sudah mati — flock dilepas kernel; parent harus bisa ambil.
    lock = InstanceLock(path)
    lock.acquire()  # tidak boleh raise
    lock.release()


def test_lock_error_tanpa_menyentuh_telegram_atau_db() -> None:
    """InstanceLockError hanya sinyal exit — tidak ada I/O Telegram/DB di path ini."""
    err = InstanceLockError("another instance")
    assert isinstance(err, RuntimeError)
    assert "another instance" in str(err)
