"""Linux kernel uevent watcher for block device hotplug (zero-poll)."""

import os
import socket
import threading

NETLINK_KOBJECT_UEVENT = 15


class UeventWatcher:
    """Listen for block add/remove via netlink — wakes only on real hotplug."""

    def __init__(self, callback):
        self.callback = callback
        self._stop = threading.Event()
        self._thread = None
        self.available = False
        self._running = False

    def is_running(self):
        return self._running and self._thread and self._thread.is_alive()

    def start(self):
        if self.is_running():
            return
        if self._thread and self._thread.is_alive():
            # A previous thread may still be winding down (blocked in recv up to 3s).
            # Join it first so a second netlink socket is never bound concurrently.
            self._stop.set()
            self._thread.join(timeout=4.0)
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name='uevent-watcher')
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            # 4s > recv timeout 3s: the loop observes _stop before the join times out.
            self._thread.join(timeout=4.0)
            self._thread = None
        self._running = False

    def _loop(self):
        sock = None
        try:
            sock = socket.socket(socket.AF_NETLINK, socket.SOCK_DGRAM, NETLINK_KOBJECT_UEVENT)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)
            sock.bind((os.getpid(), -1))
            sock.settimeout(3.0)
            self.available = True
            self._running = True
            print('Hotplug: netlink uevent listener active (event-driven, no polling)')
        except OSError as exc:
            print(f'Hotplug: netlink unavailable ({exc}), fallback to slow signature poll')
            return

        while not self._stop.is_set():
            try:
                data = sock.recv(65535)
                text = data.decode('utf-8', errors='ignore')
                if 'block' in text and ('add@' in text or 'remove@' in text):
                    try:
                        self.callback()
                    except Exception as exc:
                        print(f'Uevent callback error: {exc}')
            except socket.timeout:
                continue
            except OSError:
                break

        self._running = False
        if sock:
            try:
                sock.close()
            except OSError:
                pass
