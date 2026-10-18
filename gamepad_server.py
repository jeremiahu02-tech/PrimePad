#!/usr/bin/env python3
"""
Mobile Gamepad - PC Companion Server (multi-player + license edition)

What it does:
  * Serves the mobile gamepad page (index.html) over HTTP.
  * Accepts WebSocket connections carrying gamepad state packets and control messages.
  * Enforces the PC's license tier:
        FREE     -> 1 phone
        PRO      -> 1 phone (Pro features unlocked)
        ULTIMATE -> up to 4 phones (couch co-op) with all Pro features
  * Stores the license in license.json and verifies Flutterwave payments.
  * Gives each phone its own virtual Xbox 360 controller (ViGEmBus).
  * Prints a QR code in the terminal so the phone can connect with no typing.

Configuration:
  * Secrets (FLUTTERWAVE_SECRET_KEY) are read from a .env file next to the EXE
    or this script, or from real environment variables. Never hard-code them.
  * license.json is stored next to the EXE or this script.

Gamepad state packet (sent roughly every 100 ms, no "type" field):
  {"leftStick": {"x": .., "y": ..}, "rightStick": {"x": .., "y": ..},
   "buttons": {"A": bool, ..., "dpadRight": bool}}

Control messages (these always have a "type" field):
  phone -> PC  {"type": "get_status"}
  phone -> PC  {"type": "verify_payment", "transaction_id": "...", "tx_ref": "gp-PRO-..."}
  PC -> phone  {"type": "status", "tier": "...", "features": {...}, "capacity": n, "player": n}
  PC -> phone  {"type": "payment_result", "ok": bool, "tier": "...", "message": "...", "retry": bool}
  PC -> phone  {"type": "error", "code": "HOST_UPGRADE_REQUIRED" | "SLOTS_FULL", "message": "..."}
  PC -> all    {"type": "TIER_UPGRADED", "tier": "...", "features": {...}, "capacity": n}

Install:
    pip install vgamepad websockets qrcode
    ViGEmBus driver: https://github.com/ViGEm/ViGEmBus/releases

Run:
    python gamepad_server.py
    python gamepad_server.py --ws-port 5000 --http-port 8000 --log-level DEBUG

Security: anyone on your local network who can reach these ports can control a
player slot. Use this on a trusted home network only. Never port-forward it.
"""

import argparse
import asyncio
import functools
import json
import logging
import math
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import vgamepad as vg
import websockets
from websockets.exceptions import ConnectionClosed

try:
    import qrcode
except ImportError:  # QR output is optional
    qrcode = None


# ---------------------------------------------------------------------------
# Windowed-build safety: PyInstaller --noconsole sets stdout/stderr to None.
# Replacing them with no-op streams stops logging calls from crashing.
# ---------------------------------------------------------------------------

class _NullStream:
    def write(self, *args, **kwargs):
        pass

    def flush(self, *args, **kwargs):
        pass

    def isatty(self):
        return False


if sys.stdout is None:
    sys.stdout = _NullStream()
if sys.stderr is None:
    sys.stderr = _NullStream()


# ---------------------------------------------------------------------------
# Locations: EXE folder for user data, bundled folder for the web page
# ---------------------------------------------------------------------------

def app_dir() -> Path:
    """Folder that holds the EXE (frozen build) or this script (source run)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


# ---------------------------------------------------------------------------
# .env loading (no third-party dependency)
# ---------------------------------------------------------------------------

_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def load_dotenv(path: Path) -> bool:
    """
    Load KEY=VALUE pairs from a .env file into os.environ.

    Rules:
      * Blank lines and lines starting with # are ignored.
      * Values may be wrapped in single or double quotes.
      * Real environment variables always win over the file, so a key set in the
        system environment is never overwritten.
      * A missing file is not an error. Returns True if the file was read.
    """
    if not path.is_file():
        return False
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _ENV_LINE.match(line)
        if not match:
            continue
        key, value = match.group(1), match.group(2)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        else:
            # Strip an inline comment that follows whitespace, e.g. KEY=abc  # note
            value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
        os.environ.setdefault(key, value)
    return True


ENV_FILE = app_dir() / ".env"
load_dotenv(ENV_FILE)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MAX_PLAYERS = 4
DEFAULT_BIND = "0.0.0.0"
DEFAULT_WS_PORT = 5000
DEFAULT_HTTP_PORT = 8000

# If a connected player sends nothing for this long, their inputs are released.
# The browser heartbeat is every 100 ms, so a healthy link never trips this.
WATCHDOG_TIMEOUT_S = 0.5

# Reject oversized messages. A normal packet is well under 1 KB.
MAX_MESSAGE_BYTES = 4096

# Log repeated packet errors at these counts only, so a broken client
# cannot flood the terminal.
BAD_PACKET_LOG_EVERY = 100

# WebSocket close code (application range 4000-4999) for "host upgrade required".
CLOSE_UPGRADE_REQUIRED = 4003

# Browser button name -> Xbox 360 button constant.
BUTTON_MAP: Dict[str, int] = {
    "A": vg.XUSB_BUTTON.XUSB_GAMEPAD_A,
    "B": vg.XUSB_BUTTON.XUSB_GAMEPAD_B,
    "X": vg.XUSB_BUTTON.XUSB_GAMEPAD_X,
    "Y": vg.XUSB_BUTTON.XUSB_GAMEPAD_Y,
    "LB": vg.XUSB_BUTTON.XUSB_GAMEPAD_LEFT_SHOULDER,
    "RB": vg.XUSB_BUTTON.XUSB_GAMEPAD_RIGHT_SHOULDER,
    "VIEW": vg.XUSB_BUTTON.XUSB_GAMEPAD_BACK,
    "MENU": vg.XUSB_BUTTON.XUSB_GAMEPAD_START,
    "XBOX": vg.XUSB_BUTTON.XUSB_GAMEPAD_GUIDE,
    "dpadUp": vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_UP,
    "dpadDown": vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_DOWN,
    "dpadLeft": vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_LEFT,
    "dpadRight": vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_RIGHT,
}


# ---------------------------------------------------------------------------
# License and payment configuration
# ---------------------------------------------------------------------------

# Read from .env (or the environment). Never write the key into this file.
FLUTTERWAVE_SECRET_KEY = os.environ.get("FLUTTERWAVE_SECRET_KEY", "").strip()
FLUTTERWAVE_VERIFY_URL = "https://api.flutterwave.com/v3/transactions/{id}/verify"

TIER_FREE = "FREE"
TIER_PRO = "PRO"
TIER_ULTIMATE = "ULTIMATE"
TIER_RANK = {TIER_FREE: 0, TIER_PRO: 1, TIER_ULTIMATE: 2}
TIER_LABEL = {TIER_FREE: "Free", TIER_PRO: "Pro", TIER_ULTIMATE: "Ultimate"}

# How many phones the host allows at once, per tier.
TIER_CAPACITY = {TIER_FREE: 1, TIER_PRO: 1, TIER_ULTIMATE: MAX_PLAYERS}

# Lowest tier that unlocks each feature. The PC is authoritative; phones mirror this table for display only.
FEATURE_MIN_TIER = {
    "gyro": TIER_PRO,
    "macros": TIER_PRO,
    "deadzone_tuning": TIER_PRO,
    "custom_layout": TIER_PRO,
    "multiplayer": TIER_ULTIMATE,
}

# Accepted prices per tier and currency. Must match PRICES in index.html.
# >>> PLACEHOLDER: the NGN amounts are examples. Set your real local prices. <<<
PRICE_TABLE: Dict[str, Dict[str, float]] = {
    TIER_PRO: {"USD": 5, "NGN": 7500},
    TIER_ULTIMATE: {"USD": 15, "NGN": 22500},
}

# The phone generates payment references as gp-<TIER>-<unique id>. The tier is read from here.
TX_REF_PATTERN = re.compile(r"^gp-(PRO|ULTIMATE)-[A-Za-z0-9-]{8,80}$")
TRANSACTION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

LICENSE_FILE = app_dir() / "license.json"


# ---------------------------------------------------------------------------
# Terminal colors and logging
# ---------------------------------------------------------------------------

USE_COLOR = False


class Color:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    GREY = "\033[90m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    BRIGHT_MAGENTA = "\033[95m"
    BOLD_RED = "\033[1;31m"


def enable_terminal_colors(disabled: bool) -> bool:
    """Return True if ANSI colors should be used in this terminal."""
    if disabled or os.environ.get("NO_COLOR") or sys.stdout is None or not sys.stdout.isatty():
        return False
    if os.name == "nt":
        # An empty system call switches on ANSI escape processing in Windows consoles.
        os.system("")
    return True


def paint(text: str, code: str) -> str:
    if USE_COLOR and code:
        return f"{code}{text}{Color.RESET}"
    return text


LEVEL_COLORS = {
    logging.DEBUG: Color.GREY,
    logging.INFO: Color.GREEN,
    logging.WARNING: Color.YELLOW,
    logging.ERROR: Color.RED,
    logging.CRITICAL: Color.BOLD_RED,
}

CATEGORY_COLORS = {
    "server": Color.BLUE,
    "http": Color.CYAN,
    "qr": Color.MAGENTA,
    "players": Color.GREEN,
    "packets": Color.YELLOW,
    "license": Color.BRIGHT_MAGENTA,
}


class ColorFormatter(logging.Formatter):
    """Color-coded terminal output: level by severity, tag by subsystem."""

    def format(self, record: logging.LogRecord) -> str:
        category = record.name.rsplit(".", 1)[-1]
        timestamp = time.strftime("%H:%M:%S", time.localtime(record.created))
        level = paint(f"{record.levelname:<8}", LEVEL_COLORS.get(record.levelno, ""))
        tag = paint(f"[{category}]".ljust(10), CATEGORY_COLORS.get(category, ""))
        message = record.getMessage()
        if record.exc_info:
            message += "\n" + self.formatException(record.exc_info)
        return f"{paint(timestamp, Color.GREY)} {level} {tag} {message}"


def configure_logging(level_name: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(ColorFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level_name))
    # Keep the websockets library quiet unless something is wrong.
    logging.getLogger("websockets").setLevel(logging.WARNING)


log_server = logging.getLogger("gamepad.server")
log_http = logging.getLogger("gamepad.http")
log_qr = logging.getLogger("gamepad.qr")
log_players = logging.getLogger("gamepad.players")
log_packets = logging.getLogger("gamepad.packets")
log_license = logging.getLogger("gamepad.license")


# ---------------------------------------------------------------------------
# Input helpers
# ---------------------------------------------------------------------------

def read_axis(value: Any) -> float:
    """Convert an arbitrary JSON value to a finite float clamped to [-1.0, 1.0]."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(number):
        return 0.0
    return max(-1.0, min(1.0, number))


def read_stick(obj: Any) -> Tuple[float, float]:
    """Return (x, y) from an object like {"x": 0.5, "y": -0.2}."""
    if not isinstance(obj, dict):
        return 0.0, 0.0
    return read_axis(obj.get("x", 0.0)), read_axis(obj.get("y", 0.0))


def read_trigger(value: Any) -> int:
    """Convert a trigger to the 0-255 range. Accepts booleans or floats in [0.0, 1.0]."""
    if isinstance(value, bool):
        return 255 if value else 0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0
    if not math.isfinite(number):
        return 0
    number = max(0.0, min(1.0, number))
    return int(round(number * 255))


def format_peer(address: Any) -> str:
    if isinstance(address, tuple) and len(address) >= 2:
        return f"{address[0]}:{address[1]}"
    return "unknown"


async def send_json(ws, message: Dict[str, Any]) -> bool:
    """Send one JSON control message. Returns False if the phone is already gone."""
    try:
        await ws.send(json.dumps(message))
        return True
    except Exception:  # noqa: BLE001 - the phone may have disconnected
        return False


# ---------------------------------------------------------------------------
# License storage (license.json)
# ---------------------------------------------------------------------------

class LicenseStore:
    """
    Persists this PC's license tier in license.json.

    File layout:
      {
        "tier": "FREE" | "PRO" | "ULTIMATE",
        "transaction_id": "...",                  # payment that unlocked the current tier
        "tx_ref": "gp-PRO-...",
        "used_transaction_ids": ["...", "..."],   # every payment this PC has accepted
        "updated_at": "2025-01-01T12:00:00+00:00"
      }

    A local file can be edited by anyone with access to this PC. Treat it as a
    record of purchase, not as copy protection.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self.tier: str = TIER_FREE
        self.transaction_id: Optional[str] = None
        self.tx_ref: Optional[str] = None
        self.used_transaction_ids: List[str] = []
        self.updated_at: Optional[str] = None
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            log_license.info("No license.json found. Starting on the FREE tier.")
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("license.json must contain a JSON object")
        except (OSError, ValueError):
            # A damaged file must never stop the server. Start FREE and leave the file alone.
            log_license.exception("license.json could not be read. Starting on the FREE tier.")
            return

        tier = data.get("tier")
        if tier in TIER_RANK:
            self.tier = tier
        else:
            log_license.warning("Unknown tier %r in license.json. Using FREE.", tier)

        self.transaction_id = data.get("transaction_id")
        self.tx_ref = data.get("tx_ref")
        self.updated_at = data.get("updated_at")
        used = data.get("used_transaction_ids", [])
        if isinstance(used, list):
            self.used_transaction_ids = [str(item) for item in used]
        log_license.info("License loaded. Host tier: %s.", TIER_LABEL[self.tier])

    def _write(self, data: Dict[str, Any]) -> None:
        # Write to a temporary file first, so a crash mid-write cannot corrupt the license.
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def capacity(self) -> int:
        """How many phones the host allows right now."""
        return TIER_CAPACITY[self.tier]

    def features(self) -> Dict[str, bool]:
        """Feature flags for phones. Applies to every phone connected to this host."""
        rank = TIER_RANK[self.tier]
        return {name: rank >= TIER_RANK[minimum] for name, minimum in FEATURE_MIN_TIER.items()}

    def is_used(self, transaction_id: str) -> bool:
        return transaction_id in self.used_transaction_ids

    def record_payment(self, tier: str, transaction_id: str, tx_ref: str) -> bool:
        """Save a verified payment. Returns True if the host tier went up."""
        with self._lock:
            upgraded = TIER_RANK[tier] > TIER_RANK[self.tier]
            new_tier = tier if upgraded else self.tier
            new_txn = transaction_id if upgraded else self.transaction_id
            new_ref = tx_ref if upgraded else self.tx_ref
            used = self.used_transaction_ids + [transaction_id]
            updated = datetime.now(timezone.utc).isoformat(timespec="seconds")

            # Write to disk first. Memory is updated only after the write succeeds.
            self._write({
                "tier": new_tier,
                "transaction_id": new_txn,
                "tx_ref": new_ref,
                "used_transaction_ids": used,
                "updated_at": updated,
            })
            self.tier = new_tier
            self.transaction_id = new_txn
            self.tx_ref = new_ref
            self.used_transaction_ids = used
            self.updated_at = updated
            return upgraded


# ---------------------------------------------------------------------------
# Flutterwave verification
# ---------------------------------------------------------------------------

class PaymentError(Exception):
    """A payment could not be accepted. The message is safe to show on the phone."""

    def __init__(self, message: str, retry: bool = False) -> None:
        super().__init__(message)
        self.retry = retry  # True when trying again later may succeed (network, pending)


def verify_flutterwave_transaction(transaction_id: str) -> Dict[str, Any]:
    """
    Ask Flutterwave whether a transaction succeeded.

    Calls GET https://api.flutterwave.com/v3/transactions/{id}/verify with the secret key
    and returns the "data" object from the response. Raises PaymentError on any failure.
    This runs in a worker thread, so it does not block the WebSocket server.
    """
    if not FLUTTERWAVE_SECRET_KEY:
        raise PaymentError("Payments are not configured on this PC. Add FLUTTERWAVE_SECRET_KEY to .env.")

    url = FLUTTERWAVE_VERIFY_URL.format(id=urllib.parse.quote(str(transaction_id), safe=""))
    request = urllib.request.Request(url, method="GET", headers={
        "Authorization": f"Bearer {FLUTTERWAVE_SECRET_KEY}",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        log_license.warning("Flutterwave verify returned HTTP %d.", exc.code)
        raise PaymentError("Flutterwave rejected the verification request. Check the PC's secret key.") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise PaymentError("Could not reach Flutterwave. Check the PC's internet connection.", retry=True) from exc
    except ValueError as exc:
        raise PaymentError("Flutterwave returned an unreadable response.", retry=True) from exc

    if body.get("status") != "success" or not isinstance(body.get("data"), dict):
        raise PaymentError("Flutterwave could not find this transaction.")
    return body["data"]


def validate_payment(data: Dict[str, Any], transaction_id: str, claimed_tx_ref: str) -> Tuple[str, str]:
    """
    Check Flutterwave's record of the payment. Returns (tier, tx_ref).

    The tier comes from the tx_ref that Flutterwave stored, not from the phone's claim.
    The amount is checked against PRICE_TABLE, so a cheap payment cannot unlock a higher tier.
    """
    status = str(data.get("status", "")).lower()
    if status == "pending":
        raise PaymentError("This payment is still pending. Try again in a minute.", retry=True)
    if status != "successful":
        raise PaymentError("Flutterwave does not show this payment as successful.")

    if str(data.get("id", "")) != transaction_id:
        raise PaymentError("The payment record does not match this transaction ID.")

    tx_ref = str(data.get("tx_ref", ""))
    match = TX_REF_PATTERN.match(tx_ref)
    if not match:
        raise PaymentError("This payment was not made for the gamepad app.")
    if claimed_tx_ref and claimed_tx_ref != tx_ref:
        raise PaymentError("The payment reference does not match Flutterwave's record.")

    tier = match.group(1)
    currency = str(data.get("currency", "")).upper()
    required = PRICE_TABLE[tier].get(currency)
    if required is None:
        raise PaymentError(f"{currency or 'This currency'} is not accepted for {TIER_LABEL[tier]}.")

    try:
        paid = float(data.get("amount", 0))
    except (TypeError, ValueError):
        raise PaymentError("The amount paid could not be read.")
    if paid + 1e-9 < required:
        raise PaymentError("The amount paid is less than the price for this tier.")

    return tier, tx_ref


# ---------------------------------------------------------------------------
# Virtual controller
# ---------------------------------------------------------------------------

class VirtualXbox360:
    """One virtual Xbox 360 controller that mirrors one browser player's state."""

    def __init__(self, player_number: int) -> None:
        self.player_number = player_number
        # Plugging in the pad creates a virtual Xbox 360 controller in Windows.
        self.pad = vg.VX360Gamepad()
        self._button_cache: Dict[int, bool] = {button: False for button in BUTTON_MAP.values()}
        self._left_trigger: int = 0
        self._right_trigger: int = 0
        self._left_stick: Tuple[float, float] = (0.0, 0.0)
        self._right_stick: Tuple[float, float] = (0.0, 0.0)
        self._active = False  # True once any state has been applied since the last reset
        self._closed = False
        self.reset()

    def apply(self, state: Dict[str, Any]) -> None:
        """Apply one full state snapshot and push it to Windows in a single report."""
        self._active = True
        buttons = state.get("buttons")
        if not isinstance(buttons, dict):
            buttons = {}

        # Buttons: press or release only when the value changed.
        for name, button in BUTTON_MAP.items():
            wanted = bool(buttons.get(name, False))
            if self._button_cache[button] != wanted:
                if wanted:
                    self.pad.press_button(button=button)
                else:
                    self.pad.release_button(button=button)
                self._button_cache[button] = wanted

        # Triggers: 0-255 scale.
        left_trigger = read_trigger(buttons.get("LT", False))
        if left_trigger != self._left_trigger:
            self.pad.left_trigger(value=left_trigger)
            self._left_trigger = left_trigger

        right_trigger = read_trigger(buttons.get("RT", False))
        if right_trigger != self._right_trigger:
            self.pad.right_trigger(value=right_trigger)
            self._right_trigger = right_trigger

        # Sticks: the browser's Y is positive when pushed down, while Xbox's Y is
        # positive when pushed up, so Y is inverted here.
        left = read_stick(state.get("leftStick"))
        if left != self._left_stick:
            self.pad.left_joystick_float(x_value_float=left[0], y_value_float=-left[1])
            self._left_stick = left

        right = read_stick(state.get("rightStick"))
        if right != self._right_stick:
            self.pad.right_joystick_float(x_value_float=right[0], y_value_float=-right[1])
            self._right_stick = right

        self.pad.update()

    def reset(self) -> None:
        """Return every input to neutral. Safe to call more than once."""
        if self._closed:
            return
        self.pad.reset()
        self.pad.update()
        for button in self._button_cache:
            self._button_cache[button] = False
        self._left_trigger = 0
        self._right_trigger = 0
        self._left_stick = (0.0, 0.0)
        self._right_stick = (0.0, 0.0)
        self._active = False

    def reset_if_active(self) -> None:
        """Release inputs only if they are currently held (used by the watchdog)."""
        if self._active:
            self.reset()

    def close(self) -> None:
        """Release all inputs and unplug the virtual controller from Windows."""
        if self._closed:
            return
        try:
            self.reset()
        except Exception:  # noqa: BLE001 - best effort during teardown
            log_players.exception("Player %d: failed to reset controller during teardown.",
                                  self.player_number)
        try:
            # Explicitly clear the pad reference so Windows unplugs it immediately
            del self.pad
        except Exception:  # noqa: BLE001 - best effort during teardown
            log_players.exception("Player %d: failed to unplug controller.", self.player_number)


# ---------------------------------------------------------------------------
# Player registry (up to MAX_PLAYERS, limited by the host tier)
# ---------------------------------------------------------------------------

class SlotsFullError(Exception):
    """Raised when no player slot is available."""


@dataclass
class Player:
    number: int
    peer: str
    controller: VirtualXbox360
    ws: Any = None
    bad_packets: int = 0


class PlayerRegistry:
    """
    Tracks which player numbers are in use.

    A virtual controller is created when a phone connects and unplugged when it leaves.
    The number of usable slots is capped by the host tier through capacity_fn.
    """

    def __init__(self, max_players: int, capacity_fn: Callable[[], int]) -> None:
        self.max_players = max_players
        self._capacity_fn = capacity_fn
        self._players: Dict[int, Player] = {}

    def __len__(self) -> int:
        return len(self._players)

    def capacity(self) -> int:
        """Phones allowed right now: the tier limit, never above MAX_PLAYERS."""
        return max(1, min(self.max_players, self._capacity_fn()))

    def is_full(self) -> bool:
        return len(self._players) >= self.capacity()

    def players(self) -> List[Player]:
        return list(self._players.values())

    def acquire(self, peer: str, ws: Any) -> Player:
        """Assign the lowest free player number and create its controller."""
        if self.is_full():
            raise SlotsFullError()
        for number in range(1, self.max_players + 1):
            if number not in self._players:
                controller = VirtualXbox360(number)
                player = Player(number=number, peer=peer, controller=controller, ws=ws)
                self._players[number] = player
                return player
        raise SlotsFullError()

    def release(self, player: Player) -> None:
        """Reset and unplug a player's controller, then free the slot."""
        if self._players.get(player.number) is player:
            del self._players[player.number]
        player.controller.close()

    def close_all(self) -> None:
        for player in list(self._players.values()):
            self.release(player)


# ---------------------------------------------------------------------------
# Host state: license, control messages, and payment handling
# ---------------------------------------------------------------------------

class HostState:
    """Connects the license store, the player registry, and the control-message handlers."""

    def __init__(self, license_store: LicenseStore, registry: PlayerRegistry) -> None:
        self.license = license_store
        self.registry = registry
        self._payment_lock: Optional[asyncio.Lock] = None  # created on first use, inside the event loop
        self._tasks: Set[asyncio.Task] = set()

    def status_message(self, player_number: Optional[int] = None) -> Dict[str, Any]:
        """The host status sent to a phone on connect and on request."""
        return {
            "type": "status",
            "tier": self.license.tier,
            "features": self.license.features(),
            "capacity": self.registry.capacity(),
            "player": player_number,
        }

    def http_summary(self) -> Dict[str, Any]:
        """The status returned by GET /api/license."""
        summary = self.status_message()
        summary.pop("player", None)
        summary["players_connected"] = len(self.registry)
        return summary

    async def handle_control(self, ws, message: Dict[str, Any], player: Player) -> None:
        """Route a control message (any message that has a "type" field)."""
        kind = message.get("type")
        if kind == "get_status":
            await send_json(ws, self.status_message(player.number))
        elif kind == "verify_payment":
            # Verification talks to Flutterwave and can take seconds. Run it as a background
            # task so this phone's input stream keeps flowing while it runs.
            task = asyncio.create_task(self._verify_and_reply(ws, message, player))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        else:
            log_players.debug("Player %d sent an unknown control message: %r", player.number, kind)

    async def _verify_and_reply(self, ws, message: Dict[str, Any], player: Player) -> None:
        reply, upgraded = await self.verify_payment(message, player.number)
        await send_json(ws, reply)
        if upgraded:
            await self.broadcast({
                "type": "TIER_UPGRADED",
                "tier": self.license.tier,
                "features": self.license.features(),
                "capacity": self.registry.capacity(),
            })

    async def verify_payment(self, message: Dict[str, Any], player_number: int) -> Tuple[Dict[str, Any], bool]:
        """Verify one payment. Returns (reply for the phone, whether the host tier went up)."""

        def failure(text: str, retry: bool = False) -> Tuple[Dict[str, Any], bool]:
            return {"type": "payment_result", "ok": False, "message": text, "retry": retry}, False

        transaction_id = str(message.get("transaction_id", "")).strip()
        claimed_tx_ref = str(message.get("tx_ref", "")).strip()
        if not TRANSACTION_ID_PATTERN.match(transaction_id):
            return failure("That payment reference is not valid.")

        if self._payment_lock is None:
            self._payment_lock = asyncio.Lock()

        # One verification at a time. This stops two phones from using the same payment at once.
        async with self._payment_lock:
            if self.license.is_used(transaction_id):
                return failure("This payment has already been used on this PC.")

            try:
                data = await asyncio.to_thread(verify_flutterwave_transaction, transaction_id)
                tier, tx_ref = validate_payment(data, transaction_id, claimed_tx_ref)
            except PaymentError as exc:
                log_license.warning("Player %d: payment %s not accepted: %s",
                                    player_number, transaction_id, exc)
                return failure(str(exc), retry=exc.retry)

            try:
                upgraded = self.license.record_payment(tier, transaction_id, tx_ref)
            except OSError:
                log_license.exception("Could not write license.json.")
                return failure("The payment was confirmed, but this PC could not save the license. "
                               "Check the folder permissions and try again.", retry=True)

        if upgraded:
            log_license.info("Payment %s accepted. Host tier is now %s.",
                             transaction_id, TIER_LABEL[self.license.tier])
            text = f"{TIER_LABEL[self.license.tier]} unlocked on this PC."
        else:
            log_license.info("Payment %s accepted. Host already has %s or higher.",
                             transaction_id, TIER_LABEL[self.license.tier])
            text = f"This PC already has {TIER_LABEL[self.license.tier]}."
        return {"type": "payment_result", "ok": True, "tier": self.license.tier,
                "message": text, "retry": False}, upgraded

    async def broadcast(self, message: Dict[str, Any]) -> None:
        """Send one control message to every connected phone."""
        payload = json.dumps(message)
        targets = [p for p in self.registry.players() if p.ws is not None]
        results = await asyncio.gather(*(p.ws.send(payload) for p in targets), return_exceptions=True)
        for player, result in zip(targets, results):
            if isinstance(result, Exception):
                log_players.debug("Player %d: broadcast skipped (%s).", player.number, result)


# ---------------------------------------------------------------------------
# Packet decoding and state application
# ---------------------------------------------------------------------------

def _note_bad_packet(player: Player, reason: str) -> None:
    player.bad_packets += 1
    if player.bad_packets == 1 or player.bad_packets % BAD_PACKET_LOG_EVERY == 0:
        log_packets.warning("Player %d: %s, ignored (occurrence %d).",
                            player.number, reason, player.bad_packets)


def decode_message(raw: Any, player: Player) -> Optional[Dict[str, Any]]:
    """Parse one incoming WebSocket message. Returns None (and logs) if it is unusable."""
    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        if len(raw) > MAX_MESSAGE_BYTES:
            _note_bad_packet(player, f"oversized packet ({len(raw)} bytes)")
            return None
        message = json.loads(raw)
        if not isinstance(message, dict):
            _note_bad_packet(player, "packet is not a JSON object")
            return None
        return message
    except UnicodeDecodeError:
        _note_bad_packet(player, "packet is not valid UTF-8")
    except ValueError:
        _note_bad_packet(player, "malformed JSON")
    except Exception:  # noqa: BLE001 - keep the session alive, log the traceback
        player.bad_packets += 1
        if player.bad_packets == 1 or player.bad_packets % BAD_PACKET_LOG_EVERY == 0:
            log_packets.exception("Player %d: unexpected error decoding packet (occurrence %d).",
                                  player.number, player.bad_packets)
    return None


def apply_state(state: Dict[str, Any], player: Player) -> None:
    """Apply one gamepad state packet. Never raises, so one bad packet cannot end a session."""
    try:
        player.controller.apply(state)
    except Exception:  # noqa: BLE001 - keep the session alive, log the traceback
        player.bad_packets += 1
        if player.bad_packets == 1 or player.bad_packets % BAD_PACKET_LOG_EVERY == 0:
            log_packets.exception("Player %d: unexpected error applying packet (occurrence %d).",
                                  player.number, player.bad_packets)


# ---------------------------------------------------------------------------
# WebSocket connection handling
# ---------------------------------------------------------------------------

async def receive_loop(ws, player: Player, host: HostState) -> None:
    """Read messages for one player until the connection ends."""
    while True:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=WATCHDOG_TIMEOUT_S)
        except asyncio.TimeoutError:
            # Silence for WATCHDOG_TIMEOUT_S: the phone may have locked or lost Wi-Fi.
            # Release this player's inputs and keep waiting.
            player.controller.reset_if_active()
            continue

        message = decode_message(raw, player)
        if message is None:
            continue
        if "type" in message:
            await host.handle_control(ws, message, player)
        else:
            apply_state(message, player)


async def handle_client(ws, host: HostState) -> None:
    registry = host.registry
    peer = format_peer(getattr(ws, "remote_address", None))

    # License gate: refuse before a virtual controller is created.
    if registry.is_full():
        if registry.capacity() < MAX_PLAYERS:
            log_players.warning("Rejected %s: host tier %s allows %d phone(s). Host Upgrade Required.",
                                peer, TIER_LABEL[host.license.tier], registry.capacity())
            await send_json(ws, {
                "type": "error",
                "code": "HOST_UPGRADE_REQUIRED",
                "message": "Host Upgrade Required. This PC's plan allows one phone. "
                           "Ultimate unlocks up to four.",
            })
            await _close_quietly(ws, CLOSE_UPGRADE_REQUIRED, "Host Upgrade Required")
        else:
            log_players.warning("Rejected %s: all %d player slots are in use.", peer, registry.capacity())
            await send_json(ws, {"type": "error", "code": "SLOTS_FULL",
                                 "message": "All player slots are in use."})
            await _close_quietly(ws, 1013, "All player slots are full")
        return

    try:
        player = registry.acquire(peer, ws)
    except SlotsFullError:
        await _close_quietly(ws, 1013, "All player slots are full")
        return
    except Exception:  # noqa: BLE001 - e.g. ViGEmBus missing or a driver error
        log_players.exception("Could not create a virtual controller for %s.", peer)
        await _close_quietly(ws, 1011, "Virtual controller unavailable")
        return

    log_players.info("Player %d connected from %s (%d/%d slots in use, host tier %s).",
                     player.number, peer, len(registry), registry.capacity(),
                     TIER_LABEL[host.license.tier])
    await send_json(ws, host.status_message(player.number))

    reason = "unknown"
    try:
        await receive_loop(ws, player, host)
    except ConnectionClosed as exc:
        reason = f"connection closed (code {exc.code or 'none'})"
    except asyncio.CancelledError:
        reason = "server shutting down"
        raise
    except Exception:  # noqa: BLE001 - never let one client crash the server
        reason = "unexpected error"
        log_players.exception("Player %d: unexpected error in receive loop.", player.number)
    finally:
        registry.release(player)
        log_players.warning("Player %d disconnected from %s: %s. Inputs reset, slot freed (%d/%d in use).",
                            player.number, peer, reason, len(registry), registry.capacity())


async def _close_quietly(ws, code: int, reason: str) -> None:
    try:
        await ws.close(code=code, reason=reason)
    except Exception:  # noqa: BLE001 - the peer may already be gone
        pass


async def run_websocket_server(host_addr: str, port: int, host: HostState) -> None:
    async def handler(ws) -> None:
        await handle_client(ws, host)

    async with websockets.serve(
        handler,
        host_addr,
        port,
        max_size=MAX_MESSAGE_BYTES,
        ping_interval=10,
        ping_timeout=10,
    ):
        log_server.info("WebSocket server listening on ws://%s:%d", host_addr, port)
        await asyncio.Future()  # Run until cancelled.


# ---------------------------------------------------------------------------
# HTTP server (serves index.html and GET /api/license on a background thread)
# ---------------------------------------------------------------------------

class QuietHandler(SimpleHTTPRequestHandler):
    """Routes HTTP access logs to the debug log instead of stderr."""

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        log_http.debug("%s - %s", self.address_string(), format % args)


def make_http_handler(web_root: Path, status_provider: Callable[[], Dict[str, Any]]) -> type:
    """Build a request handler that serves web_root and answers GET /api/license."""

    class Handler(QuietHandler):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, directory=str(web_root), **kwargs)

        def do_GET(self) -> None:  # noqa: N802 - name required by http.server
            path = self.path.split("?", 1)[0]
            if path == "/api/license":
                # Read-only host status. Payment verification happens over the WebSocket.
                body = json.dumps(status_provider()).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return
            super().do_GET()

    return Handler


def start_http_server(host: str, port: int, web_root: Path,
                      status_provider: Callable[[], Dict[str, Any]]) -> ThreadingHTTPServer:
    handler = make_http_handler(web_root, status_provider)
    httpd = ThreadingHTTPServer((host, port), handler)
    thread = threading.Thread(target=httpd.serve_forever, name="http-server", daemon=True)
    thread.start()
    return httpd


# ---------------------------------------------------------------------------
# Network discovery and QR code
# ---------------------------------------------------------------------------

def _is_usable_ipv4(ip: str) -> bool:
    return not (ip.startswith("127.") or ip.startswith("0.") or ip.startswith("169.254."))


def detect_lan_ip() -> str:
    """
    Find this PC's LAN IPv4 address without sending any traffic.

    Connecting a UDP socket makes the OS pick the interface it would use to reach
    the local network, and that interface's address is the one phones can reach.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("10.255.255.255", 1))
            ip = sock.getsockname()[0]
            if _is_usable_ipv4(ip):
                return ip
    except OSError:
        pass

    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if _is_usable_ipv4(ip):
                return ip
    except OSError:
        pass

    log_server.warning("Could not detect a LAN address. Falling back to 127.0.0.1. "
                       "Use --ip to set it manually.")
    return "127.0.0.1"


def print_qr(url: str) -> None:
    if qrcode is None:
        log_qr.warning("The 'qrcode' package is not installed (pip install qrcode). "
                       "Enter this address on your phone instead: %s", url)
        return
    try:
        qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_L, border=2) # type: ignore
        qr.add_data(url)
        qr.make(fit=True)
        print()
        log_qr.info("Scan this QR code in the app to connect to this PC:")
        print()
        qr.print_ascii(invert=True)
        print()
        log_qr.info("Or type: %s", paint(url, Color.BOLD))
    except Exception:  # noqa: BLE001 - QR is a convenience, never fatal
        log_qr.exception("Could not generate the QR code. Enter this address manually: %s", url)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_args(argv: Optional[list] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Mobile Gamepad PC companion server")
    parser.add_argument("--bind", default=DEFAULT_BIND,
                        help="Interface to bind both servers to (default: %(default)s)")
    parser.add_argument("--ip", default=None,
                        help="LAN IP to show in the QR code (default: auto-detect)")
    parser.add_argument("--ws-port", type=int, default=DEFAULT_WS_PORT,
                        help="WebSocket port (default: %(default)s)")
    parser.add_argument("--http-port", type=int, default=DEFAULT_HTTP_PORT,
                        help="HTTP port for index.html (default: %(default)s)")
    parser.add_argument("--web-root", default=str(Path(__file__).resolve().parent),
                        help="Folder containing index.html (default: bundled page folder)")
    parser.add_argument("--license-file", default=str(LICENSE_FILE),
                        help="Path to license.json (default: next to the EXE or script)")
    parser.add_argument("--no-qr", action="store_true", help="Do not print the QR code")
    parser.add_argument("--no-color", action="store_true", help="Disable colored output")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        help="Logging verbosity (default: %(default)s)")
    return parser.parse_args(argv)


def main(argv: Optional[list] = None) -> int:
    global USE_COLOR
    args = parse_args(argv)
    USE_COLOR = enable_terminal_colors(args.no_color)
    configure_logging(args.log_level)

    if ENV_FILE.is_file():
        log_license.info("Loaded settings from %s.", ENV_FILE)
    if not FLUTTERWAVE_SECRET_KEY:
        log_license.warning("FLUTTERWAVE_SECRET_KEY is not set. Upgrades will fail until it is added to .env.")

    web_root = Path(args.web_root).resolve()
    if not (web_root / "index.html").is_file():
        log_server.error("index.html not found in %s. Use --web-root to point at the folder "
                         "that contains it.", web_root)
        return 1

    license_store = LicenseStore(Path(args.license_file))
    registry = PlayerRegistry(MAX_PLAYERS, capacity_fn=license_store.capacity)
    host_state = HostState(license_store, registry)

    lan_ip = args.ip or detect_lan_ip()
    page_url = f"http://{lan_ip}:{args.http_port}/index.html"
    ws_url = f"ws://{lan_ip}:{args.ws_port}"

    log_server.info("Mobile Gamepad server starting (host tier %s, up to %d phone(s)).",
                    paint(TIER_LABEL[license_store.tier], Color.BOLD), registry.capacity())
    log_server.info("Binding to %s. Your PC's LAN address is %s.",
                    paint(args.bind, Color.BOLD), paint(lan_ip, Color.BOLD))

    try:
        httpd = start_http_server(args.bind, args.http_port, web_root, host_state.http_summary)
    except OSError as exc:
        log_http.error("Could not bind HTTP on port %d: %s. Another program may be using it. "
                       "Try --http-port 8080.", args.http_port, exc)
        return 1
    log_http.info("Serving %s on http://%s:%d", web_root, args.bind, args.http_port)

    if not args.no_qr:
        print_qr(ws_url)
    else:
        log_qr.info("Connect your phone to: %s", paint(ws_url, Color.BOLD))
    log_qr.info("Page for this PC: %s", page_url)

    exit_code = 0
    try:
        asyncio.run(run_websocket_server(args.bind, args.ws_port, host_state))
    except KeyboardInterrupt:
        log_server.info("Stopped by user (Ctrl+C).")
    except OSError as exc:
        log_server.error("Could not bind WebSocket on port %d: %s. Another program may be "
                         "using it. Try --ws-port 5001.", args.ws_port, exc)
        exit_code = 1
    except Exception:  # noqa: BLE001 - top-level safety net
        log_server.exception("Unexpected fatal error.")
        exit_code = 1
    finally:
        registry.close_all()
        httpd.shutdown()
        httpd.server_close()
        log_server.info("All controllers released and servers stopped.")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())