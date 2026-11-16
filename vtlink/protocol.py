"""Pure byte encoders; no device I/O, retransmission, or robot semantics.

VT12: sender 8A, custom 0302. VT13: sender AA, current custom 0311.
The AA/type-2 header CRC8 is computed BEFORE adding the four-byte trailer.
"""
import struct
from .vt12_commands import duml, tx_set_channel, tx_query_channel
from .wire import DumlParser, A5Parser, InputState, keyframe, keyboard_packets
from .business import business_frame, downlink_transport, aa_type2_transport, keyboard_payload, UplinkDecoder

CHANNELS = {1: 0x09F2, 2: 0x0A08, 3: 0x0A1E, 4: 0x0A34, 5: 0x0A4A, 6: 0x0A60}


def vt12_query(sequence=0):
    return duml(0x61, sequence=sequence)


def vt12_channel(x, y=None, sequence=0):
    """Generate receiver 09/0D. No automatic application on connection."""
    y = x if y is None else y
    if type(x) is not int or type(y) is not int or x not in CHANNELS or y not in CHANNELS:
        raise ValueError("VT12 channels must be integers in 1..6")
    return duml(0x0D, struct.pack('<BHBH', 15, CHANNELS[x], 17, CHANNELS[y]), sequence=sequence)


def _custom(payload, command, sender, a5_sequence, transport_sequence, timestamp_ms, aa_type2, sequence):
    if not isinstance(payload, (bytes, bytearray)) or len(payload) != 30:
        raise ValueError("Custom payload must be exactly 30 bytes; no padding/truncation")
    inner = business_frame(command, bytes(payload), a5_sequence)
    encoder = aa_type2_transport if aa_type2 else downlink_transport
    segment = encoder(inner, timestamp_ms=timestamp_ms, sequence=transport_sequence)
    return duml(0x63, segment, sender=sender, receiver=0x77, sequence=sequence)


def vt12_custom(payload, *, a5_sequence=0, transport_sequence=0, timestamp_ms=0, aa_type2=True, sequence=0):
    """0302/30B in AA/type-2 (default) or A5/type-2 format, with trailer."""
    return _custom(payload, 0x0302, 0x8A, a5_sequence, transport_sequence, timestamp_ms, aa_type2, sequence)


def vt13_custom(payload, *, a5_sequence=0, transport_sequence=0, timestamp_ms=0):
    """0311/30B in AA/type-2 format, with trailer; payload is opaque data."""
    return _custom(payload, 0x0311, 0xAA, a5_sequence, transport_sequence, timestamp_ms, True, 0)


def vt12_keyboard(*, x=0, y=0, z=0, left=False, right=False, keys=0,
                  a5_sequence=0, transport_sequence=0, timestamp_ms=0, sequence=0):
    payload = keyboard_payload(x=x, y=y, z=z, left=left, right=right, keys=keys)
    segment = downlink_transport(business_frame(0x0304, payload, a5_sequence),
                                 timestamp_ms=timestamp_ms, sequence=transport_sequence)
    return duml(0x63, segment, receiver=0x77, sequence=sequence)


vt13_keyboard = keyboard_packets
vt13_keyframe = keyframe
vt02_set_channel = tx_set_channel
vt02_query_channel = tx_query_channel
