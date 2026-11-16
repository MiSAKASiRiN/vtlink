"""Monitor an explicitly selected protocol CDC; optional VT12 status query.

No robot business writes. --keyframe sends VT13 request/cancel once; start
USB capture first. One process must own the protocol serial port.
"""
import argparse
import json
import pathlib
import sys
import time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from vtlink import DumlParser, vt12_query, vt13_keyframe


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--port', required=True)
    ap.add_argument('--family', choices=('vt12', 'vt13'), required=True)
    ap.add_argument('--seconds', type=float, default=10)
    ap.add_argument('--query', action='store_true')
    ap.add_argument('--keyframe', action='store_true')
    args = ap.parse_args()
    if args.seconds <= 0 or (args.query and args.family != 'vt12') or (args.keyframe and args.family != 'vt13'):
        ap.error('seconds must be positive; query is VT12-only; keyframe is VT13-only')
    import serial
    port = serial.Serial(port=None, baudrate=961200, timeout=0.05, write_timeout=0.5,
                         xonxoff=False, rtscts=False, dsrdtr=False)
    port.dtr = port.rts = False
    port.port = args.port
    parser = DumlParser()
    cancel_at = None
    with port:
        def write(packet):
            if port.write(packet) != len(packet):
                raise IOError('Short serial write')
            port.flush()
        try:
            if args.query:
                write(vt12_query())
            if args.keyframe:
                cancel_at = time.monotonic() + .15
                write(vt13_keyframe(True))
            deadline = time.monotonic() + args.seconds
            while time.monotonic() < deadline:
                if cancel_at is not None and time.monotonic() >= cancel_at:
                    write(vt13_keyframe(False))
                    cancel_at = None
                for frame in parser.feed(port.read(4096)):
                    print(json.dumps({k:v.hex() if isinstance(v, bytes) else v for k,v in frame.items()}), flush=True)
        finally:
            if cancel_at is not None:
                write(vt13_keyframe(False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
