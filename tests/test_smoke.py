import unittest

import conductor


class SmokeTest(unittest.TestCase):
    def test_package_is_importable(self) -> None:
        self.assertEqual(conductor.__name__, "conductor")


if __name__ == "__main__":
    unittest.main()
