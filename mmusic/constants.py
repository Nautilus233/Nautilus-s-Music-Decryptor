"""Static tables, magic bytes and extension maps.

All cipher material below is transcribed from public open-source
reference implementations (``unlock-music`` ``qmc_cipher.ts`` / ``ncm.ts`` /
``qmc_key.ts``, and ``qmcdump`` ``crypt.cpp``).  This file ships **no
audio content** - only the published algorithms needed to read files the
user already owns locally.
"""
from __future__ import annotations

# ------------------------------------------------------------------ magic
NCM_MAGIC = b"CTENFDAM"

# Decrypted-audio container magic, used to (a) sniff the payload and
# (b) self-verify the audio start offset inside an .ncm container.
AUDIO_MAGICS: "dict[bytes, str]" = {
    b"fLaC": "flac",
    b"OggS": "ogg",
    b"RIFF": "wav",
    b"MAC ": "ape",
    b"ID3": "mp3",
    b"\xff\xfb": "mp3",
    b"\xff\xf3": "mp3",
    b"\xff\xf2": "mp3",
    b"\xff\xfa": "mp3",
    b"\xff\xf1": "aac",
    b"FRM8": "dff",
}
M4A_FTYP = b"ftyp"          # lives at offset 4 of an m4a/mp4 file

# ------------------------------------------------------------------ NCM
NCM_CORE_KEY = bytes.fromhex("687a4852416d736f356b496e62617857")   # b"hzHRAmso5kInbaxW"
NCM_META_KEY = bytes.fromhex("2331346C6A6B5F215C5D2630553C2728")   # b"#14ljk_!\\]&0U<'"
NCM_KEY_XOR = 0x64
NCM_META_XOR = 0x63

# ------------------------------------------------------------------ QMC
# QMCv1 static stream-cipher box (256 bytes).
QMC_STATIC_CIPHER_BOX = bytes.fromhex(
    "77483273def2c0c895ec30b251c3e1a09ee69dcffa7f14d1ceb8dcc34a6793d6"
    "28c29170ca8da2a4f00861907e6fa2e0ebae3eb667c792f491b5f66c5e8440f7"
    "f31b027fd5ab418928f425cc5211ad4368a6418b84b5ff2c924a26d8476a7c95"
    "61cce6cbbb3f47588975c375a1d9afcc087317dcaa9aa21641d8a206c68bfc66"
    "349fcf1823a00a74e72b277092e9af37e68ca7bc62659cc208c988b3f343ac74"
    "2c0fd4afa1c30164954e489ff43578957a39d66aa06d40e84fa8ef111df31b3f"
    "3f07dd6f5b193019fbef0e37f00ecd1649fe5347131abda4f14019600eed6809"
    "065f4dcf3d1afe2077e4d9daf9a42b761c71db00bcfd0c6ca547f7f600794a11"
)
assert len(QMC_STATIC_CIPHER_BOX) == 256

QMC_MIX_KEY1 = bytes([0x33, 0x38, 0x36, 0x5A, 0x4A, 0x59, 0x21, 0x40,
                      0x23, 0x2A, 0x24, 0x25, 0x5E, 0x26, 0x29, 0x28])
QMC_MIX_KEY2 = bytes([0x2A, 0x2A, 0x23, 0x21, 0x28, 0x23, 0x24, 0x25,
                      0x26, 0x5E, 0x61, 0x31, 0x63, 0x5A, 0x2C, 0x54])
QMC_ENC_V2_PREFIX = b"QQMusic EncV2,Key:"
QMC_TAG_STAG = b"STag"
QMC_TAG_QTAG = b"QTag"
QMC_MAP_MAX_KEY_LEN = 300     # longer derived keys use the RC4 stream cipher

# extension -> (cipher family, output extension)
# family "qmc1" = static cipher, "qmc2" = keyed (map / rc4) cipher.
QMC_EXT_MAP: "dict[str, tuple[str, str]]" = {
    "qmc0": ("qmc2", "mp3"),
    "qmc2": ("qmc2", "ogg"),
    "qmc3": ("qmc2", "mp3"),
    "qmc4": ("qmc2", "ogg"),
    "qmc6": ("qmc2", "ogg"),
    "qmc8": ("qmc2", "ogg"),
    "qmcflac": ("qmc2", "flac"),
    "qmcogg": ("qmc2", "ogg"),
    "mgg": ("qmc2", "ogg"),
    "mgg0": ("qmc2", "ogg"),
    "mgg1": ("qmc2", "ogg"),
    "mggl": ("qmc2", "ogg"),
    "mflac": ("qmc2", "flac"),
    "mflac0": ("qmc2", "flac"),
    "mmp4": ("qmc2", "mmp4"),
    "bkcmp3": ("qmc1", "mp3"),
    "bkcm4a": ("qmc1", "m4a"),
    "bkcflac": ("qmc1", "flac"),
    "bkcwav": ("qmc1", "wav"),
    "bkcape": ("qmc1", "ape"),
    "bkcogg": ("qmc1", "ogg"),
    "bkcwma": ("qmc1", "wma"),
    "tkm": ("qmc1", "m4a"),
    "666c6163": ("qmc1", "flac"),
    "6d7033": ("qmc1", "mp3"),
    "6f6767": ("qmc1", "ogg"),
    "6d3461": ("qmc1", "m4a"),
    "776176": ("qmc1", "wav"),
}

# QQ Music extensions that *always* belong to the keyed (QMCv2) family.
QMC_V2_EXTS = {"mgg", "mgg0", "mgg1", "mggl", "mflac", "mflac0", "mmp4",
               "qmcflac", "qmcogg", "qmc0", "qmc2", "qmc3", "qmc4", "qmc6", "qmc8"}
# Every extension we recognise as "some kind of QQ Music encrypted file".
QMC_ALL_EXTS = set(QMC_EXT_MAP)

AUDIO_MIME: "dict[str, str]" = {
    "mp3": "audio/mpeg",
    "flac": "audio/flac",
    "ogg": "audio/ogg",
    "m4a": "audio/mp4",
    "mmp4": "audio/mp4",
    "aac": "audio/aac",
    "wav": "audio/wav",
    "ape": "audio/x-ape",
    "wma": "audio/x-ms-wma",
    "dff": "audio/x-dff",
}