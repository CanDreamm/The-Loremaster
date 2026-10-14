"""Consistent SQLite backup, including committed WAL transactions."""
import sqlite3
import sys
from pathlib import Path

if len(sys.argv) != 3:
    raise SystemExit('Использование: python scripts/backup.py data/publikot-vk.sqlite3 backups/backup.sqlite3')
source, target = map(Path, sys.argv[1:])
if not source.is_file() or target.exists():
    raise SystemExit('Нужна существующая база и новый путь для копии.')
target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
target.touch(mode=0o600, exist_ok=False)
with sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True) as src:
    with sqlite3.connect(target) as dst:
        src.backup(dst)
print('Резервная копия базы создана. Фотографии скопируйте отдельно.')
