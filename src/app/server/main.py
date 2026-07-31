#!/usr/bin/env python3
"""UGreenLedPilot v2.0 — entry point."""

from socketserver import ThreadingMixIn
from http.server import HTTPServer

from app_context import AppContext, PORT, APP_VERSION
from http_handler import PilotHandler


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    ctx = AppContext()
    print(f'UGreenLedPilot v{APP_VERSION}  port={PORT}  threads=on  sse=on')
    PilotHandler.app = ctx
    server = ThreadedHTTPServer(('0.0.0.0', PORT), PilotHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        ctx.ctrl.stop_monitor()
        server.shutdown()


if __name__ == '__main__':
    main()
