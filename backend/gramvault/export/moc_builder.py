"""Build a per-category MOC ("Map of Content") note — plan §G3.

`<subfolder>/_moc/<category>.md` gathers everything GramVault knows about
one category: the most recent digest generated for it (citations rewritten
to links to the exported item notes), then a Dataview block and a
plain-Markdown fallback table of the category's items. Managed region, so
anything the user writes below `%% gramvault:end %%` survives a re-export.
"""

from __future__ import annotations

from datetime import datetime

from gramvault.export.index_builder import IndexEntry, escape_table_cell
from gramvault.export.markdown_builder import (
    MANAGED_END,
    MANAGED_START,
    render_frontmatter,
    sanitize_filename_component,
    user_tail,
)

MOC_SUBDIR_NAME = "_moc"


def moc_filename(category: str) -> str:
    """`_moc/<category>.md`'s basename — sanitized, stable."""
    return f"{sanitize_filename_component(category, max_length=60)}.md"


def build_moc_markdown(
    category: str,
    entries: list[IndexEntry],
    *,
    subfolder_name: str,
    digest_markdown: str | None = None,
    existing_text: str | None = None,
) -> str:
    """Render the MOC note for `category`.

    `entries` are the category's exported items (newest first is applied
    here). `digest_markdown` is the latest digest for the category with its
    `[[item:<id>]]` citations already rewritten to note links, or None.
    `existing_text` carries the user's tail across re-exports.
    """
    ordered = sorted(entries, key=lambda e: (e.date or "", e.item_id), reverse=True)

    frontmatter = render_frontmatter(
        {
            "gramvault_moc": category,
            "count": len(ordered),
            "updated": datetime.now().isoformat(timespec="seconds"),
        }
    )

    lines: list[str] = [
        f"# {category}",
        "",
        f"*{len(ordered)} item(s) · [[GramVault Index]]*",
        "",
    ]

    if digest_markdown and digest_markdown.strip():
        lines += ["## Latest digest", "", digest_markdown.strip(), ""]

    lines += [
        "## Items",
        "",
        "```dataview",
        "TABLE author AS Account, type AS Type, date AS Date",
        f'FROM "{subfolder_name}"',
        f'WHERE category = "{category}"',
        "SORT date DESC",
        "```",
        "",
        "| Account | Type | Date | Note |",
        "|---|---|---|---|",
    ]
    for entry in ordered:
        note_link = f"[[{entry.note_filename.removesuffix('.md')}]]"
        lines.append(
            "| "
            + " | ".join(
                escape_table_cell(cell)
                for cell in (entry.author, entry.media_type, entry.date, note_link)
            )
            + " |"
        )

    body = "\n".join(lines).strip()
    managed = f"{MANAGED_START}\n{body}\n{MANAGED_END}\n"
    doc = f"{frontmatter}\n{managed}"

    tail = user_tail(existing_text) if existing_text else ""
    if tail:
        doc += f"\n{tail}\n"
    return doc


__all__ = ["MOC_SUBDIR_NAME", "build_moc_markdown", "moc_filename"]
