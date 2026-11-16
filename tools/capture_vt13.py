#!/usr/bin/env python3
"""Read DJI 2ca3:1020 interface 0 / bulk IN 0x81 as raw HEVC.

Intended for Linux/macOS with PyUSB and a system libusb-1.0 installation.
Claims the video interface and saves incoming bytes to a file or stdout.
Keyframe control is provided separately by examples/serial_monitor.py.
"""
import argparse
import json
import pathlib
import sys
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=float, default=10,
                        help='Capture duration; 0 means until Ctrl-C (default: 10).')
    parser.add_argument('--output', default='capture.h265',
                        help='Output file; use - for binary stdout.')
    parser.add_argument('--serial-number', help='Select one unit when several are connected.')
    parser.add_argument('--list', action='store_true', help='Print descriptors only.')
    args = parser.parse_args()
    if args.seconds < 0:
        parser.error('--seconds must be nonnegative')
    try:
        import usb.core
        import usb.util
    except ImportError:
        parser.exit(2, 'Install PyUSB in a virtual environment: python -m pip install pyusb\n')
    try:
        devices = list(usb.core.find(find_all=True, idVendor=0x2ca3, idProduct=0x1020))
    except usb.core.NoBackendError:
        parser.exit(2, 'No libusb backend. Install system libusb-1.0 for your OS and Python architecture.\n')
    if args.serial_number:
        devices = [d for d in devices if (d.serial_number or '').rstrip('\0') == args.serial_number]
    if not devices:
        parser.exit(2, 'No matching DJI 2ca3:1020 device. Check USB enumeration and access permissions.\n')
    if args.list:
        for device in devices:
            print(device)
            usb.util.dispose_resources(device)
        return
    if len(devices) != 1:
        parser.exit(2, 'Multiple DJI devices found. Select one using --serial-number.\n')
    device = devices[0]
    claimed = False
    detached = False
    output = None
    try:
        # Avoid reapplying configuration 1 when already active: that could
        # disturb the other interfaces of this composite device.
        active = device.get_active_configuration()
        if active.bConfigurationValue != 1:
            raise RuntimeError('Expected active USB configuration 1. Inspect with --list first.')
        interface = active[(0, 0)]
        endpoint = usb.util.find_descriptor(interface, bEndpointAddress=0x81)
        if endpoint is None or usb.util.endpoint_type(endpoint.bmAttributes) != usb.util.ENDPOINT_TYPE_BULK:
            raise RuntimeError('Interface 0 / bulk IN 0x81 missing. Inspect descriptors before proceeding.')
        if sys.platform.startswith('linux') and device.is_kernel_driver_active(0):
            raise RuntimeError('Video interface is held by a kernel driver; inspect binding first')
        usb.util.claim_interface(device, 0)
        claimed = True
        if args.output != '-':
            pathlib.Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        output = sys.stdout.buffer if args.output == '-' else pathlib.Path(args.output).open('wb')
        print('Reading interface 0 / 0x81. No serial or bulk OUT writes.', file=sys.stderr)
        started = time.monotonic()
        deadline = started + args.seconds if args.seconds else float('inf')
        total = transfers = timeouts = 0
        interrupted = False
        try:
            while time.monotonic() < deadline:
                try:
                    chunk = device.read(0x81, 10240, timeout=1000)
                except usb.core.USBTimeoutError:
                    timeouts += 1
                    continue
                if chunk:
                    output.write(chunk.tobytes())
                    total += len(chunk)
                    transfers += 1
        except KeyboardInterrupt:
            interrupted = True
        elapsed = time.monotonic() - started
        output.flush()
        print(json.dumps({'bytes': total, 'transfers': transfers, 'timeouts': timeouts,
                          'elapsed_seconds': round(elapsed, 3), 'interrupted': interrupted,
                          'format': 'raw HEVC Annex B; concatenate USB reads unchanged'}), file=sys.stderr)
        if not total:
            print('No video bytes. Check pairing/transmitter power. Cold-start keyframe control is not implemented here.',
                  file=sys.stderr)
            return 3
    finally:
        if output is not None and args.output != '-':
            output.close()
        try:
            if claimed:
                usb.util.release_interface(device, 0)
        finally:
            try:
                if detached:
                    device.attach_kernel_driver(0)
            finally:
                usb.util.dispose_resources(device)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except BrokenPipeError:
        sys.exit(0)
    except Exception as error:
        print(f'{type(error).__name__}: {error}', file=sys.stderr)
        sys.exit(1)
