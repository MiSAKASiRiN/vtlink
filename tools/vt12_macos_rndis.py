#!/usr/bin/env python3
"""VT12 userspace RNDIS video receiver; no kernel/network configuration changes.

Claims only the unbound RNDIS interfaces 0/1. Sends RNDIS initialization,
packet-filter control and ARP for 192.168.42.105; no robot/UDP control data.
Reuses the development kit's UDP recording, H.264 decoder and preview.
RNDIS wire structures: Linux include/linux/usb/rndis_host.h.
"""
import argparse
import json
import io
import http.server
import pathlib
import queue
import socket
import struct
import sys
import threading
import time

import usb.backend.libusb1
import usb.core
import usb.util

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from vt12_video import VideoReceiver, video_payload


class Rndis:
    def __init__(self):
        backend = usb.backend.libusb1.get_backend()
        if backend is None:
            for library in ('/opt/homebrew/lib/libusb-1.0.dylib', '/usr/local/lib/libusb-1.0.dylib'):
                if pathlib.Path(library).is_file():
                    backend = usb.backend.libusb1.get_backend(find_library=lambda _, p=library: p)
                    if backend is not None:
                        break
        if backend is None:
            raise RuntimeError('Install a matching libusb-1.0 backend')
        devices = list(usb.core.find(find_all=True, idVendor=0x2ca3,
                                     idProduct=0x001f, backend=backend))
        if len(devices) != 1:
            raise RuntimeError(f'Expected one VT12, found {len(devices)}')
        self.device = devices[0]
        self.claimed = []
        self.xid = 0
        self.log = []

    def command(self, kind, body):
        self.xid += 1
        packet = struct.pack('<III', kind, 12 + len(body), self.xid) + body
        self.device.ctrl_transfer(0x21, 0, 0, 0, packet, timeout=1000)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            response = bytes(self.device.ctrl_transfer(0xa1, 1, 0, 0, 1025, timeout=1000))
            if len(response) >= 16:
                rkind, size, xid, status = struct.unpack_from('<4I', response)
                if rkind == kind | 0x80000000 and xid == self.xid:
                    self.log.append(dict(kind=kind, request=packet.hex(), response=response.hex()))
                    if status:
                        raise RuntimeError(f'RNDIS command {kind} failed: {status:#x}')
                    if size > len(response):
                        raise RuntimeError('Truncated RNDIS response')
                    return response[:size]
            time.sleep(.02)
        raise TimeoutError(f'No RNDIS reply to command {kind}')

    def query(self, oid):
        r = self.command(4, struct.pack('<4I', oid, 0, 20, 0))
        length, offset = struct.unpack_from('<II', r, 16)
        if offset + 8 + length > len(r):
            raise ValueError('Invalid RNDIS query data bounds')
        return r[offset + 8:offset + 8 + length]

    def set_filter(self, value):
        self.command(5, struct.pack('<5I', 0x0001010e, 4, 20, 0, value))

    def open(self):
        for interface in (0, 1):
            usb.util.claim_interface(self.device, interface)
            self.claimed.append(interface)
        self.command(2, struct.pack('<3I', 1, 0, 16384))
        self.mac = self.query(0x01010102)
        if len(self.mac) != 6:
            raise ValueError('Invalid RNDIS Ethernet address')
        self.set_filter(0x0f)

    def write_ethernet(self, packet):
        header = struct.pack('<11I', 1, len(packet) + 44, 36, len(packet), 0, 0, 0, 0, 0, 0, 0)
        return self.device.write(0x01, header + packet, timeout=1000)

    def close(self):
        try:
            if len(self.claimed) == 2:
                self.set_filter(0)
        except Exception:
            pass
        for interface in reversed(self.claimed):
            usb.util.release_interface(self.device, interface)
        usb.util.dispose_resources(self.device)


class UsbVideoReceiver(VideoReceiver):
    def receive(self):
        rndis = None
        recording = None
        fragments = {}
        last_sequence = None
        last_keepalive = time.monotonic()
        try:
            rndis = Rndis()
            rndis.open()
            self.rndis = rndis
            self.mac = rndis.mac.hex(':')
            host_ip = socket.inet_aton(self.bind)
            peer_ip = socket.inet_aton(self.peer)
            if self.record:
                recording = self.prefix.with_suffix('.udpbin').open('wb')
                recording.write(b'VT12UDP1')
            # Populate the receiver's neighbor cache without a system RNDIS driver.
            arp = struct.pack('!HHBBH', 1, 0x0800, 6, 4, 1)
            arp += rndis.mac + host_ip + bytes(6) + peer_ip
            rndis.write_ethernet(b'\xff'*6 + rndis.mac + b'\x08\x06' + arp)
            self.stats['arp_requests_sent'] += 1
            self.ready.set()
            buffer = bytearray()
            while not self.stop.is_set():
                if time.monotonic() - last_keepalive > 4:
                    rndis.command(8, b'')
                    last_keepalive = time.monotonic()
                try:
                    chunk = bytes(rndis.device.read(0x81, 16384, timeout=200))
                except usb.core.USBTimeoutError:
                    continue
                self.stats['usb_reads'] += 1
                self.stats['usb_bytes'] += len(chunk)
                buffer.extend(chunk)
                while len(buffer) >= 8:
                    kind, size = struct.unpack_from('<II', buffer)
                    if kind != 1 or not 44 <= size <= 65536:
                        if not any(buffer):
                            buffer.clear()
                            break
                        # A timed-out libusb read may discard partial bytes. Recover
                        # only at a complete, bounds-checked RNDIS header.
                        found = None
                        for candidate in range(1, len(buffer) - 43):
                            if buffer[candidate:candidate + 4] != b'\x01\0\0\0':
                                continue
                            _, n, off, length = struct.unpack_from('<4I', buffer, candidate)
                            if 44 <= n <= 65536 and off == 36 and 14 <= length <= n - 44:
                                found = candidate
                                break
                        discarded = found if found is not None else max(1, len(buffer) - 43)
                        del buffer[:discarded]
                        self.stats['rndis_resync_events'] += 1
                        self.stats['rndis_discarded_bytes'] += discarded
                        continue
                    if len(buffer) < size:
                        break
                    message = bytes(buffer[:size])
                    del buffer[:size]
                    offset, length = struct.unpack_from('<II', message, 8)
                    if offset + 8 < 44 or offset + 8 + length > size:
                        raise ValueError('Invalid RNDIS Ethernet bounds')
                    ethernet = message[8 + offset:8 + offset + length]
                    self.stats['ethernet_frames'] += 1
                    if len(ethernet) < 14:
                        continue
                    protocol = ethernet[12:14]
                    data = ethernet[14:]
                    if protocol == b'\x08\x06' and len(data) >= 28:
                        self.stats['arp_received'] += 1
                        if data[:8] == struct.pack('!HHBBH', 1, 0x0800, 6, 4, 1) and data[24:28] == host_ip:
                            reply = struct.pack('!HHBBH', 1, 0x0800, 6, 4, 2)
                            reply += rndis.mac + host_ip + data[8:14] + data[14:18]
                            rndis.write_ethernet(data[8:14] + rndis.mac + b'\x08\x06' + reply)
                            self.stats['arp_replies_sent'] += 1
                        continue
                    if protocol != b'\x08\x00' or len(data) < 20 or data[0] >> 4 != 4:
                        continue
                    ihl = (data[0] & 15) * 4
                    total = int.from_bytes(data[2:4], 'big')
                    if ihl < 20 or total > len(data) or total < ihl:
                        self.stats['invalid_ip'] += 1
                        continue
                    if data[12:16] != peer_ip or data[16:20] != host_ip or data[9] != 17:
                        self.stats['other_ip_packets'] += 1
                        continue
                    flags_offset = int.from_bytes(data[6:8], 'big')
                    offset = (flags_offset & 0x1fff) * 8
                    more = bool(flags_offset & 0x2000)
                    body = data[ihl:total]
                    if offset or more:
                        self.stats['ip_fragments'] += 1
                        key = data[4:6]
                        now = time.monotonic()
                        for stale in [k for k, v in fragments.items() if now - v['time'] > 2]:
                            del fragments[stale]
                            self.stats['expired_ip_datagrams'] += 1
                        assembly = fragments.setdefault(key, dict(time=now, parts={}, size=None))
                        assembly['parts'][offset] = body
                        if not more:
                            assembly['size'] = offset + len(body)
                        position = 0
                        parts = []
                        while position in assembly['parts']:
                            part = assembly['parts'][position]
                            parts.append(part)
                            position += len(part)
                        if assembly['size'] is None or position != assembly['size']:
                            continue
                        body = b''.join(parts)
                        del fragments[key]
                        self.stats['reassembled_ip_datagrams'] += 1
                    if len(body) < 8:
                        continue
                    source, dest, size, _ = struct.unpack_from('!4H', body)
                    if dest != self.port or size < 8 or size > len(body):
                        self.stats['other_udp_packets'] += 1
                        continue
                    datagram = body[8:size]
                    self.stats['datagrams'] += 1
                    self.stats['payload_bytes'] += len(datagram)
                    self.last_packet_at = time.monotonic()
                    if recording:
                        recording.write(struct.pack('<d4sHI', self.last_packet_at - self.started,
                                                    peer_ip, source, len(datagram)) + datagram)
                    try:
                        payload = video_payload(datagram, self.header_bytes)
                    except ValueError:
                        self.stats['invalid_datagrams'] += 1
                        continue
                    sequence = int.from_bytes(datagram[1:4], 'little')
                    if last_sequence is not None:
                        delta = (sequence - last_sequence) & 0xffffff
                        if 1 < delta < 1000:
                            self.stats['sequence_gaps'] += delta - 1
                        elif delta == 0:
                            self.stats['duplicate_sequences'] += 1
                        elif delta >= 1000:
                            self.stats['reordered_or_reset_sequences'] += 1
                    last_sequence = sequence
                    try:
                        self.pending.put_nowait(payload)
                    except queue.Full:
                        self.stats['queue_drops'] += 1
        except Exception as exc:
            self.error = str(exc)
            self.stop.set()
        finally:
            self.ready.set()
            if recording:
                recording.close()
            if rndis:
                rndis.close()

    def report(self):
        result = super().report()
        result.update(transport='userspace-rndis', mac=getattr(self, 'mac', None),
                      rndis_control=getattr(getattr(self, 'rndis', None), 'log', []),
                      robot_control_tx_packets=0, application_udp_tx_packets=0)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=float, default=20)
    parser.add_argument('--preview', action='store_true')
    parser.add_argument('--record', action='store_true')
    parser.add_argument('--http-port', type=int, default=18712)
    parser.add_argument('--output', default='work/vt12-macos/live')
    args = parser.parse_args()
    if args.seconds <= 0 and not args.preview:
        parser.error('Headless mode needs positive --seconds')
    receiver = UsbVideoReceiver('192.168.42.105', 8080, '192.168.42.2', 8,
                                args.output, args.record)
    receiver.start()
    server = None
    try:
        if args.preview:
            class Handler(http.server.BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass

                def do_GET(self):
                    if self.path == '/':
                        body = b'''<!doctype html><meta charset="utf-8"><title>VT12 Live</title>
<style>body{background:#12161c;color:#e6eef8;font:16px system-ui;margin:20px}img{display:block;max-width:100%;max-height:82vh;margin:auto}pre{white-space:pre-wrap}</style>
<h2>VT12 Live</h2><img id="v"><pre id="s">Waiting...</pre>
<script>setInterval(async()=>{try{let s=await(await fetch('/status')).json();document.querySelector('#s').textContent=JSON.stringify({resolution:s.width+'x'+s.height,frames:s.decoded_frames,packets:s.datagrams,key_frames:s.key_frames||0,recovery_flags:s.frames_flagged_corrupt||0,frame_age_seconds:s.frame_idle_seconds,error:s.error},null,2);if(s.frame_idle_seconds<1){document.querySelector('#v').src='/frame.jpg?t='+Date.now();document.querySelector('#v').style.opacity=1}else{document.querySelector('#v').style.opacity=.2}}catch(e){}},100)</script>'''
                        mime = 'text/html; charset=utf-8'
                    elif self.path == '/status':
                        body = json.dumps(receiver.report()).encode()
                        mime = 'application/json'
                    elif self.path.startswith('/frame.jpg'):
                        with receiver.frames_lock:
                            frame = receiver.latest
                        if frame is None:
                            self.send_error(503)
                            return
                        output = io.BytesIO()
                        frame.to_image().save(output, format='JPEG', quality=85)
                        body, mime = output.getvalue(), 'image/jpeg'
                    else:
                        self.send_error(404)
                        return
                    self.send_response(200)
                    self.send_header('Content-Type', mime)
                    self.send_header('Cache-Control', 'no-store')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    try:
                        self.wfile.write(body)
                    except (BrokenPipeError, ConnectionResetError):
                        pass

            server = http.server.ThreadingHTTPServer(('127.0.0.1', args.http_port), Handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            print(f'Preview: http://127.0.0.1:{args.http_port}', flush=True)
            while not receiver.stop.wait(1):
                receiver.write_report()
                if args.seconds and time.monotonic() - receiver.started >= args.seconds:
                    break
        else:
            receiver.stop.wait(args.seconds)
    except KeyboardInterrupt:
        pass
    finally:
        if server:
            server.shutdown()
        result = receiver.finish(args.output + '.png')
    print(json.dumps(result, indent=2))
    return 1 if result['error'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
