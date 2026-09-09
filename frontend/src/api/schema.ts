import type { components, paths } from './schema.d'

/**
 * Ergonomic aliases over the generated OpenAPI types (`schema.d.ts`, from
 * `npm run gen:api`). Prefer these for new code:
 *
 *   import type { Schemas } from '../api/schema'
 *   type Digest = Schemas['Digest']
 *
 * The hand-written mirrors in `../types` are being retired onto this a
 * module at a time (plan §A6). Migrated so far: `lib/gallery.ts`,
 * `pages/Pull.tsx`.
 */
export type Schemas = components['schemas']
export type { components, paths }
