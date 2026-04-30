"""Importers for various JSON chat formats.

StorageService should only do IO; parsing different formats is delegated here.
"""

from .parse import parse_imported_data

__all__ = ["parse_imported_data"]
