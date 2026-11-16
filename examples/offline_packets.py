"""Run from any working directory. Prints bytes only; no device access."""
import pathlib
import sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from vtlink import vt12_custom, vt13_custom, DumlParser, UplinkDecoder

payload = bytes(range(30))  # Offline fixture; application semantics are defined by the integrator.
for name, encode in [('VT02/12', vt12_custom), ('VT03/13', vt13_custom)]:
    packet = encode(payload, a5_sequence=17, transport_sequence=0x1234, timestamp_ms=0x9076)
    outer = DumlParser().feed(packet)[0]
    messages, kind = UplinkDecoder().feed(outer['payload'])
    print(name, packet.hex(' '))
    print('decoded:', kind, messages)
