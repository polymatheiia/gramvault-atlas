"""008: per-segment Whisper transcript timestamps.

Audit finding R11: `faster-whisper` already returns timed segments
(`ai.transcription.TranscriptionResult.segments`), but only the flattened
`media_files.transcript` text was ever persisted — the timestamps were
discarded. This is what a caption-deep-link or a WebVTT `<track>` (see the
audit's UX-11) would need, so keep them.

One row per (media_file, sequence_index); `ai.pipeline` deletes and
re-inserts a media file's segments whenever it (re-)transcribes, mirroring
`chat.fts.reindex`'s delete-then-insert shape.

`.py` (not `.sql`) for consistency with 002/003/004 and the re-runnable
`CREATE TABLE IF NOT EXISTS`; `schema.sql` carries the same table inline
and the parity test keeps them in sync.
"""

from __future__ import annotations

import sqlite3

_TRANSCRIPT_SEGMENTS_TABLE = """
CREATE TABLE IF NOT EXISTS transcript_segments (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    media_file_id  INTEGER NOT NULL REFERENCES media_files(id) ON DELETE CASCADE,
    sequence_index INTEGER NOT NULL,
    start_seconds  REAL NOT NULL,
    end_seconds    REAL NOT NULL,
    text           TEXT NOT NULL
)
"""


def up(conn: sqlite3.Connection) -> None:
    conn.execute(_TRANSCRIPT_SEGMENTS_TABLE)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_transcript_segments_media_file_id "
        "ON transcript_segments(media_file_id)"
    )
