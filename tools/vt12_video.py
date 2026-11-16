"""VT12 H.264 preview, raw UDP recording, and decoder statistics."""
import argparse
import collections
import json
import pathlib
import queue
import re
import socket
import struct
import threading
import time

import av

NAL = re.compile(b'\x00\x00\x01(.)', re.S)


def video_payload(datagram, header_bytes=8):
    if len(datagram) <= header_bytes:
        raise ValueError('Datagram is shorter than its video header')
    if header_bytes == 8 and datagram[0] != 0x47:
        raise ValueError('Expected the observed VT12 0x47 video header')
    payload = datagram[header_bytes:]
    if not payload.startswith((b'\x00\x00\x01', b'\x00\x00\x00\x01')):
        raise ValueError('Expected an Annex B start code after the video header')
    return payload


class VideoReceiver:
    def __init__(self, bind, port, peer, header_bytes, prefix, record=False):
        self.bind, self.port, self.peer = bind, port, peer
        self.header_bytes = header_bytes
        self.prefix = pathlib.Path(prefix)
        self.record = record
        self.stop = threading.Event()
        self.ready = threading.Event()
        self.frames_lock = threading.Lock()
        self.pending = queue.Queue(maxsize=64)
        self.latest = None
        self.latest_index = 0
        self.stats = collections.Counter()
        self.error = None
        self.threads = []
        self.last_packet_at = None
        self.last_frame_at = None

    def start(self):
        self.prefix.parent.mkdir(parents=True, exist_ok=True)
        self.started = time.monotonic()
        for target in (self.receive, self.decode):
            thread = threading.Thread(target=target, daemon=True)
            self.threads.append(thread)
            thread.start()

    def receive(self):
        output = None
        last_sequence = None
        try:
            if self.record:
                output = self.prefix.with_suffix('.udpbin').open('wb')
                output.write(b'VT12UDP1')
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4*1024*1024)
                sock.bind((self.bind, self.port))
                sock.settimeout(0.2)
                self.ready.set()
                while not self.stop.is_set():
                    try:
                        data, peer = sock.recvfrom(65535)
                    except socket.timeout:
                        continue
                    if self.peer and peer[0] != self.peer:
                        self.stats['other_peer_packets'] += 1
                        continue
                    self.stats['datagrams'] += 1
                    self.last_packet_at = time.monotonic()
                    self.stats['payload_bytes'] += len(data)
                    if output:
                        output.write(struct.pack('<d4sHI', time.monotonic()-self.started,
                                                 socket.inet_aton(peer[0]), peer[1], len(data))+data)
                    try:
                        payload = video_payload(data, self.header_bytes)
                    except ValueError:
                        self.stats['invalid_datagrams'] += 1
                        continue
                    if self.header_bytes == 8:
                        sequence = int.from_bytes(data[1:4], 'little')
                        if last_sequence is not None:
                            delta = (sequence-last_sequence) & 0xFFFFFF
                            if delta == 0:
                                self.stats['duplicate_sequences'] += 1
                            elif 1 < delta < 1000:
                                self.stats['sequence_gaps'] += delta-1
                            elif delta >= 1000:
                                self.stats['reordered_or_reset_sequences'] += 1
                        last_sequence = sequence
                    try:
                        self.pending.put_nowait(payload)
                    except queue.Full:
                        self.stats['queue_drops'] += 1
        except OSError as exc:
            self.error = str(exc)
            self.stop.set()
        finally:
            if output:
                output.close()
            self.ready.set()

    def decode(self):
        try:
            codec = av.CodecContext.create('h264', 'r')
            # Allow late entry before an IDR; preserve decoder recovery flags
            # so incomplete synchronization remains observable.
            codec.flags2 |= 1 << 22  # AV_CODEC_FLAG2_SHOW_ALL
            codec.thread_count = 2
            codec.thread_type = 'SLICE'
            have_parameters = False
            while not self.stop.is_set() or not self.pending.empty():
                try:
                    payload = self.pending.get(timeout=0.2)
                except queue.Empty:
                    continue
                types = {match.group(1)[0] & 31 for match in NAL.finditer(payload)}
                for kind in types:
                    self.stats['datagrams_with_nal_'+str(kind)] += 1
                if not have_parameters:
                    if not {7, 8}.issubset(types):
                        self.stats['startup_skipped_datagrams'] += 1
                        continue
                    have_parameters = True
                try:
                    for packet in codec.parse(payload):
                        self.publish(codec.decode(packet))
                except av.FFmpegError:
                    self.stats['decode_exceptions'] += 1
            if have_parameters:
                try:
                    for packet in codec.parse(b''):
                        self.publish(codec.decode(packet))
                    self.publish(codec.decode(None))
                except av.FFmpegError:
                    self.stats['decode_exceptions'] += 1
        except Exception as exc:
            self.error = str(exc)
            self.stop.set()

    def publish(self, frames):
        for frame in frames:
            self.stats['decoded_frames'] += 1
            self.last_frame_at = time.monotonic()
            if frame.is_corrupt:
                self.stats['frames_flagged_corrupt'] += 1
            if frame.key_frame:
                self.stats['key_frames'] += 1
            with self.frames_lock:
                self.latest = frame
                self.latest_index += 1

    def finish(self, snapshot=None):
        self.stop.set()
        for thread in self.threads:
            thread.join(timeout=3)
        with self.frames_lock:
            frame = self.latest
        if snapshot and frame is not None:
            pathlib.Path(snapshot).parent.mkdir(parents=True, exist_ok=True)
            frame.to_image().save(snapshot)
        result = self.report()
        result['running'] = False
        self.write_report(result)
        return result

    def report(self):
        with self.frames_lock:
            frame = self.latest
        elapsed = time.monotonic()-self.started
        result = {'bind': self.bind, 'port': self.port, 'peer_filter': self.peer,
                  'header_bytes': self.header_bytes, 'show_all': True,
                  'elapsed_seconds': elapsed, **dict(self.stats),
                  'width': frame.width if frame is not None else None,
                  'height': frame.height if frame is not None else None,
                  'decoder_fps': self.stats['decoded_frames']/elapsed if elapsed else 0,
                  'udp_idle_seconds': time.monotonic()-(self.last_packet_at or self.started),
                  'frame_idle_seconds': time.monotonic()-(self.last_frame_at or self.started),
                  'error': self.error, 'running': not self.stop.is_set()}
        return result

    def write_report(self, result=None):
        if result is None:
            result = self.report()
        temporary = self.prefix.with_suffix('.json.tmp')
        temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
        temporary.replace(self.prefix.with_suffix('.json'))


def preview(receiver, seconds):
    import tkinter as tk
    from PIL import Image, ImageTk
    window = tk.Tk()
    window.title('VT12 Video')
    window.geometry('1100x680')
    window.minsize(640, 400)
    canvas = tk.Label(window, bg='#15191e', text='Waiting for video...', fg='white')
    canvas.pack(fill='both', expand=True)
    status = tk.Label(window, anchor='w', padx=12, pady=8)
    status.pack(fill='x')
    shown = [-1]
    last_report = [0.0]
    def update():
        if receiver.stop.is_set() or (seconds and time.monotonic()-receiver.started >= seconds):
            window.destroy()
            return
        with receiver.frames_lock:
            frame, index = receiver.latest, receiver.latest_index
        if frame is not None and index != shown[0]:
            picture = frame.to_image()
            picture.thumbnail((max(canvas.winfo_width(), 1), max(canvas.winfo_height(), 1)), Image.Resampling.BILINEAR)
            canvas.image = ImageTk.PhotoImage(picture)
            canvas.configure(image=canvas.image, text='')
            shown[0] = index
        stats = receiver.stats
        resolution = f'{frame.width}x{frame.height}' if frame is not None else 'Waiting'
        idle = time.monotonic()-(receiver.last_frame_at or receiver.started)
        state = f'VIDEO PAUSED {idle:.1f}s' if idle > 1 else 'LIVE'
        status.configure(text=f'{state}  |  {resolution}  |  UDP {receiver.port}  |  Frames {stats["decoded_frames"]}  |  '
                              f'Packets {stats["datagrams"]}  |  Queue drops {stats["queue_drops"]}  |  '
                              f'Recovery flags {stats["frames_flagged_corrupt"]}')
        if time.monotonic()-last_report[0] >= 2:
            receiver.write_report()
            last_report[0] = time.monotonic()
        window.after(16, update)
    window.after(16, update)
    window.mainloop()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--bind', default='192.168.42.105')
    ap.add_argument('--port', type=int, default=8080)
    ap.add_argument('--peer', default='192.168.42.2')
    ap.add_argument('--header-bytes', type=int, choices=(0, 8), default=8)
    ap.add_argument('--headless', action='store_true')
    ap.add_argument('--seconds', type=float, default=0)
    ap.add_argument('--output', default='work/vt12-live')
    ap.add_argument('--record', action='store_true')
    ap.add_argument('--snapshot')
    args = ap.parse_args()
    if args.seconds < 0 or (args.headless and args.seconds <= 0):
        ap.error('Headless mode needs a positive --seconds duration')
    receiver = VideoReceiver(args.bind, args.port, args.peer, args.header_bytes, args.output, args.record)
    receiver.start()
    try:
        if args.headless:
            receiver.stop.wait(args.seconds)
        else:
            preview(receiver, args.seconds)
    except KeyboardInterrupt:
        pass
    finally:
        result = receiver.finish(args.snapshot)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if receiver.error else 0


if __name__ == '__main__':
    raise SystemExit(main())
