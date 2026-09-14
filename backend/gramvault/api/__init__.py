"""FastAPI routers for GramVault.

Each `routes_*.py` module owns one feature area. All routers are wired
into the app in `main.py`.

  routes_import.py   -> ZIP upload, import job tracking
  routes_library.py  -> gallery grid, filters, tags
  routes_enrich.py   -> enrichment trigger + progress
  routes_chat.py     -> RAG chat (SSE) + semantic search
  routes_export.py   -> Obsidian export trigger + status
"""
