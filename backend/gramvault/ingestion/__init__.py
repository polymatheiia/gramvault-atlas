"""Ingestion package (Agent A2): parses Instagram "Download Your Information"
export ZIPs, organizes any embedded media files by content hash, and writes
authors/items/media_files rows tracked by an import_jobs row.

Public surface (stable — used by both `gramvault.cli` and
`gramvault.api.routes_import`):
    - `gramvault.ingestion.parser.parse_export` / `ExportFormatError`
    - `gramvault.ingestion.organizer.organize_zip_member` /
      `organize_local_file`
    - `gramvault.ingestion.importer.import_zip` (the one-call convenience
      wrapper both the CLI and the API route use)
    - `gramvault.ingestion.linker.link_local_media` (attaches separately
      downloaded media to link-only saved items)
"""

from __future__ import annotations

from gramvault.ingestion.importer import import_zip
from gramvault.ingestion.linker import LinkReport, link_local_media
from gramvault.ingestion.parser import ExportFormatError, ParsedExport, parse_export

__all__ = [
    "ExportFormatError",
    "LinkReport",
    "ParsedExport",
    "import_zip",
    "link_local_media",
    "parse_export",
]
