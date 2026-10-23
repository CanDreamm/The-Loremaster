"""Keep startup recovery from changing another running process's jobs."""
import fcntl
import os
import sqlite3
from pathlib import Path

from .settings import ConfigError


class ExclusiveConnection(sqlite3.Connection):
    def __init__(self, database, *args, **kwargs):
        # Resolve symlinks so two paths to the same database share the lock.
        path = Path(database).resolve()
        descriptor = os.open(str(path) + '.lock', os.O_CREAT | os.O_RDWR, 0o600)
        self._lock_descriptor = None
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ConfigError('База Публикота уже используется. Остановите другую копию бота. '
                                  'Перед импортом подключений: sudo systemctl stop publikot. '
                                  'После импорта: sudo systemctl start publikot.') from None
            super().__init__(database, *args, **kwargs)
            self._lock_descriptor = descriptor
        finally:
            if self._lock_descriptor is None:
                os.close(descriptor)

    def close(self):
        try:
            super().close()
        finally:
            descriptor = self._lock_descriptor
            if descriptor is not None:
                self._lock_descriptor = None
                os.close(descriptor)

    def __del__(self):
        # Also release the process lock if a failed startup discards the connection.
        descriptor = getattr(self, '_lock_descriptor', None)
        if descriptor is not None:
            self._lock_descriptor = None
            os.close(descriptor)
