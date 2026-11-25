import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
try:
    import victor_dmm_gui as gui  # noqa: E402
except ImportError:  # no tkinter
    gui = None


@unittest.skipIf(gui is None, "tkinter not available")
class ManualTest(unittest.TestCase):
    def test_manual_is_found(self):
        p = gui.manual_path()
        self.assertIsNotNone(p, "docs/MANUAL.html missing: run tools/manual_pdf.sh")
        with open(p, encoding="utf-8") as fh:
            self.assertIn("<h1", fh.read())


if __name__ == "__main__":
    unittest.main()
