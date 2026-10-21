"""Device readers for the Beats Fit Pro and MCHOSE A7 V3 Ultra+."""

from __future__ import annotations

import asyncio
import logging
import re
import subprocess
import time
import uuid
from dataclasses import dataclass
from typing import Callable

import hid
from winrt.windows.devices.bluetooth import BluetoothDevice, BluetoothLEDevice
from winrt.windows.devices.bluetooth.genericattributeprofile import GattCommunicationStatus
from winrt.windows.storage.streams import DataReader


LOG = logging.getLogger("battery_tray")


BEATS_ADDRESS = int("F8665A4852DB", 16)
MOUSE_BLE_ADDRESS = int("C2559A4531ED", 16)
BEATS_MODEL = 0x2012
MOUSE_USB_VID = 0x3837
MOUSE_USB_PID = 0x1014
BATTERY_SERVICE = uuid.UUID("0000180f-0000-1000-8000-00805f9b34fb")
BATTERY_LEVEL = uuid.UUID("00002a19-0000-1000-8000-00805f9b34fb")


@dataclass(frozen=True)
class DeviceState:
    connected: bool = False
    level: int | None = None
    source: str = ""
    left: int | None = None
    right: int | None = None
    case: int | None = None
    charging: bool = False


@dataclass(frozen=True)
class AppleReading:
    left: int | None
    right: int | None
    case: int | None


def parse_apple_proximity(data: bytes) -> AppleReading | None:
    """Read Apple's documented proximity packet for the paired Beats model."""
    if len(data) < 9 or data[0] != 0x07 or data[1] < 6 or data[2] != 0x01:
        return None
    if int.from_bytes(data[3:5], "little") != BEATS_MODEL:
        return None

    def level(nibble: int) -> int | None:
        return nibble * 10 if nibble <= 10 else None

    first = level(data[6] & 0x0F)
    second = level(data[6] >> 4)
    left_first = bool(data[5] & 0x20)
    case = level(data[7] & 0x0F) if not data[8] & 0x08 else None
    result = AppleReading(first if left_first else second,
                          second if left_first else first, case)
    return result if any(x is not None for x in (result.left, result.right, result.case)) else None


class AppleAdvertisements:
    def __init__(self) -> None:
        self._seen: dict[str, tuple[float, int, int, AppleReading]] = {}

    def record(self, address: str, rssi: int, data: bytes) -> None:
        reading = parse_apple_proximity(data)
        if reading is None:
            return
        old = self._seen.get(address)
        count = old[2] + 1 if old and time.monotonic() - old[0] < 30 else 1
        self._seen[address] = (time.monotonic(), rssi, count, reading)

    def best(self) -> AppleReading | None:
        now = time.monotonic()
        self._seen = {key: value for key, value in self._seen.items()
                      if now - value[0] <= 30}
        candidates = [value for value in self._seen.values() if value[2] >= 2]
        return max(candidates, key=lambda x: (x[1], x[0]))[3] if candidates else None

    def clear(self) -> None:
        self._seen.clear()


def make_mouse_request() -> bytes:
    request = bytearray(64)
    request[:9] = bytes((0x4D, 1, 1, 0, 0, 9, 0, 0, 8))
    return bytes(request)


def parse_mouse_reply(frame: bytes) -> DeviceState | None:
    if len(frame) < 25 or frame[0:3] != bytes((0x4D, 1, 1)):
        return None
    payload_size = frame[3]
    if payload_size < 16 or 9 + payload_size > len(frame):
        return None
    if frame[4:6] != bytes((0, 9)) or frame[6:8] != bytes((0, 0)):
        return None
    if frame[2] & 1 and _xor(frame[2:9 + payload_size]) != 0:
        return None
    if int.from_bytes(frame[8:10], "little") != MOUSE_USB_VID:
        return None
    if int.from_bytes(frame[10:12], "little") != 0x4026:
        return None
    charge, battery = frame[19], frame[20]
    if battery > 100 or charge > 2:
        return None
    if frame[18] == 0 and battery == 0 and charge == 0:
        return DeviceState()
    return DeviceState(True, battery, "USB 接收器", charging=charge == 1)


def _xor(values: bytes) -> int:
    result = 0
    for value in values:
        result ^= value
    return result


def read_mouse_usb() -> DeviceState | None:
    devices = [d for d in hid.enumerate(MOUSE_USB_VID, MOUSE_USB_PID)
               if d["usage_page"] in (0xFF01, 0xFF0B) and d["usage"] == 1]
    for descriptor in devices:
        handle = hid.device()
        try:
            handle.open_path(descriptor["path"])
            handle.write(make_mouse_request())
            deadline = time.monotonic() + 1.5
            while time.monotonic() < deadline:
                frame = bytes(handle.read(64, max(1, int((deadline - time.monotonic()) * 1000))))
                if not frame:
                    break
                state = parse_mouse_reply(frame)
                if state is not None:
                    return state
        except (OSError, ValueError):
            continue
        finally:
            handle.close()
    return None


async def bluetooth_connected(factory: type, address: int) -> bool:
    device = await factory.from_bluetooth_address_async(address)
    if device is None:
        return False
    try:
        return int(device.connection_status) == 1
    finally:
        device.close()


async def read_mouse_ble() -> DeviceState:
    device = await BluetoothLEDevice.from_bluetooth_address_async(MOUSE_BLE_ADDRESS)
    if device is None:
        return DeviceState()
    try:
        if int(device.connection_status) != 1:
            return DeviceState()
        try:
            services = await device.get_gatt_services_for_uuid_async(BATTERY_SERVICE)
            if services.status != GattCommunicationStatus.SUCCESS:
                return DeviceState(True, source="蓝牙")
            for service in services.services:
                try:
                    characteristics = await service.get_characteristics_for_uuid_async(BATTERY_LEVEL)
                    if characteristics.status != GattCommunicationStatus.SUCCESS:
                        continue
                    for characteristic in characteristics.characteristics:
                        result = await characteristic.read_value_async()
                        if result.status == GattCommunicationStatus.SUCCESS and result.value.length:
                            value = DataReader.from_buffer(result.value).read_byte()
                            if value <= 100:
                                return DeviceState(True, value, "蓝牙")
                finally:
                    service.close()
        except Exception:
            LOG.exception("Mouse Bluetooth battery read failed")
        return DeviceState(True, source="蓝牙")
    finally:
        device.close()


def windows_beats_battery() -> int | None:
    """Use the Windows device property when a connected headset exposes one."""
    script = (
        "$d=Get-PnpDevice -Class Bluetooth | Where-Object "
        "{ $_.InstanceId -like 'BTHENUM\\DEV_F8665A4852DB*' } | Select-Object -First 1; "
        "if($d){Get-PnpDeviceProperty -InstanceId $d.InstanceId | "
        "Where-Object { $_.KeyName -match 'Battery' } | "
        "ForEach-Object { $_.Data }}"
    )
    try:
        run = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                             capture_output=True, text=True, timeout=5,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        for match in re.findall(r"\b\d{1,3}\b", run.stdout):
            value = int(match)
            if value <= 100:
                return value
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


async def read_beats(advertisements: AppleAdvertisements,
                     aggregate_reader: Callable[[], int | None]) -> DeviceState:
    if not await bluetooth_connected(BluetoothDevice, BEATS_ADDRESS):
        return DeviceState()
    parts = advertisements.best()
    if parts:
        buds = [x for x in (parts.left, parts.right) if x is not None]
        return DeviceState(True, min(buds) if buds else None, "耳机广播",
                           left=parts.left, right=parts.right, case=parts.case)
    return DeviceState(True, await asyncio.to_thread(aggregate_reader), "Windows")


async def read_mouse() -> DeviceState:
    usb = await asyncio.to_thread(read_mouse_usb)
    if usb and usb.connected:
        return usb
    return await read_mouse_ble()
