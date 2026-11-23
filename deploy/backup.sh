#!/bin/bash
# Nightly SQLite backup. Run from the repo folder (see the systemd units here).
# Uses SQLite's online backup API inside the container, so it's safe while the
# app is running. Backups land in the data volume under backups/.
set -euo pipefail
KEEP_DAYS=30
docker compose exec -T app python -c "
import datetime, pathlib, sqlite3, time
d = pathlib.Path('/app/instance/backups'); d.mkdir(exist_ok=True)
src = sqlite3.connect('/app/instance/tracker.db')
dst = sqlite3.connect(d / f'tracker-{datetime.date.today()}.db')
src.backup(dst); dst.close(); src.close()
cutoff = time.time() - $KEEP_DAYS * 86400
for f in d.glob('tracker-*.db'):
    if f.stat().st_mtime < cutoff: f.unlink()
"
