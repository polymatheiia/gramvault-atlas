/**
 * Hand-written TypeScript mirrors of the backend's request/response
 * shapes. Field names/shapes match the JSON FastAPI serializes (pydantic
 * `model_dump(mode="json")`: datetimes as ISO 8601 strings, enums as their
 * string values).
 *
 * There is now a generated alternative: `src/api/schema.d.ts`, produced by
 * `npm run gen:api` (openapi-typescript over `openapi.json`, which
 * `gramvault openapi --out frontend/openapi.json` refreshes). New code
 * should prefer types pulled from there via the `Schemas` alias in
 * `src/api/schema.ts` — e.g. `type Digest = Schemas['Digest']`. This file
 * is retired module by module as that happens (plan §A6); already moved:
 * `lib/gallery.ts`, `pages/Pull.tsx`.
 *
 * Until a shape is moved: when you change its endpoint, run `npm run
 * gen:api` and update the mirror here too. CI's `api-types` job fails if
 * the committed `schema.d.ts` / `openapi.json` drift from the routes.
 */

export type MediaType = 'photo' | 'video' | 'reel' | 'carousel'

/** Media type for an individual file on disk (a carousel Item is made of
 * several photo/video MediaFile rows). */
export type FileMediaType = 'photo' | 'video'

/** Shared status enum for long-running jobs (import, export, jobs table). */
export type JobStatus = 'pending' | 'running' | 'done' | 'failed' | 'cancelled'

/** Kinds of background job tracked in the `jobs` table (Pipeline UI). */
export type JobKind = 'enrich' | 'categorize' | 'digest' | 'pull' | 'model_pull' | 'reembed'

export type EnrichmentStatus = 'pending' | 'running' | 'done' | 'failed'

export type TagKind = 'auto' | 'manual' | 'hashtag'

export type CategorySource = 'keyword' | 'llm' | 'manual'

export type ChatRole = 'user' | 'assistant' | 'system'

export interface Author {
  id: number | null
  username: string
  full_name: string | null
  profile_url: string | null
  avatar_path: string | null
  created_at: string | null
}

export interface Tag {
  id: number | null
  name: string
  kind: TagKind
}

export interface Category {
  id: number | null
  name: string
  sort_order: number
  color: string | null
  description: string | null
}

export interface CategoryWithCount extends Category {
  count: number
}

export interface MediaFile {
  id: number | null
  item_id: number
  file_path: string
  media_type: FileMediaType
  sequence_index: number
  width: number | null
  height: number | null
  duration_seconds: number | null
  /** Populated by faster-whisper for videos/reels. */
  transcript: string | null
  /** Populated by the llava vision model. */
  vision_caption: string | null
  checksum: string | null
  /** On-screen text read by OCR. `null` both before an attempt and after
   * a discarded one — told apart by `ocr_attempted_at`. */
  ocr_text: string | null
  ocr_attempted_at: string | null
  ocr_model: string | null
  vision_model: string | null
  transcript_model: string | null
}

export interface Item {
  id: number | null
  external_id: string | null
  author: Author | null
  media_type: MediaType
  caption: string | null
  permalink: string | null
  taken_at: string | null
  imported_at: string | null
  import_job_id: number | null
  enrichment_status: EnrichmentStatus
  category_id: number | null
  /** Resolved category name, for display. */
  category: string | null
  category_source: CategorySource | null
  category_confidence: number | null
  /** Why the classifier chose this category (keyword hits or LLM rationale). */
  category_reason: string | null
  tags: Tag[]
  media_files: MediaFile[]
}

export interface ImportJob {
  id: number | null
  source_path: string
  status: JobStatus
  total_items: number
  processed_items: number
  failed_items: number
  error_message: string | null
  started_at: string | null
  finished_at: string | null
  created_at: string | null
}

export interface ChatCitation {
  id: number | null
  message_id: number | null
  item_id: number
  media_file_id: number | null
  snippet: string | null
}

export interface ChatMessage {
  id: number | null
  session_id: number
  role: ChatRole
  content: string
  created_at: string | null
  citations: ChatCitation[]
}

export interface ChatSession {
  id: number | null
  title: string | null
  created_at: string | null
  updated_at: string | null
  messages: ChatMessage[]
}

// --- response/request wrapper shapes (from api/routes_*.py) ---

export interface ItemListResponse {
  items: Item[]
  total: number
  page: number
  page_size: number
}

// `ItemIdListResponse` now lives in the generated `api/schema.d.ts`
// (imported via `api/schema.ts` — see `lib/gallery.ts`).

export interface TagUpdateRequest {
  tags: string[]
}

export interface ImportJobListResponse {
  jobs: ImportJob[]
}

export type EnrichStepName = 'transcribe' | 'ocr' | 'vision_caption' | 'embed'

export type OcrScope = 'silent_thin_caption' | 'all_silent' | 'all_media' | 'retry_discarded'

export interface EnrichScope {
  item_ids?: number[] | null
  category?: string | null
  only_missing?: boolean
}

export interface EnrichSteps {
  transcribe: boolean
  ocr: boolean
  vision_caption: boolean
  embed: boolean
}

export interface EnrichmentRunRequest {
  /** Legacy shape — null means "all pending". Ignored when `scope` is set. */
  item_ids?: number[] | null
  scope?: EnrichScope
  steps?: EnrichSteps
  ocr_scope?: OcrScope
}

export interface EnrichmentRunResponse {
  queued_count: number
  /** The `jobs` row tracking this run — poll GET /api/jobs/{job_id}. */
  job_id: number | null
}

export interface Job {
  id: number | null
  kind: JobKind
  status: JobStatus
  params: Record<string, unknown> | null
  progress: Record<string, unknown> | null
  result: Record<string, unknown> | null
  error_message: string | null
  cancel_requested: boolean
  started_at: string | null
  finished_at: string | null
  created_at: string | null
}

// --- models / providers (GET/PUT/POST /api/models*) ---

export type AiTask = 'chat' | 'vision' | 'embedding' | 'categorize' | 'digest'
export type ProviderKind = 'ollama' | 'openai' | 'anthropic'

export interface OllamaModelInfo {
  name: string
  size: number | null
  modified_at: string | null
}

export interface ProviderInfo {
  name: string
  kind: string
  base_url: string | null
  api_key_set: boolean
}

export interface TaskRouting {
  provider: string
  model: string
  source: 'configured' | 'default'
}

export interface ModelsOverview {
  ollama_reachable: boolean
  ollama_models: OllamaModelInfo[]
  providers: ProviderInfo[]
  tasks: Record<AiTask, TaskRouting>
  auth_token_set: boolean
}

export interface SuggestedModel {
  tasks: string[]
  provider_kind: ProviderKind
  model: string
  size: string
  note: string
}

export interface TaskRoutingUpdate {
  task: AiTask
  provider: string
  model: string
  provider_kind?: ProviderKind | null
  base_url?: string | null
}

export interface TaskRoutingUpdateResponse extends ModelsOverview {
  needs_reembed: boolean
}

export interface SecretUpdate {
  provider?: string | null
  api_key?: string | null
  auth_token?: string | null
  set_auth_token?: boolean
}

export interface TestTaskResult {
  ok: boolean
  detail: string
  latency_ms: number | null
}

export interface JobStartResponse {
  job_id: number
}

// --- categories (GET/POST/PATCH/DELETE /api/library/categories) ---

export interface CategoryListResponse {
  categories: CategoryWithCount[]
  uncategorized_count: number
  total: number
}

export interface ItemCategoryUpdateRequest {
  category_id: number | null
}

export interface CategoryCreateRequest {
  name: string
  description?: string | null
  color?: string | null
  sort_order?: number | null
}

export interface CategoryUpdateRequest {
  name?: string | null
  description?: string | null
  color?: string | null
  sort_order?: number | null
}

export interface StepProgress {
  done: number
  pending: number
}

export interface EnrichmentProgress {
  total: number
  pending: number
  running: number
  done: number
  failed: number
  steps: Record<EnrichStepName, StepProgress>
  job_id: number | null
}

// --- categorize (POST /api/categorize/run, GET /api/categorize/progress) ---

export type CategorizeMethod = 'keyword' | 'llm' | 'keyword_then_llm'
export type CategorizeScopeName = 'uncategorized' | 'needs_review' | 'all'

export interface CategorizeRunRequest {
  scope?: CategorizeScopeName | { item_ids: number[] }
  method?: CategorizeMethod
}

export interface CategorizeRunResponse {
  queued_count: number
  /** The `jobs` row tracking this run — poll GET /api/jobs/{job_id}. */
  job_id: number | null
}

export interface CategorizeProgress {
  total: number
  categorized: number
  uncategorized: number
  needs_review: number
  by_source: Record<string, number>
  job_id: number | null
}

// Instagram pull (/api/pull*) types now come from the generated
// `api/schema.d.ts` — see `pages/Pull.tsx`.

// --- digests (/api/digests*) ---

export interface DigestTemplateInfo {
  name: string
  description: string
  extract_prompt: string
  reduce_prompt: string
  version: string
  source: 'builtin' | 'user'
  default_task: string
}

export interface DigestSelectionRequest {
  category?: string | null
  query?: string | null
  item_ids?: number[] | null
}

export interface DigestPreflightRequest extends DigestSelectionRequest {
  template: string
}

export interface DigestPreflightResponse {
  item_count: number
  batches: number
  estimated_tokens_in: number
  estimated_tokens_out: number
  estimated_cost: number | null
  provider: string
  model: string
}

export interface DigestCreateRequest extends DigestPreflightRequest {
  name?: string | null
}

export interface DigestCreateResponse {
  digest_id: number
  job_id: number
  item_count: number
}

export interface DigestExportResponse {
  path: string
}

export type ExportLayout = 'flat' | 'by-category' | 'by-date'

export interface ExportSettings {
  layout: ExportLayout
  media_mode: 'copy' | 'link'
  default_vault_subfolder: string
  vault_configured: boolean
}

export interface Digest {
  id: number | null
  name: string
  template: string
  template_version: string | null
  status: JobStatus
  selection: Record<string, unknown>
  item_ids: number[]
  provider: string | null
  model: string | null
  tokens_in: number
  tokens_out: number
  cost_estimate: number | null
  markdown: string | null
  error_message: string | null
  job_id: number | null
  created_at: string | null
  finished_at: string | null
}

export interface ChatSessionCreateRequest {
  title: string | null
}

export interface ChatMessageCreateRequest {
  content: string
}

export interface SemanticSearchResult {
  item: Item
  score: number
  snippet: string | null
}

export interface SemanticSearchResponse {
  query: string
  results: SemanticSearchResult[]
}

export interface ExportRequest {
  item_ids: number[] | null
  vault_subfolder: string | null
}

export interface ExportSkippedItem {
  item_id: number | null
  reason: string
}

export interface ExportJobStatus {
  id: number
  status: JobStatus
  vault_subfolder: string | null
  total_items: number
  processed_items: number
  failed_items: number
  notes_written: number
  notes_updated: number
  media_files_copied: number
  skipped: ExportSkippedItem[]
  error_message: string | null
}

export interface VaultPathCheckRequest {
  /** Omit (null) to validate the vault path already configured in config.yaml. */
  vault_dir: string | null
}

export interface VaultPathCheckResponse {
  valid: boolean
  reason: string | null
}

// --- library filter query params (GET /api/library/items) ---

export interface LibraryItemFilters {
  author?: string
  media_type?: MediaType
  tag?: string
  /** Category name, or '__uncategorized__' for items with no category. */
  category?: string
  q?: string
  date_from?: string
  date_to?: string
  page?: number
  page_size?: number
}
