import unittest
import csv
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch
import asyncio

from battery_core import AppleAdvertisements, DeviceState, make_mouse_request, parse_apple_proximity, parse_mouse_reply, read_beats, read_mouse
from app import append_history, format_level, icon_image, new_history_path, position_visible, tooltip


class BatteryCoreTests(unittest.TestCase):
    def test_mouse_real_receiver_reply(self):
        frame = bytes.fromhex(
            "4d 01 01 10 00 09 00 00 37 38 26 40 04 00 00 00 "
            "00 10 02 00 28 00 08 e4 a3" + " 00" * 39
        )
        self.assertEqual(parse_mouse_reply(frame), DeviceState(True, 40, "USB 接收器"))
        self.assertEqual(make_mouse_request()[:9], bytes.fromhex("4d 01 01 00 00 09 00 00 08"))
        broken = bytearray(frame)
        broken[20] = 99
        self.assertIsNone(parse_mouse_reply(broken))

    def test_beats_parts_and_missing_values(self):
        packet = bytes.fromhex("07 19 01 12 20 20 76 08 00")
        reading = parse_apple_proximity(packet)
        self.assertEqual((reading.left, reading.right, reading.case), (60, 70, 80))
        self.assertIsNone(parse_apple_proximity(bytes.fromhex("07 19 01 11 20 20 76 08 00")))
        self.assertIsNone(parse_apple_proximity(bytes.fromhex("07 19 01 12 20 20 ff 0f 00")))
        self.assertIsNone(parse_apple_proximity(bytes.fromhex("07 05 01 12 20 20 76 08 00")))

    def test_advertisement_requires_repeat_and_expires(self):
        ads = AppleAdvertisements()
        packet = bytes.fromhex("07 19 01 12 20 20 76 08 00")
        ads.record("a", -40, packet)
        self.assertIsNone(ads.best())
        ads.record("a", -40, packet)
        self.assertEqual(ads.best().left, 60)
        moment, rssi, count, reading = ads._seen["a"]
        ads._seen["a"] = (moment - 31, rssi, count, reading)
        self.assertIsNone(ads.best())

    def test_disconnected_and_unknown_ui(self):
        self.assertIn("未连接", tooltip("Beats", DeviceState()))
        self.assertEqual(format_level(None), "--")
        self.assertIn("--", tooltip("Beats", DeviceState(True, source="Windows")))
        self.assertFalse(position_visible(100000, 100000, 320, 187))

    def test_tray_number_changes_with_level(self):
        low = icon_image("mouse", DeviceState(True, 8, "USB"))
        high = icon_image("mouse", DeviceState(True, 100, "USB"))
        self.assertNotEqual(low.crop((15, 15, 56, 44)).tobytes(),
                            high.crop((15, 15, 56, 44)).tobytes())
        self.assertNotEqual(icon_image("beats", DeviceState()).tobytes(),
                            icon_image("beats", DeviceState(True, 0, "Windows")).tobytes())

    def test_history_csv_contains_both_devices_and_missing_readings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "battery_history.csv"
            when = datetime(2026, 10, 20, 14, 35, tzinfo=timezone.utc)
            states = {"beats": DeviceState(True, 60, "耳机广播", left=60, right=70),
                      "mouse": DeviceState()}
            append_history(path, states, when)
            append_history(path, states, when)
            with path.open(encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), 4)
            self.assertEqual(rows[0]["time"], "2026-10-20T14:35:00+00:00")
            self.assertEqual((rows[0]["left"], rows[0]["right"]), ("60", "70"))
            self.assertEqual((rows[1]["device"], rows[1]["connected"], rows[1]["level"]),
                             ("mouse", "0", ""))

    def test_new_history_path_creates_unique_session_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            when = datetime(2026, 10, 20, 14, 35, tzinfo=timezone.utc)
            first = new_history_path(root, when)
            second = new_history_path(root, when)
            self.assertNotEqual(first, second)
            self.assertTrue(first.name.startswith("battery_history_20261020_143500"))
            self.assertEqual(first.read_text(encoding="utf-8-sig").splitlines()[0],
                             "time,device,connected,level,left,right,case,charging,source")

    def test_beats_prefers_parts_then_windows_fallback(self):
        ads = AppleAdvertisements()
        packet = bytes.fromhex("07 19 01 12 20 20 76 08 00")
        with patch("battery_core.bluetooth_connected", new=AsyncMock(return_value=True)):
            aggregate = lambda: 55
            self.assertEqual(asyncio.run(read_beats(ads, aggregate)).level, 55)
            ads.record("own-device", -35, packet)
            ads.record("own-device", -35, packet)
            state = asyncio.run(read_beats(ads, aggregate))
            self.assertEqual((state.level, state.left, state.right, state.case),
                             (60, 60, 70, 80))
        with patch("battery_core.bluetooth_connected", new=AsyncMock(return_value=False)):
            self.assertEqual(asyncio.run(read_beats(ads, lambda: 55)), DeviceState())

    def test_mouse_prefers_live_usb_then_bluetooth(self):
        usb = DeviceState(True, 40, "USB 接收器")
        ble = DeviceState(True, 75, "蓝牙")
        with patch("battery_core.read_mouse_usb", return_value=usb), \
             patch("battery_core.read_mouse_ble", new=AsyncMock(return_value=ble)) as read_ble:
            self.assertEqual(asyncio.run(read_mouse()), usb)
            read_ble.assert_not_awaited()
        with patch("battery_core.read_mouse_usb", return_value=None), \
             patch("battery_core.read_mouse_ble", new=AsyncMock(return_value=ble)):
            self.assertEqual(asyncio.run(read_mouse()), ble)


if __name__ == "__main__":
    unittest.main()
