#!/usr/bin/env python3
"""Live C5 preview on macOS; only fixed keyframe request/cancel commands.

Usage: ./start_preview.command [--rotation 0|180] [--seconds 15]
Q / Esc / closing the player window stops capture and releases the device.
"""
import argparse
import datetime
import json
import os
import pathlib
import queue
import shutil
import signal
import subprocess
import threading
import time
from zoneinfo import ZoneInfo

from macos_probe import ROOT, REQUEST, CANCEL, registry, write_keyframe
from vtlink import DumlParser
from vtlink.hevc import AnnexB, START


class VideoGate:
    """Drop pre-roll until complete parameter sets and a first IRAP slice."""
    def __init__(self):
        self.framer = AnnexB()
        self.parameters = {}
        self.synced = False

    def feed(self, data):
        result = []
        for nal in self.framer.feed(data):
            marker = START.match(nal)
            if marker is None or len(nal) < marker.end() + 3:
                continue
            header = marker.end()
            kind = (nal[header] >> 1) & 63
            if kind in (32, 33, 34):
                self.parameters[kind] = nal
            if not self.synced:
                if not (len(self.parameters) == 3 and 16 <= kind <= 21 and nal[header + 2] & 0x80):
                    continue
                self.synced = True
                result.extend(self.parameters[k] for k in (32, 33, 34))
            result.append(nal)
        return b''.join(result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rotation', type=int, choices=(0, 180), default=0,
                        help='Display rotation only; default 0; select 180 only for an inverted camera')
    parser.add_argument('--seconds', type=float, default=0, help='0 runs until the window is closed')
    parser.add_argument('--fps', type=float, default=60, help='Nominal playback rate for the untimestamped HEVC stream')
    args = parser.parse_args()
    if not 0 <= args.seconds <= 86400 or not 1 <= args.fps <= 120:
        parser.error('seconds must be 0..86400; fps must be 1..120')
    import serial
    import usb.core
    import usb.util

    ffplay = shutil.which('ffplay')
    if not ffplay:
        parser.error('ffplay missing; install FFmpeg first: brew install ffmpeg')
    run = ROOT / 'work' / ('preview-' + datetime.datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y%m%d-%H%M%S'))
    run.mkdir(parents=True, exist_ok=False)
    log = (run / 'events.jsonl').open('w')
    started = time.monotonic()
    log_lock = threading.Lock()
    lock = threading.Lock()
    stop = threading.Event()
    need_keyframe = threading.Event()
    reset_gate = threading.Event()
    chunks = queue.Queue(maxsize=128)
    threads = []
    errors = []
    stats = dict(usb_bytes=0, usb_reads=0, usb_timeouts=0, serial_frames=0,
                 serial_invalid_bytes=0, queue_resets=0, sync_events=0,
                 keyframe_requests=0, keyframe_cancels=0, synced=False,
                 keyboard_packets_sent=0, custom_control_packets_sent=0)
    device = port = player = None
    claimed = pending_cancel = False

    def event(kind, **data):
        item = dict(seconds=round(time.monotonic() - started, 6), event=kind, **data)
        with log_lock:
            log.write(json.dumps(item, ensure_ascii=False) + '\n')
            log.flush()
        print(json.dumps(item, ensure_ascii=False), flush=True)

    def fail(stage, error):
        message = f'{stage}: {type(error).__name__}: {error}'
        with lock:
            errors.append(message)
        event('error', message=message)
        stop.set()

    def transmit(packet):
        event('serial_write_attempt', hex=packet.hex(' '))
        count = write_keyframe(port, packet)
        stats['keyframe_requests' if packet == REQUEST else 'keyframe_cancels'] += 1
        event('serial_write_complete', bytes=count, hex=packet.hex(' '))

    def read_usb():
        try:
            while not stop.is_set():
                try:
                    data = bytes(device.read(0x81, 65536, timeout=200))
                except usb.core.USBTimeoutError:
                    with lock:
                        stats['usb_timeouts'] += 1
                    continue
                if not data:
                    continue
                with lock:
                    stats['usb_bytes'] += len(data)
                    stats['usb_reads'] += 1
                try:
                    chunks.put_nowait(data)
                except queue.Full:
                    # Compressed data was lost: explicitly reset before a fresh IRAP.
                    while True:
                        try:
                            chunks.get_nowait()
                        except queue.Empty:
                            break
                    chunks.put_nowait(None)
                    chunks.put_nowait(data)
                    need_keyframe.set()
                    with lock:
                        stats['queue_resets'] += 1
                    event('queue_reset')
        except Exception as error:
            if not stop.is_set():
                fail('USB', error)

    def read_serial():
        framing = DumlParser()
        try:
            while not stop.is_set():
                data = port.read(max(1, min(port.in_waiting, 8192)))
                frames = framing.feed(data)
                with lock:
                    stats['serial_frames'] += len(frames)
                    stats['serial_invalid_bytes'] = framing.invalid
        except Exception as error:
            if not stop.is_set():
                fail('Protocol', error)

    def feed_player():
        gate = VideoGate()
        try:
            while not stop.is_set():
                try:
                    data = chunks.get(timeout=0.1)
                except queue.Empty:
                    continue
                if data is None or reset_gate.is_set():
                    gate = VideoGate()
                    reset_gate.clear()
                    with lock:
                        stats['synced'] = False
                    if data is None:
                        continue
                was_synced = gate.synced
                payload = gate.feed(data)
                if payload:
                    view = memoryview(payload)
                    while view and not stop.is_set():
                        written = os.write(player.stdin.fileno(), view)
                        if written <= 0:
                            raise IOError('ffplay pipe write made no progress')
                        view = view[written:]
                if gate.synced and not was_synced:
                    with lock:
                        stats['synced'] = True
                        stats['sync_events'] += 1
                    event('video_synchronized')
        except BrokenPipeError:
            stop.set()
        except Exception as error:
            if not stop.is_set():
                fail('player input', error)

    def read_player_log():
        severe = ('could not find ref', 'cu_qp_delta', 'pps id out of range',
                  'duplicate poc', 'error parsing', 'invalid nal unit')
        with (run / 'ffplay-stderr.log').open('wb') as target:
            for line in iter(player.stderr.readline, b''):
                target.write(line)
                target.flush()
                text = line.decode(errors='replace').strip()
                if any(word in text.lower() for word in severe):
                    reset_gate.set()
                    need_keyframe.set()
                    event('decoder_recovery', message=text)

    def signal_stop(_signum, _frame):
        stop.set()

    signal.signal(signal.SIGINT, signal_stop)
    signal.signal(signal.SIGTERM, signal_stop)
    try:
        _, ports, location = registry()
        if 3 not in ports:
            raise RuntimeError('Protocol CDC data interface 3 was not found')
        devices = list(usb.core.find(find_all=True, idVendor=0x2ca3, idProduct=0x1020))
        if len(devices) != 1:
            raise RuntimeError(f'Connect exactly one DJI C5; found {len(devices)}')
        device = devices[0]
        config = device.get_active_configuration()
        if config.bConfigurationValue != 1:
            raise RuntimeError('USB configuration 1 is not active')
        ep = usb.util.find_descriptor(config[(0, 0)], bEndpointAddress=0x81)
        if ep is None or usb.util.endpoint_type(ep.bmAttributes) != usb.util.ENDPOINT_TYPE_BULK:
            raise RuntimeError('Video bulk IN endpoint 0x81 not found')
        usb.util.claim_interface(device, 0)
        claimed = True
        port = serial.Serial(port=None, baudrate=961200, timeout=0.05, write_timeout=0.5,
                             xonxoff=False, rtscts=False, dsrdtr=False, exclusive=True)
        port.dtr = port.rts = False
        port.port = ports[3]
        port.open()
        command = [ffplay, '-hide_banner', '-loglevel', 'warning', '-nostats',
                   '-window_title', 'DJI C5 Live Preview - Q / Esc to exit',
                   '-x', '1280', '-y', '720', '-autoexit', '-an', '-sn',
                   '-framedrop', '-sync', 'ext', '-flags', 'low_delay',
                   '-probesize', '32768', '-analyzeduration', '0',
                   '-f', 'hevc', '-framerate', str(args.fps)]
        if args.rotation == 180:
            command += ['-vf', 'hflip,vflip']
        command += ['-i', 'pipe:0']
        player = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.PIPE, bufsize=0)
        event('connected', protocol=port.port, baudrate=961200, location=hex(location),
              rotation=args.rotation, log_directory=str(run), player_pid=player.pid)
        for target in (read_usb, read_serial, feed_player, read_player_log):
            thread = threading.Thread(target=target, name=target.__name__, daemon=True)
            threads.append(thread)
            thread.start()
        need_keyframe.set()
        next_request = time.monotonic() + 0.25
        cancel_at = next_status = 0.0
        deadline = time.monotonic() + args.seconds if args.seconds else float('inf')
        while not stop.is_set() and time.monotonic() < deadline:
            code = player.poll()
            if code is not None:
                if code != 0:
                    raise RuntimeError(f'ffplay exited with code {code}; see {run / "ffplay-stderr.log"}')
                break
            now = time.monotonic()
            if pending_cancel and now >= cancel_at:
                transmit(CANCEL)
                pending_cancel = False
            with lock:
                synced = stats['synced']
            if not pending_cancel and now >= next_request and (need_keyframe.is_set() or not synced):
                need_keyframe.clear()
                pending_cancel = True
                transmit(REQUEST)
                cancel_at = time.monotonic() + 0.15
                next_request = time.monotonic() + 2.0
            if now >= next_status:
                with lock:
                    snapshot = dict(stats)
                event('status', **snapshot)
                next_status = now + 2.0
            stop.wait(0.01)
    except Exception as error:
        fail('preview', error)
    finally:
        # Cancellation is sent even when the window closes during a reset pulse.
        if pending_cancel and port is not None and port.is_open:
            try:
                transmit(CANCEL)
            except Exception as error:
                fail('cancel on close', error)
        stop.set()
        if player is not None and player.poll() is None:
            player.terminate()
            try:
                player.wait(timeout=3)
            except subprocess.TimeoutExpired:
                player.kill()
                player.wait(timeout=2)
        for thread in threads:
            thread.join(timeout=2)
            if thread.is_alive():
                errors.append(f'{thread.name} did not stop')
        if player is not None:
            player.stdin.close()
            player.stderr.close()
        if port is not None:
            port.close()
        if device is not None:
            try:
                if claimed:
                    usb.util.release_interface(device, 0)
            except Exception as error:
                errors.append(f'USB release: {error}')
            finally:
                usb.util.dispose_resources(device)
        report = dict(stats, errors=errors, elapsed_seconds=time.monotonic() - started,
                      rotation=args.rotation, log_directory=str(run))
        (run / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
        event('closed', report=str(run / 'report.json'), errors=errors)
        log.close()
    return 1 if errors else 0


if __name__ == '__main__':
    raise SystemExit(main())
