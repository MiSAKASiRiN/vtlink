"""VT12 business framing and strict A5/type-2 uplink decoding.

Downlink type-2 includes the four-byte transport trailer verified on VT02.
Unsupported transport envelopes produce a decoding error.
"""
import struct


def crc(data, initial, polynomial):
    value = initial
    for byte in data:
        value ^= byte
        for _ in range(8):
            value = (value >> 1) ^ (polynomial if value & 1 else 0)
    return value


def business_frame(command, payload, sequence=0):
    header = struct.pack('<BHB', 0xA5, len(payload), sequence & 255)
    body = header + bytes([crc(header, 0xFF, 0x8C)]) + struct.pack('<H', command) + payload
    return body + struct.pack('<H', crc(body, 0xFFFF, 0x8408))



def downlink_transport(data, *, timestamp_ms=0, sequence=0):
    """VT12 -> VT02 type-2: length includes four trailer bytes.

    The peer removes this trailer before writing UART. Omitting it truncates
    the business frame by four bytes (39-byte custom frame becomes 35 bytes).
    This path is a direct transport, without FEC/retransmission.
    """
    if len(data) > 999:
        raise ValueError('业务帧过长')
    header = b'\xA5' + struct.pack('<H', len(data) + 4)
    body = header + bytes([crc(header, 0x77, 0x8C), 2]) + data
    body += struct.pack('<HH', timestamp_ms & 65535, sequence & 65535)
    return body + struct.pack('<H', crc(body, 0x1862, 0x8408))


def aa_type2_transport(data, *, timestamp_ms=0, sequence=0):
    """AA/type-2 transport format, matching the fixed wire fixture.

    The wire length includes the trailer; AA header CRC8 uses the length before
    that trailer. Each call generates one AA/type-2 segment.
    """
    if len(data) > 999:
        raise ValueError('业务帧过长')
    crc_header = b'\xAA' + struct.pack('<H', len(data))
    header = b'\xAA' + struct.pack('<H', len(data) + 4)
    body = header + bytes([crc(crc_header, 0x77, 0x8C), 2]) + data
    body += struct.pack('<HH', timestamp_ms & 65535, sequence & 65535)
    return body + struct.pack('<H', crc(body, 0x1862, 0x8408))


def keyboard_payload(*, x=0, y=0, z=0, left=False, right=False, keys=0):
    for value in (x, y, z):
        if type(value) is not int or not -32768 <= value <= 32767:
            raise ValueError('x/y/z 必须为 int16 整数')
    if type(keys) is not int or not 0 <= keys <= 65535:
        raise ValueError('keys 必须为 0–65535 的整数位图')
    if type(left) is not bool or type(right) is not bool:
        raise ValueError('left/right 必须为布尔值')
    return struct.pack('<hhhBBHH', x, y, z, left, right, keys, 0)



class UplinkDecoder:
    def __init__(self):
        self.buffer = bytearray()
        self.invalid_bytes = 0

    def reset(self):
        self.buffer.clear()

    def feed_a5(self, data):
        self.buffer.extend(data)
        messages = []
        while len(self.buffer) >= 5:
            if self.buffer[0] != 0xA5 or crc(self.buffer[:4], 0xFF, 0x8C) != self.buffer[4]:
                del self.buffer[0]
                self.invalid_bytes += 1
                continue
            length = int.from_bytes(self.buffer[1:3], 'little')
            if length > 1000:
                del self.buffer[0]
                self.invalid_bytes += 1
                continue
            size = length + 9
            if len(self.buffer) < size:
                break
            packet = bytes(self.buffer[:size])
            if crc(packet[:-2], 0xFFFF, 0x8408) != int.from_bytes(packet[-2:], 'little'):
                del self.buffer[0]
                self.invalid_bytes += 1
                continue
            del self.buffer[:size]
            messages.append(dict(command_id=int.from_bytes(packet[5:7], 'little'),
                                 sequence=packet[3], payload_hex=packet[7:-2].hex(),
                                 payload_bytes=length))
        return messages

    def feed(self, data):
        if len(data) < 5:
            raise ValueError('业务传输帧过短')
        # Raw A5 reports are accepted only with a valid business header CRC.
        if data[0] == 0xA5 and crc(data[:4], 0xFF, 0x8C) == data[4]:
            return self.feed_a5(data), 'raw-a5'
        messages, cursor = [], 0
        while cursor < len(data):
            header = data[cursor:cursor + 5]
            if len(header) < 5 or header[0] not in (0xA5, 0xAA):
                raise ValueError('不支持的传输封装')
            size_version = int.from_bytes(header[1:3], 'little')
            length = size_version & 0x3FFF
            if size_version >> 14 or length > 1003 or header[4] != 2:
                raise ValueError('暂不支持该冗余/FEC 传输类型')
            size = length + 7
            segment = data[cursor:cursor + size]
            if len(segment) != size or crc(segment[:-2], 0x1862, 0x8408) != int.from_bytes(segment[-2:], 'little'):
                raise ValueError('传输帧长度或 CRC16 错误')
            if header[0] == 0xAA:
                if length < 4:
                    raise ValueError('AA/type-2 传输帧过短')
                crc_header = b'\xAA' + struct.pack('<H', length - 4)
                content = segment[5:-6]  # timestamp + transport sequence
            else:
                crc_header, content = header[:3], segment[5:-2]
            if crc(crc_header, 0x77, 0x8C) != header[3]:
                raise ValueError('传输帧 CRC8 错误')
            # Validate every segment before exposing any data from this envelope.
            messages.append(content)
            cursor += size
        result = []
        for content in messages:
            result.extend(self.feed_a5(content))
        return result, 'type2'
