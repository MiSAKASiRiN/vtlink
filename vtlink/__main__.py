"""Offline packet generation/inspection and serial enumeration."""
import argparse
import json
from . import __version__
from .protocol import *


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--version', action='version', version=__version__)
    sub = ap.add_subparsers(dest='action', required=True)
    sub.add_parser('ports', help='List serial ports; never open them')
    p = sub.add_parser('frames', help='Print representative packets; no transmission')
    p.add_argument('--family', choices=('vt12', 'vt13'), required=True)
    p.add_argument('--channel', type=int, choices=range(1, 7), default=3)
    p = sub.add_parser('inspect', help='Validate and decode hex DUML stream')
    p.add_argument('hex')
    args = ap.parse_args()
    if args.action == 'ports':
        from serial.tools import list_ports
        result = [dict(port=p.device, vid=p.vid, pid=p.pid, interface=p.interface,
                       location=p.location, description=p.description) for p in list_ports.comports()]
    elif args.action == 'frames':
        if args.family == 'vt12':
            packets = dict(query=vt12_query(), channel=vt12_channel(args.channel),
                           custom_example=vt12_custom(bytes(range(30)), timestamp_ms=1000, transport_sequence=1),
                           keyboard_neutral=vt12_keyboard(),
                           transmitter_uart_set=vt02_set_channel(args.channel),
                           transmitter_uart_query=vt02_query_channel())
        else:
            packets = dict(keyframe_request=vt13_keyframe(True), keyframe_cancel=vt13_keyframe(False),
                           custom_example=vt13_custom(bytes(range(30)), timestamp_ms=1000, transport_sequence=1))
            packets.update({f'keyboard_neutral_{i}': p for i,p in enumerate(vt13_keyboard(InputState()))})
        result = {'transmitted': False, 'family': args.family,
                  'note': 'custom_example contains offline test bytes; application payload semantics are defined by the integrator',
                  'packets': {k:v.hex(' ') for k,v in packets.items()}}
    else:
        parser = DumlParser()
        frames = parser.feed(bytes.fromhex(args.hex))
        result = {'frames': [{k:v.hex(' ') if isinstance(v, bytes) else v for k,v in f.items()} for f in frames],
                  'invalid_bytes': parser.invalid, 'buffered_bytes': len(parser.buffer)}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if frames and not parser.invalid and not parser.buffer else 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
