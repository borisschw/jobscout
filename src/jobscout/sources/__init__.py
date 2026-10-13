"""Job source adapters."""

from jobscout.config import CompanyConfig
from jobscout.sources.base import Source, SourceError, build_source

__all__ = ["Source", "SourceError", "build_source"]
