"""VT02/12 and VT03/13 protocol SDK. Importing never opens hardware."""
from .protocol import (
    DumlParser, A5Parser, InputState, UplinkDecoder, vt12_channel,
    vt12_query, vt12_custom, vt12_keyboard, vt13_custom,
    vt13_keyboard, vt13_keyframe, vt02_set_channel, vt02_query_channel,
)

__version__ = "1.0.0"
