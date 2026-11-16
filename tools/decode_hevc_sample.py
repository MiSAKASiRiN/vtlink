#!/usr/bin/env python3
"""Decode a local HEVC fixture using the shared synchronization implementation."""
import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from vtlink.hevc import SyncDecoder


def main():
    kit = pathlib.Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', nargs='?', default=str(kit / 'samples/vt13-video.h265'))
    parser.add_argument('--snapshot', help='Save the latest returned video frame as PNG')
    args = parser.parse_args()
    decoder = SyncDecoder()
    latest = None
    with pathlib.Path(args.input).open('rb') as stream:
        while True:
            block = stream.read(4093)
            if not block:
                break
            frames, _reset = decoder.feed(block)
            if frames:
                latest = frames[-1]
    # The incremental live decoder intentionally retains an unfinished tail;
    # this test checks synchronization and image recovery, not EOF flushing.
    if args.snapshot and latest:
        latest.to_image().save(args.snapshot)
    result = {'frames_returned': decoder.frames, 'resolution': decoder.last_resolution,
              'synced': decoder.synced, 'decode_faults': decoder.faults, 'warnings': decoder.warning_count,
              'note': 'Live framing keeps the final unfinished NAL/packet until further input.'}
    print(json.dumps(result, indent=2))
    return 0 if decoder.synced and decoder.frames and not decoder.faults else 1


if __name__ == '__main__':
    sys.exit(main())
