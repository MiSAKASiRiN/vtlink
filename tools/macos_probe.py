#!/usr/bin/env python3
"""Bounded C5 USB + Protocol probe. Sends only keyframe request/cancel.

Run with python tools/macos_probe.py --seconds 30.
Never resets/reconfigures the device or detaches a kernel driver.
"""
import argparse
import collections
import datetime
import json
import multiprocessing
import pathlib
import plistlib
import queue
import struct
import subprocess
import sys
import threading
import time
from zoneinfo import ZoneInfo

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from vtlink.wire import DumlParser, keyframe

REQUEST = bytes.fromhex('55 17 04 38 aa 0e 00 00 40 01 01 00 00 00 00 00 24 00 00 00 24 8f 06')
CANCEL = bytes.fromhex('55 17 04 38 aa 0e 00 00 40 01 01 00 00 00 00 00 04 00 00 00 04 1c 47')


def write_keyframe(port, packet):
    """Allow exactly two vetted packets, including on shutdown."""
    if packet not in (REQUEST, CANCEL):
        raise ValueError('Only the fixed keyframe request/cancel packets are allowed')
    count = port.write(packet)
    if count != len(packet):
        raise IOError(f'Short serial write: {count}/{len(packet)}')
    return count


def registry():
    raw = subprocess.check_output(['ioreg', '-a', '-l', '-r', '-n', 'C5', '-p', 'IOService'])
    devices = [n for n in plistlib.loads(raw)
               if n.get('idVendor') == 0x2ca3 and n.get('idProduct') == 0x1020]
    if len(devices) != 1:
        raise RuntimeError(f'Expected exactly one C5 in IORegistry, got {len(devices)}')
    ports = {}

    def walk(node, interface=None):
        interface = node.get('bInterfaceNumber', interface)
        if node.get('IOCalloutDevice'):
            ports[interface] = node['IOCalloutDevice']
        for child in node.get('IORegistryEntryChildren', []):
            walk(child, interface)

    walk(devices[0])
    return raw, ports, devices[0]['locationID']


def decode_worker(chunks, messages, ready, output, started):
    """Isolate codec initialization/GIL stalls from USB draining and serial timing."""
    from vtlink.hevc import SyncDecoder
    decoder = SyncDecoder()
    latest = None
    first = True
    last_report = 0.0

    def report():
        messages.put({'kind': 'state', 'value': dict(
            decoded_frames=decoder.frames, decode_faults=decoder.faults,
            decode_warnings=decoder.warning_count, synced=decoder.synced,
            resolution=decoder.last_resolution, last_decode_fault=decoder.last_fault,
            nal_types=dict(decoder.nals))})

    try:
        ready.set()
        while True:
            data = chunks.get()
            if data is None:
                break
            faults_before = decoder.faults
            frames, _ = decoder.feed(data)
            if faults_before != decoder.faults:
                messages.put({'kind': 'event', 'event': 'decode_fault',
                              'seconds': time.monotonic() - started,
                              'message': decoder.last_fault})
            if frames:
                latest = frames[-1]
                if first:
                    stamp = time.monotonic() - started
                    frames[0].to_image().save(output / 'first-frame.png')
                    messages.put({'kind': 'event', 'event': 'first_decoded_frame',
                                  'seconds': stamp, 'width': frames[0].width, 'height': frames[0].height})
                    first = False
            now = time.monotonic()
            if now - last_report >= 0.1:
                report()
                last_report = now
        if latest is not None:
            latest.to_image().save(output / 'last-frame.png')
        report()
    except Exception as error:
        messages.put({'kind': 'failure', 'message': f'{type(error).__name__}: {error}'})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=float, default=30)
    parser.add_argument('--output', type=pathlib.Path)
    parser.add_argument('--no-keyframe', action='store_true', help='Receive only; zero serial payload writes')
    args = parser.parse_args()
    if not 3 <= args.seconds <= 120:
        parser.error('--seconds must be between 3 and 120')
    import av
    import serial
    import usb.core
    import usb.util

    if keyframe(True) != REQUEST or keyframe(False) != CANCEL:
        raise RuntimeError('Core protocol differs from the approved keyframe vectors')
    output = args.output or ROOT / 'work' / ('macos-probe-' + datetime.datetime.now(
        ZoneInfo('Asia/Shanghai')).strftime('%Y%m%d-%H%M%S'))
    output.mkdir(parents=True, exist_ok=False)
    events = (output / 'events.jsonl').open('w')
    started = time.monotonic()
    event_lock = threading.Lock()
    state_lock = threading.Lock()
    stop = threading.Event()
    mp = multiprocessing.get_context('spawn')
    chunks = mp.Queue(maxsize=512)
    messages = mp.Queue()
    decoder_ready = mp.Event()
    failures = []
    state = dict(usb_bytes=0, usb_transfers=0, usb_timeouts=0, usb_errors=0,
                 serial_bytes=0, serial_frames=0, serial_invalid_bytes=0,
                 decoded_frames=0, decode_faults=0, decode_warnings=0, synced=False,
                 queue_overflows=0, tx_packets=[], serial_commands={}, monitor=None)
    decode_process = None
    device = port = None
    claimed = pending_cancel = False
    workers = []

    def event(kind, **data):
        item = {'seconds': round(time.monotonic() - started, 6), 'event': kind, **data}
        with event_lock:
            events.write(json.dumps(item) + '\n')
            events.flush()
        print(json.dumps(item), flush=True)

    def fail(stage, error):
        message = f'{stage}: {type(error).__name__}: {error}'
        with state_lock:
            failures.append(message)
        event('failure', message=message)
        stop.set()

    def transmit(packet):
        event('serial_write_attempt', hex=packet.hex(' '))
        count = write_keyframe(port, packet)
        item = {'seconds': time.monotonic() - started, 'hex': packet.hex(' '), 'bytes': count}
        with state_lock:
            state['tx_packets'].append(item)
        event('serial_write_complete', **item)

    def read_usb():
        try:
            with (output / 'capture.h265').open('wb') as raw:
                while not stop.is_set():
                    try:
                        data = bytes(device.read(0x81, 65536, timeout=200))
                    except usb.core.USBTimeoutError:
                        with state_lock:
                            state['usb_timeouts'] += 1
                        continue
                    if not data:
                        continue
                    raw.write(data)
                    with state_lock:
                        first = state['usb_bytes'] == 0
                        state['usb_bytes'] += len(data)
                        state['usb_transfers'] += 1
                    if first:
                        event('first_usb_bytes', size=len(data))
                    try:
                        chunks.put_nowait(data)
                    except queue.Full:
                        with state_lock:
                            state['queue_overflows'] += 1
                        raise RuntimeError('Decode queue overflow; aborting without silently dropping bytes')
        except Exception as error:
            with state_lock:
                state['usb_errors'] += 1
            fail('usb', error)

    def read_serial():
        framing = DumlParser()
        counts = collections.Counter()
        try:
            with (output / 'protocol-rx.bin').open('wb') as raw:
                while not stop.is_set():
                    data = port.read(max(1, min(port.in_waiting, 8192)))
                    if not data:
                        continue
                    raw.write(data)
                    frames = framing.feed(data)
                    monitor = None
                    for frame in frames:
                        counts[f'{frame["set"]:02x}/{frame["id"]:02x}'] += 1
                        if (frame['set'], frame['id']) == (0x3f, 0xf6) and len(frame['payload']) in (10, 12):
                            values = struct.unpack_from('<5H', frame['payload'], len(frame['payload']) - 10)
                            monitor = dict(zip(('0302', '0309', '0310', 'rc', 'keyboard'), values))
                    with state_lock:
                        first = state['serial_frames'] == 0 and bool(frames)
                        state['serial_bytes'] += len(data)
                        state['serial_frames'] += len(frames)
                        state['serial_invalid_bytes'] = framing.invalid
                        state['serial_commands'] = dict(counts)
                        if monitor is not None:
                            state['monitor'] = monitor
                    if first:
                        event('first_valid_protocol_frames', count=len(frames))
        except Exception as error:
            fail('serial', error)

    def collect_decoder():
        while True:
            try:
                message = messages.get_nowait()
            except queue.Empty:
                return
            if message['kind'] == 'state':
                with state_lock:
                    state.update(message['value'])
            elif message['kind'] == 'event':
                message.pop('kind')
                event(message.pop('event'), **message)
            else:
                fail('decoder', RuntimeError(message['message']))

    try:
        decode_process = mp.Process(target=decode_worker,
                                    args=(chunks, messages, decoder_ready, output, started),
                                    name='HEVC-Decode', daemon=True)
        decode_process.start()
        if not decoder_ready.wait(timeout=10):
            raise RuntimeError('Decoder process did not become ready')
        before, ports, location = registry()
        (output / 'ioreg-before.plist').write_bytes(before)
        if 3 not in ports or 5 not in ports:
            raise RuntimeError(f'Expected CDC data interfaces 3 and 5, got {ports}')
        devices = list(usb.core.find(find_all=True, idVendor=0x2ca3, idProduct=0x1020))
        if len(devices) != 1:
            raise RuntimeError(f'Expected exactly one C5 via libusb, got {len(devices)}')
        device = devices[0]
        config = device.get_active_configuration()
        if config.bConfigurationValue != 1:
            raise RuntimeError('Configuration 1 is not active; refusing to change it')
        interface = config[(0, 0)]
        endpoint = usb.util.find_descriptor(interface, bEndpointAddress=0x81)
        if endpoint is None or usb.util.endpoint_type(endpoint.bmAttributes) != usb.util.ENDPOINT_TYPE_BULK:
            raise RuntimeError('Expected interface 0 bulk IN endpoint 0x81')
        state.update(protocol_port=ports[3], debug_port=ports[5], location_id=hex(location),
                     usb_bus=device.bus, usb_address=device.address,
                     baudrate=961200, baudrate_note='API requested value; wire behavior verified by valid DUML reception',
                     receive_only=args.no_keyframe, python=sys.version, pyav=av.__version__)
        (output / 'usb-descriptors.txt').write_text(str(device))
        usb.util.claim_interface(device, 0)
        claimed = True
        event('video_interface_claimed', interface=0, endpoint='0x81')
        port = serial.Serial(port=None, baudrate=961200, timeout=0.05, write_timeout=0.5,
                             xonxoff=False, rtscts=False, dsrdtr=False, exclusive=True)
        port.dtr = False
        port.rts = False
        port.port = ports[3]
        port.open()
        event('protocol_opened', port=port.port, baudrate=port.baudrate)
        during, current_ports, current_location = registry()
        if current_ports != ports or current_location != location:
            raise RuntimeError('CDC mapping changed after opening video and Protocol')
        (output / 'ioreg-concurrent.plist').write_bytes(during)
        for target, name in ((read_usb, 'USB-Read'), (read_serial, 'Protocol-Read')):
            worker = threading.Thread(target=target, name=name, daemon=True)
            workers.append(worker)
            worker.start()
        streaming = time.monotonic()
        deadline = streaming + args.seconds
        next_request = streaming + 1.0
        cancel_at = next_progress = 0.0
        attempts = 0
        while time.monotonic() < deadline and not stop.is_set():
            collect_decoder()
            if not decode_process.is_alive():
                raise RuntimeError('Decoder process exited during capture')
            now = time.monotonic()
            if pending_cancel and now >= cancel_at:
                transmit(CANCEL)
                pending_cancel = False
            with state_lock:
                synced = state['synced']
            if (not args.no_keyframe and not pending_cancel and attempts < 5 and
                    now >= next_request and (attempts == 0 or not synced)):
                # Set pending first so cleanup attempts cancellation even on a failed write.
                pending_cancel = True
                transmit(REQUEST)
                attempts += 1
                cancel_at = time.monotonic() + 0.15
                next_request = now + 2.0
            if now >= next_progress:
                with state_lock:
                    snapshot = {k: state[k] for k in ('usb_bytes', 'serial_frames', 'decoded_frames',
                                                      'decode_faults', 'synced', 'monitor')}
                event('progress', **snapshot)
                next_progress = now + 2.0
            stop.wait(0.01)
        state['streaming_seconds'] = time.monotonic() - streaming
    except KeyboardInterrupt:
        failures.append('Interrupted by user')
    except Exception as error:
        fail('main', error)
    finally:
        if pending_cancel and port is not None and port.is_open:
            try:
                transmit(CANCEL)
            except Exception as error:
                fail('cancel_on_close', error)
        stop.set()
        # Join producers first, then deliver EOF after all captured chunks.
        for worker in workers:
            worker.join(timeout=5)
            if worker.is_alive():
                failures.append(f'{worker.name} did not exit within 5 seconds')
        if decode_process is not None:
            if decode_process.is_alive():
                try:
                    chunks.put(None, timeout=1)
                except queue.Full:
                    failures.append('Decoder queue did not accept EOF')
                limit = time.monotonic() + 5
                while decode_process.is_alive() and time.monotonic() < limit:
                    collect_decoder()
                    decode_process.join(timeout=0.05)
                if decode_process.is_alive():
                    failures.append('Decoder did not exit within 5 seconds')
                    decode_process.terminate()
                    decode_process.join(timeout=1)
            collect_decoder()
            if decode_process.exitcode != 0:
                failures.append(f'Decoder exit code: {decode_process.exitcode}')
        if port is not None:
            try:
                port.close()
            except Exception as error:
                failures.append(f'Closing serial: {error}')
        if device is not None:
            try:
                if claimed:
                    usb.util.release_interface(device, 0)
            except Exception as error:
                failures.append(f'Releasing interface: {error}')
            finally:
                usb.util.dispose_resources(device)
        try:
            after, after_ports, _ = registry()
            (output / 'ioreg-after.plist').write_bytes(after)
            state['ports_after'] = after_ports
        except Exception as error:
            failures.append(f'Post-close registry: {error}')
        with state_lock:
            report = dict(state)
        report.update(failures=failures, output=str(output), elapsed_seconds=time.monotonic() - started,
                      timestamp=datetime.datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
                      allowed_tx=['keyframe_request', 'keyframe_cancel'], keyboard_packets_sent=0,
                      custom_control_packets_sent=0, serial_and_usb_concurrent=bool(
                          state['usb_bytes'] and state['serial_frames']))
        report['decoder_isolation'] = 'spawned process'
        tx = report['tx_packets']
        report['keyframe_hold_seconds'] = [b['seconds'] - a['seconds'] for a, b in zip(tx, tx[1:])
                                          if a['hex'] == REQUEST.hex(' ') and b['hex'] == CANCEL.hex(' ')]
        report['passed'] = bool(report['serial_and_usb_concurrent'] and not failures and
                              (args.no_keyframe or state['decoded_frames'] > 0) and
                              state['decode_faults'] == 0)
        (output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
        event('finished', passed=report['passed'], report=str(output / 'report.json'))
        events.close()
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
