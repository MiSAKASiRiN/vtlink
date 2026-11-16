"""Incremental HEVC Annex B framing and parameter-set/IRAP synchronization."""
import collections
import re


START = re.compile(b'\x00\x00(?:\x00)?\x01')


class AnnexB:
    def __init__(self):
        self.buffer = bytearray()

    def feed(self, data):
        self.buffer.extend(data)
        matches = list(START.finditer(self.buffer))
        if len(matches) < 2:
            if len(self.buffer) > 16 * 1024 * 1024:
                self.buffer.clear()
                raise ValueError('HEVC NAL exceeded 16 MiB without a boundary')
            return []
        result = [bytes(self.buffer[a.start():b.start()]) for a, b in zip(matches, matches[1:])]
        del self.buffer[:matches[-1].start()]
        return result


class SyncDecoder:
    """Discard pre-roll until complete parameter sets and a fresh IRAP."""
    def __init__(self):
        import av
        self.av = av
        av.logging.set_level(av.logging.WARNING)
        self.framer = AnnexB()
        self.parameters = {}
        self.context = None
        self.synced = False
        self.frames = 0
        self.warning_count = 0
        self.faults = 0
        self.nals = collections.Counter()
        self.last_fault = ''
        self.last_resolution = None
        self.sync_events = 0

    def reset(self):
        self.parameters.clear()
        self.context = None
        self.synced = False

    def feed(self, data):
        output = []
        needs_reset = False
        for nal in self.framer.feed(data):
            marker = START.match(nal)
            if marker is None or len(nal) < marker.end() + 3:
                continue
            header = marker.end()
            kind = (nal[header] >> 1) & 63
            self.nals[kind] += 1
            if kind in (32, 33, 34):
                self.parameters[kind] = nal
            if not self.synced:
                if not (16 <= kind <= 21 and nal[header + 2] & 0x80 and len(self.parameters) == 3):
                    continue
                self.context = self.av.CodecContext.create('hevc', 'r')
                self.context.thread_count = 1
                self.synced = True
                self.sync_events += 1
                nal = b''.join(self.parameters[k] for k in (32, 33, 34)) + nal
            try:
                frames = []
                with self.av.logging.Capture() as logs:
                    for packet in self.context.parse(nal):
                        frames.extend(self.context.decode(packet))
                self.warning_count += len(logs)
                severe = [entry[2].strip() for entry in logs if any(value in entry[2].lower() for value in (
                    'cu_qp_delta', 'could not find ref', 'pps id out of range', 'duplicate poc',
                    'error parsing', 'invalid nal unit'))]
                if severe or any(frame.is_corrupt for frame in frames):
                    self.last_fault = severe[0] if severe else 'decoder flagged corrupt frame'
                    self.faults += 1
                    self.reset()
                    needs_reset = True
                    continue
                for frame in frames:
                    self.frames += 1
                    self.last_resolution = (frame.width, frame.height)
                    output.append(frame)
            except self.av.FFmpegError as error:
                self.last_fault = str(error)
                self.faults += 1
                self.reset()
                needs_reset = True
        return output, needs_reset
