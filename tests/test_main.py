#!/usr/bin/env python3
"""Unit tests for the UGreenLedPilot entry point (SIGTERM / graceful shutdown)."""

import signal as _signal
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src' / 'app' / 'server'))

import main as main_mod


class TestGracefulShutdown(unittest.TestCase):
    def test_stops_monitor_then_shuts_server(self):
        ctx = MagicMock()
        server = MagicMock()
        done = threading.Event()
        server.shutdown.side_effect = lambda: done.set()

        main_mod._handle_sigterm(ctx, server)

        ctx.ctrl.stop_monitor.assert_called_once_with()
        self.assertTrue(done.wait(2), 'server.shutdown() should be invoked')

    def test_shutdown_is_non_blocking(self):
        # server.shutdown() must run in a daemon thread, never in the
        # signal-handler thread (blocking there deadlocks serve_forever()).
        ctx = MagicMock()
        server = MagicMock()
        main_mod._handle_sigterm(ctx, server)
        # Returns immediately; shutdown() is delegated to a daemon thread.

    def test_shutdown_runs_in_daemon_thread(self):
        ctx = MagicMock()
        server = MagicMock()
        called = threading.Event()
        recorded = {}

        def shutdown_spy():
            recorded['thread'] = threading.current_thread()
            called.set()

        server.shutdown.side_effect = shutdown_spy
        main_mod._handle_sigterm(ctx, server)

        self.assertTrue(called.wait(2), 'server.shutdown() should run')
        self.assertTrue(recorded['thread'].daemon)
        self.assertNotEqual(recorded['thread'], threading.current_thread())


class TestMainWiring(unittest.TestCase):
    def test_main_registers_sigterm_handler(self):
        with patch.object(main_mod, 'AppContext') as mock_ctx, \
                patch.object(main_mod, 'ThreadedHTTPServer') as mock_server_cls, \
                patch.object(main_mod.signal, 'signal') as mock_signal, \
                patch.object(main_mod, '_handle_sigterm') as mock_handle:
            ctx = mock_ctx.return_value
            server = mock_server_cls.return_value
            server.serve_forever.side_effect = KeyboardInterrupt

            main_mod.main()

            mock_signal.assert_called_once()
            sig, handler = mock_signal.call_args.args
            self.assertEqual(sig, _signal.SIGTERM)
            # handler must route back to the shared graceful shutdown
            handler(_signal.SIGTERM, None)
            mock_handle.assert_called_with(ctx, server)
            # KeyboardInterrupt path reuses the same graceful shutdown
            self.assertEqual(mock_handle.call_count, 2)
            mock_handle.assert_called_with(ctx, server)


if __name__ == '__main__':
    unittest.main()
