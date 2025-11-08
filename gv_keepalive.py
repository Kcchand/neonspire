# gv_keepalive.py
import os, threading, time, logging

log = logging.getLogger("gv_keepalive")

def start_keepalive_background():
    """
    For the new 2Captcha-flow, we don't reuse sessions, so keepalive is optional.
    Set GAMEVAULT_KEEPALIVE_SECS<=0 to disable.
    """
    secs = int(os.getenv("GAMEVAULT_KEEPALIVE_SECS", "0") or 0)
    if secs <= 0:
        log.info("Keepalive disabled (GAMEVAULT_KEEPALIVE_SECS<=0).")
        return

    def _tick():
        log.info("GameVault keepalive loop every %ss", secs)
        while True:
            try:
                # No-op: we don't want to waste 2Captcha on background pings.
                # If you decide to add a *lightweight* health check later, do it here.
                pass
            except Exception as e:
                log.warning("Keepalive tick error: %s", e)
            time.sleep(secs)

    t = threading.Thread(target=_tick, daemon=True)
    t.start()