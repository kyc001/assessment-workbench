export type RunStatus =
  | 'queued'
  | 'running'
  | 'waiting_human'
  | 'cancelling'
  | 'succeeded'
  | 'failed'
  | 'cancelled'
  | 'interrupted'

export interface WorkflowRun {
  id: string
  workflow: string
  status: RunStatus
  current_phase?: string | null
  created_at: string
  updated_at: string
  error?: string | null
}

export interface PhaseEvent {
  id: string
  run_id: string
  workflow: string
  phase: string
  status: 'running' | 'completed' | 'failed'
  occurrence_id: string
  round: number
  parent_run_id?: string | null
  entity_type?: string | null
  entity_id?: string | null
  input_artifact_ids: string[]
  output_artifact_ids: string[]
  started_at: string
  completed_at?: string | null
  summary?: string | null
  warnings: string[]
  error?: string | null
}

export interface HumanReview {
  id: string
  run_id: string
  phase: string
  prompt: string
  artifact_ids: string[]
  created_at: string
}

export interface ArtifactRef {
  id: string
  run_id: string
  logical_name: string
  version: number
  path: string
  media_type: string
  sha256: string
  size_bytes: number
  created_by_phase?: string | null
  created_at: string
}

export interface RunSummary {
  run: WorkflowRun
  parent_run_id?: string | null
  child_count: number
}

export interface RunDetail {
  run: WorkflowRun
  parent_run_id?: string | null
  events: PhaseEvent[]
  children: WorkflowRun[]
  human_review?: HumanReview | null
  artifacts: ArtifactRef[]
}

export interface RunSnapshot {
  detail: RunDetail
  research: ResearchRecord[]
  questions: QuestionRecord[]
  documents: DocumentRecord[]
}

export interface ResearchRecord {
  research_role: string
  attempt: number
  run_id: string
  status: RunStatus
  report_artifact_id?: string | null
  error?: string | null
}

export interface QuestionRecord {
  question_number: number
  plan_id?: string
  run_id?: string | null
  status: RunStatus
  error?: string | null
  bundle_artifact_id?: string | null
  editable_path?: string | null
  requires_human_review?: boolean
  exam_round?: number
  replacement_history?: QuestionRunVersion[]
}

export interface QuestionRunVersion {
  exam_round?: number
  run_id?: string | null
  status?: RunStatus
  bundle_artifact_id?: string | null
}

export interface DocumentRecord {
  view: 'questions' | 'solutions' | 'rubric'
  attempt: number
  run_id: string
  status: RunStatus
  source_artifact_id?: string | null
  pdf_artifact_id?: string | null
  log_artifact_id?: string | null
  inspection_artifact_id?: string | null
  page_artifact_ids: string[]
  error?: string | null
}

export interface WorkspaceInfo {
  root: string
  database: string
  run_count: number
}

export interface EditableQuestion {
  question_number: number
  sha256: string
  bundle: Record<string, unknown>
}

export interface ArtifactContent {
  artifact: ArtifactRef
  kind: 'json' | 'text' | 'binary'
  content?: unknown
}

export interface ApiErrorPayload {
  code?: string
  detail?: string
  fields?: Record<string, string>
}
