import type {
  ApiErrorPayload,
  ArtifactContent,
  EditableQuestion,
  RunSnapshot,
  RunSummary,
  WorkflowRun,
  WorkspaceInfo,
} from './types'

export class ApiError extends Error {
  status: number
  code?: string

  constructor(status: number, payload: ApiErrorPayload) {
    super(payload.detail || `请求失败：HTTP ${status}`)
    this.status = status
    this.code = payload.code
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers)
  if (init?.body && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json')
  const response = await fetch(path, { ...init, headers })
  if (!response.ok) {
    const payload = (await response.json().catch(() => ({}))) as ApiErrorPayload
    throw new ApiError(response.status, payload)
  }
  return response.json() as Promise<T>
}

export const api = {
  workspace: () => request<WorkspaceInfo>('/api/workspace'),
  runs: () => request<RunSummary[]>('/api/runs'),
  snapshot: (runId: string) => request<RunSnapshot>(`/api/runs/${runId}/snapshot`),
  createExam: (payload: {
    subject: string
    target_level: string
    requirements: string
    source_context: string
    human_gates: boolean
    compile_pdf: boolean
  }) =>
    request<{ run: WorkflowRun }>('/api/exams', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  runAction: (runId: string, action: string, reason = '') =>
    request<{ run: WorkflowRun }>(`/api/runs/${runId}/${action}`, {
      method: 'POST',
      body: JSON.stringify({ actor: 'gui-user', reason }),
    }),
  resume: (runId: string) =>
    request<{ run: WorkflowRun }>(`/api/runs/${runId}/resume`, { method: 'POST' }),
  cancel: (runId: string) =>
    request<{ run: WorkflowRun }>(`/api/runs/${runId}/cancel`, { method: 'POST' }),
  question: (runId: string, number: number) =>
    request<EditableQuestion>(`/api/exams/${runId}/questions/${number}`),
  saveQuestion: (runId: string, number: number, payload: EditableQuestion) =>
    request<EditableQuestion>(`/api/exams/${runId}/questions/${number}`, {
      method: 'PUT',
      body: JSON.stringify({ expected_sha256: payload.sha256, bundle: payload.bundle }),
    }),
  rerunQuestion: (runId: string, number: number, feedback: string) =>
    request<{ run: WorkflowRun }>(`/api/exams/${runId}/questions/${number}/rerun`, {
      method: 'POST',
      body: JSON.stringify({ feedback: feedback.trim() ? [feedback.trim()] : [] }),
    }),
  publishQuestion: (runId: string, number: number, childRunId: string) =>
    request<EditableQuestion>(`/api/exams/${runId}/questions/${number}/publish`, {
      method: 'POST',
      body: JSON.stringify({ child_run_id: childRunId }),
    }),
  assembleEdited: (runId: string) =>
    request<{ run: WorkflowRun }>(`/api/exams/${runId}/assemble-edited`, { method: 'POST' }),
  artifact: (artifactId: string) => request<ArtifactContent>(`/api/artifacts/${artifactId}`),
  artifactUrl: (artifactId: string) => `/api/artifacts/${artifactId}/download`,
}
