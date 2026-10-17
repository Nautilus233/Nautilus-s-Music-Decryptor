"""User-facing error types.

Every exception raised on purpose by this package derives from
`MusicDecryptorError` so the UI can render a single, readable message
instead of a traceback.
"""
from __future__ import annotations


class MusicDecryptorError(Exception):
    """Base class for all expected / user-facing failures."""

    def __init__(self, message: str, *, detail: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail

    def __str__(self) -> str:  # noqa: D105
        return self.message if not self.detail else f"{self.message}（{self.detail}）"


class UnsupportedFormatError(MusicDecryptorError):
    """The file header / extension is not one we recognise."""


class DecryptionFailedError(MusicDecryptorError):
    """The container was recognised but could not be decoded."""


class NeedsOnlineError(MusicDecryptorError):
    """The file is bound to an online / hardware key and cannot be
    decoded offline (e.g. QQ Music `STag` files).  Triggers the
    fallback download pipeline."""


class SearchNoResultError(MusicDecryptorError):
    """Every configured search source came back empty."""


class DownloadValidationError(MusicDecryptorError):
    """A downloaded file failed header / bitrate / size validation."""


class NetworkError(MusicDecryptorError):
    """All retries for a network call were exhausted."""


class CancelledError(MusicDecryptorError):
    """The user cancelled the running job."""