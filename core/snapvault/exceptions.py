"""SnapVault core 异常体系。"""


class SnapVaultError(Exception):
    """核心逻辑层异常基类。"""


class ConfigError(SnapVaultError):
    """配置错误。"""


class DatabaseError(SnapVaultError):
    """数据库错误（损坏、迁移失败等）。"""


class IntegrityError(SnapVaultError):
    """数据完整性错误。"""


class JobError(SnapVaultError):
    """异步任务错误。"""


class OCRUnavailableError(SnapVaultError):
    """OCR 引擎不可用（模型缺失或加载失败）。"""


class EmbeddingUnavailableError(SnapVaultError):
    """Embedding 引擎不可用。"""


class ExportError(SnapVaultError):
    """导出错误。"""


class CryptoError(SnapVaultError):
    """加解密错误。"""


class PrivacyViolationError(SnapVaultError):
    """隐私违规（检测到网络请求）。"""


class SingleInstanceError(SnapVaultError):
    """单实例冲突。"""
