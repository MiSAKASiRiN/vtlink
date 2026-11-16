import json
import pathlib
import unittest
from vtlink import *
from vtlink.business import business_frame, aa_type2_transport, crc
from vtlink.hevc import AnnexB

ROOT = pathlib.Path(__file__).resolve().parents[1]


class ProtocolTests(unittest.TestCase):
    def test_vt12_hardware_capture(self):
        expected = bytes.fromhex('553f048f8a772d01400963a52b00b402a51e002da30203c00102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d16f18aad2d01dbaea2a0')
        packet = vt12_custom(bytes([0xc0])+bytes(range(1,30)), a5_sequence=301,
                             transport_sequence=301, timestamp_ms=0xad8a, sequence=301, aa_type2=False)
        self.assertEqual(packet, expected)
        self.assertEqual(len(packet[16:-8]), 39)  # Complete A5 frame before trailer/CRCs.

    def test_vt13_independent_current_vector(self):
        expected = bytes.fromhex('553f048faa770000400963aa2b00a602a51e0011be1103000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1daf5176903412f6d19412')
        self.assertEqual(vt13_custom(bytes(range(30)), a5_sequence=17,
                         transport_sequence=0x1234, timestamp_ms=0x9076), expected)

    def test_aa_type2_fixed_vector(self):
        expected = bytes.fromhex('aa2b00a602a51e00007d0203000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1dc280e8030100ef0c')
        self.assertEqual(vt12_custom(bytes(range(30)), timestamp_ms=1000, transport_sequence=1)[11:-2], expected)

    def test_family_and_command_separation(self):
        for encode,sender,command in [(vt12_custom,0x8a,0x302),(vt13_custom,0xaa,0x311)]:
            frame = DumlParser().feed(encode(bytes(range(30))))[0]
            self.assertEqual((frame['sender'],frame['receiver'],frame['set'],frame['id']),(sender,0x77,9,0x63))
            decoded,_ = UplinkDecoder().feed(frame['payload'])
            self.assertEqual(decoded[0]['command_id'],command)
            self.assertEqual(decoded[0]['payload_hex'],bytes(range(30)).hex())

    def test_payload_limits(self):
        for encoder in (vt12_custom,vt13_custom):
            for payload in (b'',bytes(29),bytes(31),bytes(60),'x'*30):
                with self.assertRaises(ValueError):encoder(payload)

    def test_channel_and_query_vectors(self):
        self.assertEqual(vt12_query().hex(),'550d04338a0e0000400961eb31')
        self.assertEqual(vt12_channel(3).hex(),'551304038a0e000040090d0f1e0a111e0a8690')
        for n in (0,7,True,3.0):
            with self.assertRaises(ValueError):vt12_channel(n)

    def test_transmitter_manual_vectors(self):
        self.assertEqual(vt02_set_channel(1).hex(),'a501000068010f018bbd')
        self.assertEqual(vt02_query_channel().hex(),'a500000abd020f6604')

    def test_keyframe_and_keyboard_reference(self):
        vectors = json.loads((ROOT/'profiles/vt13-vectors.json').read_text())
        for reset,key in [(True,'request_hex'),(False,'cancel_hex')]:
            self.assertEqual(vt13_keyframe(reset),bytes.fromhex(vectors['keyframe'][key]))
        for v in vectors['keyboard']:
            self.assertEqual(list(vt13_keyboard(InputState(**v['state']))),[bytes.fromhex(s) for s in v['packets_hex']])

    def test_stream_noise_crc_and_fragmentation(self):
        good = vt13_custom(bytes(range(30)))
        bad = bytearray(good); bad[-1] ^= 1
        parser = DumlParser(); result=[]
        for b in b'noise'+bad+good+good:
            result.extend(parser.feed(bytes([b])))
        self.assertEqual(len(result),2)
        self.assertGreater(parser.invalid,0)
        self.assertFalse(parser.buffer)

    def test_uplink_fragmented_a5_and_bad_transport(self):
        inner = business_frame(0x0310,bytes(range(100)),41)
        parser = UplinkDecoder()
        self.assertEqual(parser.feed_a5(inner[:12]),[])
        self.assertEqual(parser.feed_a5(inner[12:])[0]['command_id'],0x0310)
        bad = bytearray(aa_type2_transport(inner)); bad[-1]^=1
        with self.assertRaises(ValueError):parser.feed(bad)

    def test_keyboard_has_full_trailer(self):
        p = DumlParser().feed(vt12_keyboard(x=37, y=-19, left=True, keys=0x55aa))[0]['payload']
        inner = p[5:-6]
        decoded = A5Parser().feed(inner)
        self.assertEqual(len(decoded),1)
        self.assertEqual(decoded[0][0],0x0304)
        self.assertEqual(len(decoded[0][1]),12)
        self.assertEqual(crc(p[:-2],0x1862,0x8408),int.from_bytes(p[-2:],'little'))

    def test_annexb_boundaries_split_across_reads(self):
        units=[b'\0\0\0\1\x40\x01abc',b'\0\0\1\x42\x01def',b'\0\0\1\x44\x01ghi']
        parser=AnnexB(); result=[]
        for b in b''.join(units):result.extend(parser.feed(bytes([b])))
        self.assertEqual(result,units[:2])


if __name__ == '__main__':
    unittest.main()
