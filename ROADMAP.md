# Roadmap

GramVault Atlas is feature-complete for its intended use: pull/import →
enrich → categorize → digest → Obsidian, plus gallery, feed, and chat.
Everything in `docs/DESIGN.md` §J is implemented and tested.

This file tracks nice-to-haves. None are commitments. If you want to pick
one up, open an issue first to discuss the approach, and see
[CONTRIBUTING.md](CONTRIBUTING.md).

## Known open items (from the design doc)

- **Finish the `types.ts` migration.** Frontend request/response types are
  moving onto the generated `src/api/schema.ts` a module at a time
  (`lib/gallery.ts` and `pages/Pull.tsx` are done). Convert the rest.
- **Category MOC screenshots / real feed recording** for the README.

## Ideas by area

**Ingestion**
- `gramvault link-media` as an Import-page action (pick a directory, show
  matched/unmatched counts) for non-terminal users.
- Content-hash fallback in the linker, to catch files renamed after
  download (currently matched by shortcode-in-filename only).
- Incremental re-import of a newer export without re-processing unchanged
  items.

**Pull**
- A scheduled pull (cron/systemd timer) with a "new items" summary.
- Support for Chromium-family local cookie stores, not just Firefox.

**AI pipeline**
- GPU-aware batching for `faster-whisper`.
- Configurable enrichment concurrency.

**Chat / search**
- Conversation history persisted across sessions.
- Retrieval knobs (top-k, hybrid weighting) exposed in Settings.

**Obsidian**
- Selective re-export by tag/date range from the Gallery, not just
  whole-library.
- Configurable note templates.

**Packaging**
- A signed/packaged release (PyInstaller or Electron wrapper) for users
  who don't want a Python/Node toolchain.
