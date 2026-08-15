"""Exclusive Linux input ownership for one configured Bluetooth HID device."""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import re
from collections.abc import Callable, Coroutine
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any

from homeassistant.core import CALLBACK_TYPE, callback

_LOGGER = logging.getLogger(__name__)

# Linux include/uapi/linux/input.h: _IOW('E', 0x90, int)
_EVIOCGRAB = 0x40044590
_EVENT_NAME_RE = re.compile(r"event[0-9]+\Z")
_POLL_INTERVAL = 0.25

TaskFactory = Callable[[Coroutine[Any, Any, None], str], asyncio.Task[None]]


@dataclass(frozen=True, slots=True)
class InputGrabStatus:
    """Snapshot of matching and exclusively acquired evdev nodes."""

    matching_nodes: tuple[str, ...] = ()
    grabbed_nodes: tuple[str, ...] = ()
    errors: tuple[tuple[str, str], ...] = ()

    @property
    def active(self) -> bool:
        """Return whether every currently matching node is protected."""
        return bool(self.matching_nodes) and self.matching_nodes == self.grabbed_nodes


def _default_task_factory(
    coroutine: Coroutine[Any, Any, None], name: str
) -> asyncio.Task[None]:
    return asyncio.create_task(coroutine, name=name)


class BluetoothInputGrabber:
    """Continuously grab only evdev nodes whose sysfs uniq equals one MAC."""

    def __init__(
        self,
        address: str,
        *,
        sys_class_input: Path = Path("/sys/class/input"),
        dev_input: Path = Path("/dev/input"),
        task_factory: TaskFactory = _default_task_factory,
        opener: Callable[[str, int], int] = os.open,
        closer: Callable[[int], None] = os.close,
        ioctl: Callable[[int, int, int], Any] = fcntl.ioctl,
    ) -> None:
        self.address = address.casefold()
        self._sys_class_input = sys_class_input
        self._dev_input = dev_input
        self._task_factory = task_factory
        self._opener = opener
        self._closer = closer
        self._ioctl = ioctl
        self._fds: dict[str, int] = {}
        self._last_errors: dict[str, str] = {}
        self._listeners: set[Callable[[InputGrabStatus], None]] = set()
        self._status = InputGrabStatus()
        self._task: asyncio.Task[None] | None = None
        self._sync_lock = Lock()
        self._stopping = False

    @property
    def status(self) -> InputGrabStatus:
        """Return the last published acquisition state."""
        return self._status

    async def async_start(self) -> None:
        """Acquire existing nodes, then watch for disconnect and recreation."""
        if self._task is not None:
            return
        self._stopping = False
        await self._async_reconcile()
        self._task = self._task_factory(
            self._async_poll(), f"Bluetooth HID input protection {self.address}"
        )

    async def async_stop(self) -> None:
        """Stop reconciliation and release every exclusive input grab."""
        self._stopping = True
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        await asyncio.to_thread(self._release_all_sync)
        self._publish_status(InputGrabStatus())

    def async_add_listener(
        self, listener: Callable[[InputGrabStatus], None]
    ) -> CALLBACK_TYPE:
        """Subscribe a diagnostic entity to acquisition-state changes."""
        self._listeners.add(listener)
        return lambda: self._listeners.discard(listener)

    async def _async_poll(self) -> None:
        while not self._stopping:
            await asyncio.sleep(_POLL_INTERVAL)
            await self._async_reconcile()

    async def _async_reconcile(self) -> None:
        status = await asyncio.to_thread(self._reconcile_sync)
        self._publish_status(status)

    @callback
    def _publish_status(self, status: InputGrabStatus) -> None:
        if status == self._status:
            return
        self._status = status
        for listener in tuple(self._listeners):
            listener(status)

    def _reconcile_sync(self) -> InputGrabStatus:
        with self._sync_lock:
            if self._stopping:
                return self._status

            matching = self._matching_nodes_sync()
            for path in tuple(self._fds):
                if path not in matching:
                    self._release_sync(path)

            errors: dict[str, str] = {}
            for path in sorted(matching):
                if path in self._fds:
                    continue
                try:
                    fd = self._opener(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
                    try:
                        self._ioctl(fd, _EVIOCGRAB, 1)
                    except Exception:
                        self._closer(fd)
                        raise
                except Exception as err:
                    error_text = f"{type(err).__name__}: {err}"
                    errors[path] = error_text
                    if self._last_errors.get(path) != error_text:
                        _LOGGER.warning(
                            "Could not exclusively acquire Bluetooth HID input "
                            "%s for %s: %s",
                            path,
                            self.address,
                            err,
                        )
                    continue
                self._fds[path] = fd
                _LOGGER.info(
                    "Exclusively acquired Bluetooth HID input %s for %s",
                    path,
                    self.address,
                )

            self._last_errors = errors
            return InputGrabStatus(
                matching_nodes=tuple(sorted(matching)),
                grabbed_nodes=tuple(sorted(self._fds)),
                errors=tuple(sorted(errors.items())),
            )

    def _matching_nodes_sync(self) -> set[str]:
        matching: set[str] = set()
        try:
            candidates = tuple(self._sys_class_input.glob("event*"))
        except OSError:
            return matching
        for candidate in candidates:
            if _EVENT_NAME_RE.fullmatch(candidate.name) is None:
                continue
            try:
                uniq = (candidate / "device" / "uniq").read_text().strip().casefold()
            except OSError:
                continue
            if uniq == self.address:
                matching.add(str(self._dev_input / candidate.name))
        return matching

    def _release_sync(self, path: str) -> None:
        fd = self._fds.pop(path, None)
        if fd is None:
            return
        with suppress(OSError):
            self._ioctl(fd, _EVIOCGRAB, 0)
        with suppress(OSError):
            self._closer(fd)
        _LOGGER.info("Released Bluetooth HID input %s for %s", path, self.address)

    def _release_all_sync(self) -> None:
        with self._sync_lock:
            for path in tuple(self._fds):
                self._release_sync(path)
