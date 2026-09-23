import json
import os
import re
import threading
import time

# MFRC522 registers and commands (NXP MFRC522 datasheet, section 9).
COMMAND_REG, COMM_IEN_REG, COMM_IRQ_REG, ERROR_REG = 0x01, 0x02, 0x04, 0x06
FIFO_DATA_REG, FIFO_LEVEL_REG, CONTROL_REG, BIT_FRAMING_REG = 0x09, 0x0A, 0x0C, 0x0D
MODE_REG, TX_CONTROL_REG, TX_ASK_REG = 0x11, 0x14, 0x15
T_MODE_REG, T_PRESCALER_REG, T_RELOAD_REG_H, T_RELOAD_REG_L = 0x2A, 0x2B, 0x2C, 0x2D
VERSION_REG = 0x37
CMD_IDLE, CMD_TRANSCEIVE, CMD_SOFT_RESET = 0x00, 0x0C, 0x0F
PICC_REQA, PICC_ANTICOLLISION_CL1 = 0x26, 0x93


class Mfrc522:
    """Reads card UIDs from an RC522 over SPI (spidev); no GPIO access needed.

    Wire the RC522's RST pin to 3.3 V. Reads the 4-byte UID of the MIFARE
    Classic cards and key fobs that come with RC522 kits.
    """

    def __init__(self, spi):
        self.spi = spi
        self.write(COMMAND_REG, CMD_SOFT_RESET)
        time.sleep(0.05)
        self.write(T_MODE_REG, 0x8D)  # timer: ~25 ms timeout for card replies
        self.write(T_PRESCALER_REG, 0x3E)
        self.write(T_RELOAD_REG_L, 30)
        self.write(T_RELOAD_REG_H, 0)
        self.write(TX_ASK_REG, 0x40)  # 100% ASK modulation
        self.write(MODE_REG, 0x3D)  # CRC preset 0x6363
        self.write(TX_CONTROL_REG, self.read(TX_CONTROL_REG) | 0x03)  # antenna on

    @classmethod
    def open(cls, bus=0, device=0):
        import spidev  # only on the Pi

        spi = spidev.SpiDev()
        spi.open(bus, device)
        spi.max_speed_hz = 1_000_000
        spi.mode = 0
        return cls(spi)

    def write(self, register, value):
        self.spi.xfer2([(register << 1) & 0x7E, value])

    def read(self, register):
        return self.spi.xfer2([((register << 1) & 0x7E) | 0x80, 0])[1]

    def version(self):
        return self.read(VERSION_REG)

    def transceive(self, data, last_bits=0):
        """Sends bytes to a card and returns the reply bytes, or None when no card answered."""
        self.write(COMM_IEN_REG, 0x77 | 0x80)
        self.write(COMM_IRQ_REG, 0x7F)  # clear interrupt flags
        self.write(FIFO_LEVEL_REG, 0x80)  # flush FIFO
        self.write(COMMAND_REG, CMD_IDLE)
        for byte in data:
            self.write(FIFO_DATA_REG, byte)
        self.write(COMMAND_REG, CMD_TRANSCEIVE)
        self.write(BIT_FRAMING_REG, 0x80 | last_bits)  # StartSend
        for _ in range(200):
            irq = self.read(COMM_IRQ_REG)
            if irq & 0x30:  # RxIRq or IdleIRq: reply received
                break
            if irq & 0x01:  # TimerIRq: no card
                return None
        else:
            return None
        self.write(BIT_FRAMING_REG, 0x00)
        if self.read(ERROR_REG) & 0x1B:  # buffer overflow, collision, parity or protocol error
            return None
        count = self.read(FIFO_LEVEL_REG)
        return [self.read(FIFO_DATA_REG) for _ in range(count)]

    def read_uid(self):
        """Returns the UID of a card in the field as a hex string, or None."""
        answer = self.transceive([PICC_REQA], last_bits=7)
        if not answer or len(answer) != 2:
            return None
        return parse_anticollision(self.transceive([PICC_ANTICOLLISION_CL1, 0x20]))


def parse_anticollision(reply):
    """Checks a 4-byte UID + BCC anticollision reply; returns the UID as hex or None."""
    if not reply or len(reply) != 5:
        return None
    check = 0
    for byte in reply[:4]:
        check ^= byte
    if check != reply[4]:
        return None
    return "".join(f"{byte:02X}" for byte in reply[:4])


NAME_PATTERN = re.compile(r"^[A-Za-z0-9 _-]{1,32}$")


class CardStore:
    """Authorised cards, saved as {"UID": "name"} in a JSON file on the Pi."""

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        try:
            with open(path, encoding="utf-8") as file:
                self._cards = {str(uid).upper(): str(name) for uid, name in json.load(file).items()}
        except (OSError, ValueError):
            self._cards = {}

    def _save(self):
        folder = os.path.dirname(self.path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as file:
            json.dump(self._cards, file, indent=1)

    def lookup(self, uid):
        with self._lock:
            return self._cards.get(uid.upper())

    def add(self, uid, name):
        with self._lock:
            self._cards[uid.upper()] = name
            self._save()

    def remove(self, uid):
        with self._lock:
            found = self._cards.pop(uid.upper(), None) is not None
            if found:
                self._save()
            return found

    def summary(self):
        with self._lock:
            # Only the end of the UID is shown on the dashboard.
            return [{"uid": uid, "label": f"...{uid[-4:]}", "name": name} for uid, name in sorted(self._cards.items(), key=lambda c: c[1].lower())]


class RfidReader(threading.Thread):
    """Polls the RC522 and reports each new card tap.

    A card held on the reader counts as one tap; it must be removed for
    `repeat_after` seconds before it counts again. While enrolling, the next
    tapped card is saved under the given name instead of being checked.
    """

    def __init__(self, cards, on_tap, open_reader=Mfrc522.open, poll_interval=0.1, repeat_after=1.5,
                 enroll_timeout=20.0, clock=time.monotonic):
        super().__init__(daemon=True, name="rfid")
        self.cards = cards
        self.on_tap = on_tap
        self.open_reader = open_reader
        self.poll_interval = poll_interval
        self.repeat_after = repeat_after
        self.enroll_timeout = enroll_timeout
        self.clock = clock
        self.status = "starting"
        self.last_tap = None  # {"uid", "name", "authorised", "time"}
        self._reader = None
        self._last_uid = None
        self._last_seen = 0.0
        self._enrolling = None  # (name, deadline)
        self._lock = threading.Lock()

    def start_enrolling(self, name):
        if not NAME_PATTERN.match(name):
            raise ValueError("name must be 1-32 letters, digits, spaces, '-' or '_'")
        with self._lock:
            self._enrolling = (name, self.clock() + self.enroll_timeout)

    def cancel_enrolling(self):
        with self._lock:
            self._enrolling = None

    def state(self):
        now = self.clock()
        with self._lock:
            enrolling = self._enrolling if self._enrolling and self._enrolling[1] > now else None
            last = dict(self.last_tap, age=round(now - self.last_tap["time"], 1)) if self.last_tap else None
        if last:
            del last["time"]
        return {
            "reader": self.status,
            "enrolling": {"name": enrolling[0], "seconds_left": round(enrolling[1] - now)} if enrolling else None,
            "last_tap": last,
            "cards": self.cards.summary(),
        }

    def run(self):
        while True:
            if self._reader is None:
                try:
                    self._reader = self.open_reader()
                    version = self._reader.version()
                    if version in (0x00, 0xFF):
                        raise OSError(f"no RC522 answering on SPI (version register 0x{version:02X})")
                    self.status = "ok"
                    print(f"RFID: RC522 ready (version 0x{version:02X})")
                except (OSError, ImportError) as error:
                    self._reader = None
                    if self.status != f"not found: {error}":
                        print(f"RFID: {error}; retrying every 10 s")
                    self.status = f"not found: {error}"
                    time.sleep(10)
                    continue
            try:
                uid = self._reader.read_uid()
            except OSError as error:
                print(f"RFID: read failed: {error}")
                self._reader = None
                continue
            self.handle_uid(uid)
            time.sleep(self.poll_interval)

    def handle_uid(self, uid):
        """Processes one poll result (a UID or None)."""
        now = self.clock()
        if uid is None:
            if self._last_uid and now - self._last_seen > self.repeat_after:
                self._last_uid = None
            return
        new_tap = uid != self._last_uid
        self._last_uid, self._last_seen = uid, now
        if not new_tap:
            return
        with self._lock:
            enrolling = self._enrolling if self._enrolling and self._enrolling[1] > now else None
            self._enrolling = None
        if enrolling:
            self.cards.add(uid, enrolling[0])
            print(f"RFID: card ...{uid[-4:]} added for {enrolling[0]}")
            with self._lock:
                self.last_tap = {"uid": f"...{uid[-4:]}", "name": enrolling[0], "authorised": True,
                                 "enrolled": True, "time": now}
            return
        name = self.cards.lookup(uid)
        print(f"RFID: card ...{uid[-4:]} {'authorised: ' + name if name else 'not authorised'}")
        with self._lock:
            self.last_tap = {"uid": f"...{uid[-4:]}", "name": name, "authorised": name is not None,
                             "enrolled": False, "time": now}
        self.on_tap(name)
