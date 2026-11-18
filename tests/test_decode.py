import math
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import victor_dmm as v  # noqa: E402


def scramble(pkt):
    """Inverse of victor_dmm.unscramble, to build test reports."""
    raw = bytearray(14)
    for i in range(14):
        raw[i] = (v._bit_reverse(pkt[13 - v._SHUFFLE[i]]) + v._OBFUSCATION[i]) & 0xFF
    return bytes(raw)


def pkt(digits, dp, b7=0, b8=0, b9=0, b10=0, b11=0):
    return digits.encode() + b" " + dp.encode() + bytes([b7, b8, b9, b10, b11]) + b"\r\n"


class DecodeTest(unittest.TestCase):
    def check(self, p, display, value, flags=None):
        r = v.decode(v.unscramble(scramble(p)))
        self.assertEqual(r.display, display)
        if math.isinf(value):
            self.assertEqual(r.value, value)
        else:
            self.assertAlmostEqual(r.value, value)
        if flags is not None:
            self.assertEqual(r.flags, flags)
        return r

    def test_millivolt_dc_auto(self):
        self.check(pkt("+1234", "4", b7=0x30, b9=0x40, b10=0x80), "123.4 mV", 0.1234, ["DC", "AUTO"])

    def test_negative_ac_current(self):
        self.check(pkt("-0512", "1", b7=0x08, b10=0x40), "-0.512 A", -0.512, ["AC"])

    def test_overload(self):
        r = self.check(pkt("+?0:?", "0", b7=0x20, b9=0x20, b10=0x20), "OL kOhm", math.inf)
        self.assertTrue(r.overload)

    def test_frequency(self):
        self.check(pkt("+5000", "2", b10=0x08), "50.00 Hz", 50.0)

    def test_capacitance_nano(self):
        self.check(pkt("+0470", "4", b8=0x02, b10=0x04), "47.0 nF", 47e-9)

    def test_all_zero_packet_ignored(self):
        self.assertIsNone(v.unscramble(bytes(14)))

    def test_bad_framing(self):
        with self.assertRaises(ValueError):
            v.decode(b"x" * 14)

    def test_csv_row(self):
        r = v.decode(pkt("+1234", "4", b7=0x30, b9=0x40, b10=0x80))
        row = v.csv_row(0.0, 1.5, r)
        self.assertEqual(row[1:], ["1.500", "0.1234", "V", "123.4 mV", "voltage", "DC AUTO"])


if __name__ == "__main__":
    unittest.main()
