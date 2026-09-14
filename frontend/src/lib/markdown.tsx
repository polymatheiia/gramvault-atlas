import type { Root, Text } from 'mdast'
import ReactMarkdown, { type Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'
import rehypeSanitize from 'rehype-sanitize'
import { visit } from 'unist-util-visit'
import { CitationChip } from '../components/CitationChip'
import type { ChatCitation } from '../types'

/** Matches the inline citation marker format emitted by the chat backend —
 * see `backend/gramvault/chat/prompt.py` (CITATION_MARKER_REGEX) and
 * `backend/gramvault/chat/service.py::parse_citations`. */
const CITATION_MARKER_REGEX = /\[\[item:(\d+)\]\]/g

/** A remark plugin that splits `[[item:<id>]]` out of text nodes into a
 * `citation-chip` element (via `data.hName`/`hProperties`, the documented
 * way to inject a custom hast tag from a remark transform) so it survives
 * `rehype-sanitize` and reaches `<CitationChip>` through the `components`
 * map below — the raw marker is never shown to the user. */
function remarkCitations() {
  return (tree: Root) => {
    visit(tree, 'text', (node: Text, index, parent) => {
      if (!parent || typeof index !== 'number' || !node.value.includes('[[item:')) return
      const regex = new RegExp(CITATION_MARKER_REGEX)
      const pieces: Root['children'] = []
      let last = 0
      let match: RegExpExecArray | null
      while ((match = regex.exec(node.value))) {
        if (match.index > last) pieces.push({ type: 'text', value: node.value.slice(last, match.index) })
        pieces.push({
          type: 'citationReference',
          data: { hName: 'citation-chip', hProperties: { dataItemId: match[1] } },
          // mdast requires children on non-text nodes for most utilities;
          // this one is rendered entirely from hProperties.
          children: [],
        } as unknown as Root['children'][number])
        last = match.index + match[0].length
      }
      if (last < node.value.length) pieces.push({ type: 'text', value: node.value.slice(last) })
      parent.children.splice(index, 1, ...pieces)
      return index + pieces.length
    })
  }
}

/** Deliberately narrow: text formatting, lists, code, and links — no
 * images, no headings, no raw HTML, no tables (audit finding UX-5 — the
 * chat used to render markdown syntax completely unsanitized, i.e. not at
 * all, so `**bold**` and `- lists` showed up as literal text). Built from
 * scratch rather than extending `rehype-sanitize`'s `defaultSchema`, since
 * that default is far more permissive (headings, tables, images, footnotes)
 * than an LLM chat reply needs. */
const CHAT_SANITIZE_SCHEMA = {
  tagNames: ['p', 'br', 'strong', 'em', 'del', 'code', 'pre', 'ul', 'ol', 'li', 'blockquote', 'a', 'citation-chip'],
  attributes: {
    a: ['href'],
    'citation-chip': ['dataItemId'],
  },
  protocols: {
    href: ['http', 'https'],
  },
}

export interface ChatMarkdownProps {
  content: string
  citations?: ChatCitation[]
}

/** Renders one assistant/user chat message: GFM markdown (bold, lists,
 * code, links), sanitized to `CHAT_SANITIZE_SCHEMA`, with `[[item:<id>]]`
 * markers rendered as clickable `CitationChip`s instead of raw text. */
export function ChatMarkdown({ content, citations = [] }: ChatMarkdownProps) {
  const snippetByItemId = new Map<number, string | null>()
  for (const c of citations) snippetByItemId.set(c.item_id, c.snippet)

  const components: Components = {
    a: ({ href, children }) => (
      <a href={href} target="_blank" rel="noreferrer">
        {children}
      </a>
    ),
    // Not a real HTML tag — `remarkCitations` mints it above, and
    // react-markdown maps any hast tag name through `components` the same
    // way regardless of whether the browser recognizes it.
    'citation-chip': ({ ...props }: { 'data-item-id'?: string }) => {
      const itemId = Number(props['data-item-id'])
      return <CitationChip itemId={itemId} snippet={snippetByItemId.get(itemId)} />
    },
  } as Components

  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm, remarkCitations]}
      rehypePlugins={[[rehypeSanitize, CHAT_SANITIZE_SCHEMA]]}
      components={components}
    >
      {content}
    </ReactMarkdown>
  )
}
