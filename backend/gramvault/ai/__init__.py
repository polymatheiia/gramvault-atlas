"""AI pipeline package (Ollama client + ChromaDB embedding store).

`ollama_client` and `embedding_store` expose the public functions that
`gramvault.chat.*` imports for retrieval and RAG responses — keep their
signatures stable, or update those call sites alongside any change.
"""

from __future__ import annotations
