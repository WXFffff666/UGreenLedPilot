#!/usr/bin/env python3
"""UGreenLedPilot v2.2.1 — entry point."""

import signal
import threading
from socketserver import ThreadingMixIn
from http.server import HTTPServer

from app_context import AppContext, PORT, APP_VERSION
from http_handler import PilotHandler


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _handle_sigterm(ctx, server):
    """Graceful shutdown shared by SIGTERM and KeyboardInterrupt.

    server.shutdown() must NOT be called directly from the signal handler
    thread: it blocks until serve_forever() returns, which can never happen
    while the handler occupies the main thread -> deadlock. Spawn a daemon
    thread instead so shutdown() runs concurrently with serve_forever().
    """
    ctx.ctrl.stop_monitor()
    threading.Thread(target=server.shutdown, daemon=True).start()


def main():
    ctx = AppContext()
    print(f'UGreenLedPilot v{APP_VERSION}  port={PORT}  threads=on  sse=on')
    PilotHandler.app = ctx
    server = ThreadedHTTPServer(('0.0.0.0', PORT), PilotHandler)
    signal.signal(signal.SIGTERM, lambda s, f: _handle_sigterm(ctx, server))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _handle_sigterm(ctx, server)


if __name__ == '__main__':
    main()
