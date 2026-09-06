"""Composition storage subpackage: a standalone module layer for whole/parts
management.

The cloud holds the parts (chunk/volume/segment entities) while file
management holds the whole (the complete-form representation):
- spec         coded specification: composition descriptor encode/decode
               (one schema across all three split paths)
- integrity    integrity: per-part/whole-file SHA-256 and verification
- splitter     split into volumes: large-file volumes / video segments /
               text chunks
- reassembler  reassembly: volume concatenation / video concat / text
               reconstruction (with verification)
"""

from core.application.composition.integrity import sha256_bytes, sha256_file  # noqa: F401
from core.application.composition.reassembler import (  # noqa: F401
    reassemble_text,
    reassemble_volumes,
    reassemble_video,
)
from core.application.composition.spec import (  # noqa: F401
    COMPOSITION_KINDS,
    decode_composition,
    encode_composition,
    is_composite,
)
from core.application.composition.splitter import (  # noqa: F401
    SPLIT_VOLUME_BYTES,
    split_text,
    split_video,
    split_volume,
)
