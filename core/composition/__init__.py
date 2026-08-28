"""组合存储模块包（v2.8）：零整治理的独立模块层。

云上为零（分片/分卷/分段实体）、文件管理为整（完整形态表达）：
- spec         编码化规范：组合描述符 encode/decode（三路拆分统一 schema）
- integrity    完整性：分片/整文件 SHA-256 与校验
- splitter     化整为零：大文件分卷 / 视频分段 / 文本分片
- reassembler  化零为整：分卷拼接 / 视频 concat / 文本重建（含校验）
"""

from core.composition.integrity import sha256_bytes, sha256_file  # noqa: F401
from core.composition.reassembler import (  # noqa: F401
    reassemble_text,
    reassemble_volumes,
    reassemble_video,
)
from core.composition.spec import (  # noqa: F401
    COMPOSITION_KINDS,
    decode_composition,
    encode_composition,
    is_composite,
)
from core.composition.splitter import (  # noqa: F401
    SPLIT_VOLUME_BYTES,
    split_text,
    split_video,
    split_volume,
)
