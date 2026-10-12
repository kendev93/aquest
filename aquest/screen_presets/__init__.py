"""筛选方案：公式选股页整套可复现输入的保存与加载。"""

from .payload import MAX_CONDITIONS, MAX_PAYLOAD_BYTES, MAX_SIGNALS, normalize_payload
from .service import (
    MAX_PRESETS,
    PRESET_NAME_MAX_LENGTH,
    PRESET_NOTE_MAX_LENGTH,
    ScreenPresetService,
)

__all__ = [
    "MAX_CONDITIONS",
    "MAX_PAYLOAD_BYTES",
    "MAX_PRESETS",
    "MAX_SIGNALS",
    "PRESET_NAME_MAX_LENGTH",
    "PRESET_NOTE_MAX_LENGTH",
    "ScreenPresetService",
    "normalize_payload",
]
