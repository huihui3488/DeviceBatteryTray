from pathlib import Path

from battery_core import DeviceState
from app import icon_image


target = Path(__file__).resolve().parent / "build" / "app.ico"
target.parent.mkdir(parents=True, exist_ok=True)
icon_image("beats", DeviceState(True, 80)).save(
    target, format="ICO", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64)]
)
