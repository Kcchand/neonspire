import time, threading, logging, os
from gamevault_automation import ping_ok, KEEPALIVE_SECS

log = logging.getLogger("gv_keepalive")
if not log.handlers:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

_started = False

def _loop():
    interval = max(120, KEEPALIVE_SECS or 300)  # never < 2 minutes
    log.info("GameVault keepalive loop every %ss", interval)
    while True:
        ok = ping_ok()
        if not ok:
            log.warning("Keepalive ping not OK (auto-reauth attempted by client).")
        time.sleep(interval)

def start_keepalive_background():
    global _started
    if _started:
        return
    t = threading.Thread(target=_loop, name="gv-keepalive", daemon=True)
    t.start()
    _started = True