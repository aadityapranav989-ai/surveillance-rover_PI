import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rfid  # noqa: E402
from rfid import CardStore, Mfrc522, RfidReader, parse_anticollision  # noqa: E402


class FakeRc522Spi:
    """Emulates the RC522's registers well enough for REQA + anticollision with one card."""

    def __init__(self, uid=None):
        self.registers = {rfid.VERSION_REG: 0x92, rfid.TX_CONTROL_REG: 0x80}
        self.fifo = []
        self.uid = uid
        self.writes = []

    def xfer2(self, data):
        address, value = data
        register = (address >> 1) & 0x3F
        if address & 0x80:  # read
            if register == rfid.FIFO_DATA_REG:
                return [0, self.fifo.pop(0)]
            if register == rfid.FIFO_LEVEL_REG:
                return [0, len(self.fifo)]
            return [0, self.registers.get(register, 0)]
        self.writes.append((register, value))
        if register == rfid.FIFO_DATA_REG:
            self.fifo.append(value)
        elif register == rfid.FIFO_LEVEL_REG and value & 0x80:
            self.fifo = []
        elif register == rfid.COMMAND_REG and value == rfid.CMD_TRANSCEIVE:
            sent, self.fifo = self.fifo, []
            if self.uid is None:
                self.registers[rfid.COMM_IRQ_REG] = 0x01  # timer: no card
            else:
                self.registers[rfid.COMM_IRQ_REG] = 0x30
                if sent == [rfid.PICC_REQA]:
                    self.fifo = [0x04, 0x00]  # ATQA
                elif sent == [rfid.PICC_ANTICOLLISION_CL1, 0x20]:
                    check = 0
                    for byte in self.uid:
                        check ^= byte
                    self.fifo = list(self.uid) + [check]
        elif register == rfid.COMM_IRQ_REG:
            self.registers[rfid.COMM_IRQ_REG] = 0
        else:
            self.registers[register] = value
        return [0, 0]


class Mfrc522Test(unittest.TestCase):
    def test_reads_card_uid(self):
        reader = Mfrc522(FakeRc522Spi(uid=[0xDE, 0xAD, 0xBE, 0xEF]))
        self.assertEqual(reader.read_uid(), "DEADBEEF")

    def test_no_card(self):
        self.assertIsNone(Mfrc522(FakeRc522Spi(uid=None)).read_uid())

    def test_turns_antenna_on(self):
        spi = FakeRc522Spi()
        Mfrc522(spi)
        self.assertIn((rfid.TX_CONTROL_REG, 0x83), spi.writes)

    def test_parse_rejects_bad_checksum(self):
        self.assertEqual(parse_anticollision([0x12, 0x34, 0x56, 0x78, 0x12 ^ 0x34 ^ 0x56 ^ 0x78]), "12345678")
        self.assertIsNone(parse_anticollision([0x12, 0x34, 0x56, 0x78, 0x00]))
        self.assertIsNone(parse_anticollision([0x12, 0x34]))


class ReaderTest(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "cards.json")
        self.cards = CardStore(self.path)
        self.cards.add("DEADBEEF", "Asha")
        self.taps = []
        self.now = 100.0
        self.reader = RfidReader(self.cards, self.taps.append, open_reader=None, clock=lambda: self.now)

    def poll(self, uid, advance=0.1):
        self.now += advance
        self.reader.handle_uid(uid)

    def test_authorised_and_unknown_cards(self):
        self.poll("DEADBEEF")
        self.poll(None, advance=2)
        self.poll("CAFEF00D")
        self.assertEqual(self.taps, ["Asha", None])
        self.assertFalse(self.reader.state()["last_tap"]["authorised"])

    def test_card_held_on_reader_counts_once(self):
        for _ in range(10):
            self.poll("DEADBEEF")
            self.poll(None)  # the RC522 misses a held card on alternate polls
        self.assertEqual(self.taps, ["Asha"])

    def test_same_card_again_after_removal(self):
        self.poll("DEADBEEF")
        self.poll(None, advance=2)
        self.poll("DEADBEEF")
        self.assertEqual(self.taps, ["Asha", "Asha"])

    def test_enrolls_next_card(self):
        self.reader.start_enrolling("Ravi")
        self.poll("CAFEF00D")
        self.assertEqual(self.taps, [], "an enrolling tap is not an authorisation")
        self.assertEqual(CardStore(self.path).lookup("cafef00d"), "Ravi", "saved to disk")
        self.assertIsNone(self.reader.state()["enrolling"])

    def test_enrolling_times_out(self):
        self.reader.start_enrolling("Ravi")
        self.poll("CAFEF00D", advance=21)
        self.assertEqual(self.taps, [None])
        self.assertIsNone(self.cards.lookup("CAFEF00D"))

    def test_rejects_bad_names(self):
        with self.assertRaises(ValueError):
            self.reader.start_enrolling("../../etc")

    def test_remove_card(self):
        self.assertTrue(self.cards.remove("DEADBEEF"))
        self.assertIsNone(CardStore(self.path).lookup("DEADBEEF"))
        self.assertFalse(self.cards.remove("DEADBEEF"))

    def test_card_summary_for_dashboard(self):
        self.assertEqual(self.reader.state()["cards"], [{"uid": "DEADBEEF", "label": "...BEEF", "name": "Asha"}])


if __name__ == "__main__":
    unittest.main()
