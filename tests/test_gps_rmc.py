import unittest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from gps_rmc import parse_rmc_line, parse_rmc_timestamp

def rmc(time='120000.00', speed='0', status='A', mode='A'):
    body = f'GPRMC,{time},{status},4000.000,N,08600.000,W,{speed},0,051126,,,{mode}'
    check = 0
    for char in body:
        check ^= ord(char)
    return f'${body}*{check:02X}'


class ParserTests(unittest.TestCase):
    def test_valid_gps_time_and_speed(self):
        line = rmc(time='120001.25', speed='52.1386')
        self.assertTrue(parse_rmc_line(line)[2])
        self.assertAlmostEqual(parse_rmc_line(line)[1], 60, places=3)
        self.assertEqual(parse_rmc_timestamp(line)-parse_rmc_timestamp(rmc()), 1.25)

    def test_corrupt_missing_and_invalid_fixes(self):
        for line in (rmc()[:-2]+'FF', rmc().split('*')[0], rmc(speed='nan'),
                     rmc(speed=''), rmc(speed='-1'), rmc(status='V'),
                     rmc(mode='E'), rmc(mode='S'), rmc(time='246000')):
            with self.subTest(line=line): self.assertFalse(parse_rmc_line(line)[2])

    def test_midnight(self):
        a = rmc(time='235959.50').replace('051126', '041126')
        self.assertEqual(parse_rmc_timestamp(rmc(time='000000.50'))-parse_rmc_timestamp(a), 1)


if __name__ == '__main__':
    unittest.main()
