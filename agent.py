"""
agent.py — Main entry point
Launches Stream 1 (Forex) and Stream 2 (Luno) as background threads.
Keeps itself alive and restarts any thread that dies.
"""

import time
import logging
import threading

import stream_forex
import stream_luno

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


def watchdog(target_fn, name, restart_delay=15):
    """Run target_fn in a loop — restart it if it exits or crashes."""
    while True:
        log.info(f"[watchdog] Starting {name}...")
        t = threading.Thread(target=target_fn, daemon=True, name=name)
        t.start()
        t.join()   # blocks until thread exits (crash or normal exit)
        log.warning(f"[watchdog] {name} exited — restarting in {restart_delay}s")
        time.sleep(restart_delay)


if __name__ == "__main__":
    log.info("=" * 60)
    log.info("  AUTONOMOUS TRADING AGENT")
    log.info("  Stream 1 | Forex (Alpaca paper) | $1,000")
    log.info("  Stream 2 | Luno (3 strategies)  | R18,000")
    log.info("=" * 60)

    # Launch both streams with watchdogs in background threads
    for fn, name in [
        (stream_forex.run, "ForexStream"),
        (stream_luno.run,  "LunoStream"),
    ]:
        t = threading.Thread(
            target=watchdog, args=(fn, name),
            daemon=True, name=f"watchdog-{name}"
        )
        t.start()
        log.info(f"✅ {name} watchdog started")

    log.info("Agent running. Ctrl+C to stop.\n")

    # Keep main thread alive
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        log.info("Agent stopped.")
