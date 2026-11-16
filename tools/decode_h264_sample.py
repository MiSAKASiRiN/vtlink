import argparse
import collections
import json
import pathlib
import re
import struct
import time
import av

ap=argparse.ArgumentParser()
ap.add_argument('capture',type=pathlib.Path)
ap.add_argument('--header-bytes',type=int,default=8)
ap.add_argument('--show-all',action='store_true')
ap.add_argument('--output',type=pathlib.Path,default=pathlib.Path('work/video-sample'))
args=ap.parse_args()
data=args.capture.read_bytes()
assert data[:8]==b'VT12UDP1'
cursor=8
codec=av.CodecContext.create('h264','r')
if args.show_all:
    codec.flags2 |= 1 << 22
counts=collections.Counter()
nals=collections.Counter()
lengths=collections.Counter()
errors=[]
prefixes=[]
first_time,last_time=None,None
args.output.parent.mkdir(parents=True,exist_ok=True)
snapshot=args.output.with_suffix('.showall.png' if args.show_all else '.png')
elementary=args.output.with_suffix('.h264')
start=time.monotonic()
dimensions=collections.Counter()
last_frame=[None]
def decode_packet(packet):
    try:
        for frame in codec.decode(packet):
            counts['decoded_frames']+=1
            last_frame[0]=frame
            if frame.is_corrupt:
                counts['corrupt_frames']+=1
            dimensions[f'{frame.width}x{frame.height}']+=1
            if frame.key_frame:
                counts['key_frames']+=1
            if counts['decoded_frames']==30:
                frame.to_image().save(snapshot)
    except av.FFmpegError as exc:
        counts['decode_exceptions']+=1
        if len(errors)<10:
            errors.append({'datagram':counts['datagrams'],'message':str(exc)})
with av.logging.Capture() as logs, elementary.open('wb') as output:
    while cursor<len(data):
        elapsed,peer,port,length=struct.unpack_from('<d4sHI',data,cursor)
        cursor+=18
        payload=data[cursor:cursor+length]
        cursor+=length
        assert len(payload)==length
        counts['datagrams']+=1
        counts['payload_bytes']+=length
        lengths[length]+=1
        first_time=elapsed if first_time is None else first_time
        last_time=elapsed
        if len(prefixes)<5:
            prefixes.append(payload[:args.header_bytes].hex(' '))
        h264=payload[args.header_bytes:]
        if h264.startswith((b'\x00\x00\x00\x01',b'\x00\x00\x01')):
            counts['annexb_at_payload_start']+=1
        for marker in re.finditer(b'\x00\x00\x01(.)',h264,re.S):
            nals[marker.group(1)[0]&31]+=1
        output.write(h264)
        for packet in codec.parse(h264):
            decode_packet(packet)
    for packet in codec.parse(b''):
        decode_packet(packet)
    decode_packet(None)
    if last_frame[0] is not None:
        last_frame[0].to_image().save(args.output.with_suffix('.last.png'))
result={'capture':str(args.capture),'header_bytes':args.header_bytes,'av_version':av.__version__,
        'show_all':args.show_all,
        'decode_exceptions':counts['decode_exceptions'],'corrupt_frames':counts['corrupt_frames'],
        **counts,'dimensions':dict(dimensions),'nal_types':dict(nals),'header_examples':prefixes,
        'packet_size_range':[min(lengths),max(lengths)] if lengths else [],
        'capture_duration_seconds':last_time-first_time if last_time else 0,
        'decode_elapsed_seconds':time.monotonic()-start,'errors':errors,
        'ffmpeg_log_messages':len(logs),'ffmpeg_first_messages':logs[:8],
        'snapshot':str(snapshot) if snapshot.exists() else None,'elementary_stream':str(elementary)}
args.output.with_suffix('.showall.decode.json' if args.show_all else '.decode.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(json.dumps(result,ensure_ascii=False,indent=2))
