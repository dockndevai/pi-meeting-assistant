"""
arm/drivers.py — where poses go: the on-screen simulator or real servos.

    ARM_DRIVER=sim        (default) no hardware; screens show the pose
    ARM_DRIVER=pca9685    PCA9685 16-channel PWM board on I2C bus 1

Decision: talk to the PCA9685 with smbus2 directly
  It's six registers. A direct driver avoids pulling the Adafruit Blinka
  stack into the image and works on any Linux I2C bus.

Calibration is env-driven so a re-assembled arm needs no code change:
    ARM_CHANNELS=base:0,shoulder:1,elbow:2,claw:3
    ARM_PULSE_US=500,2500          pulse width at 0° and 180°
    ARM_TRIM=base:0,shoulder:-4    per-joint degree offset (assembly error)
    ARM_INVERT=elbow               joints mounted mirror-image
"""

from __future__ import annotations

import os
import time

from arm.motion import Pose


def _parse_map(raw: str, cast=float) -> dict:
    out = {}
    for part in filter(None, (p.strip() for p in raw.split(","))):
        k, _, v = part.partition(":")
        out[k.strip()] = cast(v) if v else True
    return out


class SimDriver:
    name = "simulator"

    def write(self, pose: Pose) -> None:
        pass


class PCA9685Driver:
    name = "pca9685"
    _MODE1, _PRESCALE, _LED0_ON_L = 0x00, 0xFE, 0x06

    def __init__(self):
        from smbus2 import SMBus

        self.addr = int(os.environ.get("ARM_I2C_ADDR", "0x40"), 16)
        self.bus = SMBus(int(os.environ.get("ARM_I2C_BUS", "1")))
        self.channels = _parse_map(os.environ.get("ARM_CHANNELS", "base:0,shoulder:1,elbow:2,claw:3"), int)
        lo, hi = (float(x) for x in os.environ.get("ARM_PULSE_US", "500,2500").split(","))
        self.pulse = (lo, hi)
        self.trim = _parse_map(os.environ.get("ARM_TRIM", ""))
        self.invert = set(_parse_map(os.environ.get("ARM_INVERT", "")))
        self._last: dict[str, int] = {}
        self._set_freq(50)

    def _set_freq(self, hz: int) -> None:
        prescale = round(25_000_000 / (4096 * hz)) - 1
        old = self.bus.read_byte_data(self.addr, self._MODE1)
        self.bus.write_byte_data(self.addr, self._MODE1, (old & 0x7F) | 0x10)   # sleep
        self.bus.write_byte_data(self.addr, self._PRESCALE, prescale)
        self.bus.write_byte_data(self.addr, self._MODE1, old & ~0x10)
        time.sleep(0.005)
        self.bus.write_byte_data(self.addr, self._MODE1, (old & ~0x10) | 0xA0)  # restart + auto-increment

    def write(self, pose: Pose) -> None:
        for joint, deg in pose.items():
            ch = self.channels.get(joint)
            if ch is None:
                continue
            deg = deg + float(self.trim.get(joint, 0))
            if joint in self.invert:
                deg = 180 - deg
            deg = max(0.0, min(180.0, deg))
            us = self.pulse[0] + (self.pulse[1] - self.pulse[0]) * deg / 180
            ticks = int(us * 4096 / 20_000)          # 20 ms period at 50 Hz
            if self._last.get(joint) == ticks:
                continue                             # skip the I2C write if nothing moved
            self._last[joint] = ticks
            reg = self._LED0_ON_L + 4 * ch
            self.bus.write_i2c_block_data(self.addr, reg, [0, 0, ticks & 0xFF, ticks >> 8])


def make_driver():
    kind = os.environ.get("ARM_DRIVER", "sim").strip().lower()
    if kind == "pca9685":
        try:
            return PCA9685Driver()
        except Exception as e:
            print(f"[arm] PCA9685 unavailable ({e}); falling back to the simulator")
    return SimDriver()
