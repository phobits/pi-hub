"""Touch-initiated poweroff / reboot (Ticket #2).

Two gates, both deliberate:

- ``app.allow_shutdown`` (``APP_ALLOW_SHUTDOWN``) defaults to **false**. The
  local demo server must never be able to halt a developer's machine; only
  the Pi's ``.env`` turns this on.
- The command is a fixed argv through ``sudo -n`` and matches the sudoers
  rule in ``deploy/sudoers/smart-display`` exactly. No shell, no arguments
  from the request.
"""
from __future__ import annotations

import logging
import subprocess
from typing import Any, Callable, Sequence


logger = logging.getLogger(__name__)

SYSTEMCTL = "/usr/bin/systemctl"
COMMANDS: dict[str, tuple[str, ...]] = {
    "shutdown": ("sudo", "-n", SYSTEMCTL, "poweroff"),
    "reboot": ("sudo", "-n", SYSTEMCTL, "reboot"),
}
NOT_ALLOWED_MESSAGE = "Ausschalten ist auf diesem Gerät nicht freigegeben."


def _default_runner(argv: Sequence[str]) -> int:
    completed = subprocess.run(
        list(argv),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=15,
        check=False,
    )
    if completed.returncode != 0:
        logger.warning(
            "%s exited %s: %s",
            " ".join(argv),
            completed.returncode,
            completed.stderr.decode("utf-8", "replace").strip(),
        )
    return completed.returncode


class PowerController:
    def __init__(
        self,
        *,
        allowed: bool,
        runner: Callable[[Sequence[str]], int] | None = None,
    ) -> None:
        self.allowed = bool(allowed)
        self._runner = runner or _default_runner

    def shutdown(self) -> dict[str, Any]:
        return self._run("shutdown", "Wird ausgeschaltet …")

    def reboot(self) -> dict[str, Any]:
        return self._run("reboot", "Wird neu gestartet …")

    def _run(self, action: str, message: str) -> dict[str, Any]:
        if not self.allowed:
            return {"ok": False, "message": NOT_ALLOWED_MESSAGE}
        argv = COMMANDS[action]
        try:
            code = self._runner(argv)
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("power action %s failed: %s", action, exc)
            return {"ok": False, "message": "Befehl konnte nicht ausgeführt werden."}
        if code != 0:
            return {
                "ok": False,
                "message": "Befehl wurde abgelehnt (sudo-Regel fehlt?).",
            }
        return {"ok": True, "message": message}
