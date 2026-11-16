"""VT12 DUML commands and standalone VT02 UART channel commands. No device I/O."""
import struct
from .wire import crc, a5_frame as a5


def duml(command_id, payload=b'', receiver=0x0E, sequence=0,
         command_set=9, sender=0x8A, flags=0x40):
    length = 13 + len(payload)
    if not 13 <= length <= 1023:
        raise ValueError('DUML length must be 13..1023')
    header = b'\x55' + struct.pack('<H', length | 0x400)
    body = header + bytes([crc(header, 0x77, 0x8C)])
    body += struct.pack('<BBHBBB', sender, receiver, sequence,
                        flags, command_set, command_id) + payload
    return body + struct.pack('<H', crc(body, 0x3692, 0x8408))


def tx_set_channel(channel, sequence=0):
    """Generate the channel-setting A5 frame for standalone VT02 UART."""
    if channel not in range(1, 7):
        raise ValueError('Standalone transmitter supports channels 1..6')
    return a5(0x0F01, bytes([channel]), sequence)


def tx_query_channel(sequence=10):
    return a5(0x0F02, b'', sequence)
