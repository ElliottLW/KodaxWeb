"""Pair a new camera with your Wi-Fi from any laptop.

A camera in pairing mode only talks to devices on its own Wi-Fi network
(``KodakCam-...``), which a server can't join. Run this on a laptop instead:

    1. Put the camera in pairing mode and connect the laptop to its network.
    2. py -m kodaxweb.setup
    3. Reconnect the laptop to your home Wi-Fi and add the camera on the
       server (by IP, or with Find cameras).

Options:  py -m kodaxweb.setup --help
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from datetime import datetime
from pathlib import Path

import aiohttp

from . import DEFAULT_DATA_DIR, setup_flow


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    return input(f"{prompt}{suffix}: ").strip() or default


def _local_timezone() -> str:
    """The laptop's IANA time zone name, if it can be worked out."""
    try:
        import tzlocal  # optional

        return str(tzlocal.get_localzone_name())
    except Exception:  # noqa: BLE001 - any failure just means "ask"
        return ""


async def _pair(args: argparse.Namespace) -> int:
    if not args.public_key.is_file():
        print(f"Missing {args.public_key}: see 'Setting up a new camera' in the README.")
        return 1

    async with aiohttp.ClientSession() as http:
        print("Looking for a camera in pairing mode...")
        camera = await setup_flow.find_camera(
            http, (args.ip,) if args.ip else setup_flow.HOTSPOT_IPS
        )
        if camera is None:
            print(
                "No camera found. Check this laptop is connected to the camera's "
                "KodakCam-... Wi-Fi network, and that the camera is in pairing mode."
            )
            return 1
        print(f"Found camera {camera['mac']} (firmware {camera['firmware']}) at {camera['ip']}.\n")

        ssid = args.ssid or _ask("Your Wi-Fi name (2.4 GHz)")
        security = "OPEN" if args.open else "WPA2"
        password = ""
        if security != "OPEN":
            password = args.password or getpass.getpass("Wi-Fi password (hidden): ")
        timezone = args.timezone or _ask("Time zone, e.g. Europe/London", _local_timezone() or "UTC")
        offset = datetime.now().astimezone().utcoffset()
        offset_minutes = int(offset.total_seconds() // 60) if offset else 0

        if not args.yes and _ask("\nSend these details to the camera? (y/n)", "y").lower() != "y":
            print("Cancelled.")
            return 1

        last: dict = {}
        async for step in setup_flow.run_setup(
            http,
            public_key=args.public_key,
            ip=camera["ip"],
            ssid=ssid,
            password=password,
            security=security,
            timezone_name=timezone,
            utc_offset_minutes=offset_minutes,
            flicker=args.flicker,
        ):
            last = step
            if step["ok"]:
                mark = "done"
            elif step.get("optional"):
                mark = "not supported (skipped)"
            else:
                mark = f"FAILED ({step['detail']})"
            print(f"  {step['step']}: {mark}")

    if last.get("ok") and last.get("step") == "Send Wi-Fi details":
        print(
            f"\nThe camera is restarting and joining '{ssid}' (about a minute)."
            "\nReconnect this laptop to your home Wi-Fi, then add the camera on the server"
            f" (its MAC address is {camera['mac']}, which helps find it in your router)."
        )
        return 0
    print("\nSetup didn't finish. Put the camera back in pairing mode and try again.")
    return 1


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="py -m kodaxweb.setup",
        description="Pair a camera in pairing mode with your Wi-Fi. Run it on a laptop "
        "connected to the camera's KodakCam-... network.",
    )
    parser.add_argument("--ssid", help="your Wi-Fi name (asked if left out)")
    parser.add_argument("--password", help="your Wi-Fi password (asked, hidden, if left out)")
    parser.add_argument("--open", action="store_true", help="the Wi-Fi has no password")
    parser.add_argument("--timezone", help="IANA time zone, e.g. Europe/London (asked if left out)")
    parser.add_argument(
        "--flicker", type=int, choices=(50, 60), default=50,
        help="mains frequency: 50 (UK/EU, default) or 60 (US)",
    )
    parser.add_argument(
        "--public-key", type=Path, default=DEFAULT_DATA_DIR / "public.pem",
        help="the setup key (default: data/public.pem)",
    )
    parser.add_argument("--ip", help="camera address, if it isn't found automatically")
    parser.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    args = parser.parse_args()
    try:
        sys.exit(asyncio.run(_pair(args)))
    except KeyboardInterrupt:
        sys.exit("\nCancelled.")


if __name__ == "__main__":
    main()
