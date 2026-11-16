"""DJI USB device communication formats: DUML, A5 and input reports.

Pure Python; no device access and no DJI runtime dependency.
"""
from dataclasses import dataclass
import struct


def crc(data, initial, polynomial):
    value = initial
    for byte in data:
        value ^= byte
        for _ in range(8):
            value = (value >> 1) ^ (polynomial if value & 1 else 0)
    return value


def duml(command_set, command_id, payload=b'', receiver=0x0e, sequence=0):
    length = 13 + len(payload)
    if not 13 <= length <= 1023:
        raise ValueError('DUML packet length exceeds its 10-bit length field')
    sof = b'\x55' + struct.pack('<H', length | 0x400)
    body = sof + bytes([crc(sof, 0x77, 0x8c)])
    body += struct.pack('<BBHBBB', 0xaa, receiver, sequence, 0x40, command_set, command_id) + payload
    return body + struct.pack('<H', crc(body, 0x3692, 0x8408))


def keyframe(reset):
    value = 0x24 if reset else 0x04
    return duml(1, 1, struct.pack('<IBBBHB', 0, 0, value, 0, 0, value))


KEY_BITS = {name: 1 << index for index, name in enumerate(
    ('W', 'S', 'A', 'D', 'SHIFT', 'CTRL', 'Q', 'E', 'R', 'F', 'G', 'Z', 'X', 'C', 'V', 'B'))}


@dataclass(frozen=True)
class InputState:
    mouse_x: int = 0
    mouse_y: int = 0
    mouse_z: int = 0
    left: int = 0
    right: int = 0
    middle: int = 0
    keys: int = 0

    def payload(self):
        if any(not -32768 <= value <= 32767 for value in (self.mouse_x, self.mouse_y, self.mouse_z)):
            raise ValueError('Mouse coordinates must fit signed 16 bits')
        if any(value not in (0, 1) for value in (self.left, self.right, self.middle)):
            raise ValueError('Mouse button states must be 0 or 1')
        if not 0 <= self.keys <= 0xffff:
            raise ValueError('Keyboard mask must fit unsigned 16 bits')
        buttons = self.left | (self.right << 2) | (self.middle << 4)
        # Input_Device_Status_RC151: sdr + xyz + packed buttons + low keys
        # + ssfn + high keys + reserved, Pack=1; 13 bytes total.
        return struct.pack('<BhhhBBHBB', 0, self.mouse_x, self.mouse_y, self.mouse_z,
                           buttons, self.keys & 0xff, 0, self.keys >> 8, 0)


def keyboard_packets(state):
    payload = state.payload()
    # Send one input report to each target: type 6/id 0 and type 14/id 0.
    return (duml(0x3f, 0x11, payload, receiver=0x06),
            duml(0x3f, 0x11, payload, receiver=0x0e))


def a5_frame(command_id, payload, sequence=0):
    if len(payload) > 1024:
        raise ValueError('A5 payload too long')
    header = b'\xa5' + struct.pack('<HB', len(payload), sequence & 0xff)
    body = header + bytes([crc(header, 0xff, 0x8c)]) + struct.pack('<H', command_id) + payload
    return body + struct.pack('<H', crc(body, 0xffff, 0x8408))



class A5Parser:
    def __init__(self):
        self.buffer = bytearray()
        self.invalid = 0

    def feed(self, data):
        self.buffer.extend(data)
        result = []
        while len(self.buffer) >= 5:
            if self.buffer[0] != 0xa5 or crc(self.buffer[:4], 0xff, 0x8c) != self.buffer[4]:
                del self.buffer[0]
                self.invalid += 1
                continue
            length = struct.unpack_from('<H', self.buffer, 1)[0]
            if length > 1024:
                del self.buffer[0]
                self.invalid += 1
                continue
            total = length + 9
            if len(self.buffer) < total:
                break
            frame = bytes(self.buffer[:total])
            if crc(frame[:-2], 0xffff, 0x8408) != struct.unpack_from('<H', frame, total - 2)[0]:
                del self.buffer[0]
                self.invalid += 1
                continue
            del self.buffer[:total]
            result.append((struct.unpack_from('<H', frame, 5)[0], frame[7:-2], frame[3], frame))
        return result


class DumlParser:
    def __init__(self):
        self.buffer = bytearray()
        self.invalid = 0

    def feed(self, data):
        self.buffer.extend(data)
        result = []
        while len(self.buffer) >= 4:
            if self.buffer[0] != 0x55 or crc(self.buffer[:3], 0x77, 0x8c) != self.buffer[3]:
                del self.buffer[0]
                self.invalid += 1
                continue
            length = struct.unpack_from('<H', self.buffer, 1)[0] & 0x3ff
            if not 13 <= length <= 1023:
                del self.buffer[0]
                self.invalid += 1
                continue
            if len(self.buffer) < length:
                break
            frame = bytes(self.buffer[:length])
            if crc(frame[:-2], 0x3692, 0x8408) != struct.unpack_from('<H', frame, length - 2)[0]:
                del self.buffer[0]
                self.invalid += 1
                continue
            del self.buffer[:length]
            result.append({'set': frame[9], 'id': frame[10], 'sender': frame[4], 'receiver': frame[5],
                           'sequence': struct.unpack_from('<H', frame, 6)[0], 'flags': frame[8],
                           'payload': frame[11:-2], 'raw': frame})
        return result
