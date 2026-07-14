import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import katex from 'katex'
import { Braces, Eye, PackageCheck, Play, Save, Upload } from 'lucide-react'
import { api } from '../api'
import type { QuestionRecord } from '../types'
import StatusPill from './StatusPill'

interface Props {
  runId: string
  questions: QuestionRecord[]
  onRunCreated: (runId: string) => void
}

type ContentBlock = { kind?: string; content?: string }
type QuestionPart = { id?: string; label?: string; prompt?: unknown; score?: number }
type SolutionStep = {
  id?: string
  description?: unknown
  expression?: string | null
  conclusion?: unknown
}
type RubricItem = {
  id?: string
  description?: unknown
  score?: number
  partial_credit?: Array<{ score?: number; condition?: string }>
}

function MathBlock({ content, display }: { content: string; display: boolean }) {
  const html = useMemo(() => {
    try {
      return katex.renderToString(content, { displayMode: display, throwOnError: false })
    } catch {
      return content
    }
  }, [content, display])
  return <span className={display ? 'math-display' : 'math-inline'} dangerouslySetInnerHTML={{ __html: html }} />
}

function Blocks({ blocks }: { blocks: unknown }) {
  if (!Array.isArray(blocks)) return null
  return (
    <>
      {(blocks as ContentBlock[]).map((block, index) => {
        const content = block.content || ''
        if (block.kind === 'display_math') return <MathBlock key={index} content={content} display />
        if (block.kind === 'inline_math') return <MathBlock key={index} content={content} display={false} />
        return <span key={index}>{content}</span>
      })}
    </>
  )
}

function QuestionPreview({ bundle }: { bundle: Record<string, unknown> }) {
  const question = (bundle.question || {}) as Record<string, unknown>
  const solution = (bundle.solution || {}) as Record<string, unknown>
  const rubric = (bundle.rubric || {}) as Record<string, unknown>
  const options = Array.isArray(question.options) ? question.options : []
  const parts = Array.isArray(question.parts) ? (question.parts as QuestionPart[]) : []
  const steps = Array.isArray(solution.steps) ? (solution.steps as SolutionStep[]) : []
  const items = Array.isArray(rubric.items) ? (rubric.items as RubricItem[]) : []
  return (
    <div className="question-preview">
      <div className="question-heading">
        <span>第 {String(question.number || '')} 题</span>
        <strong>{String(question.score || '')} 分</strong>
      </div>
      <div className="question-statement"><Blocks blocks={question.statement} /></div>
      {!!options.length && (
        <ol className="option-list" type="A">
          {options.map((option, index) => <li key={index}><Blocks blocks={option} /></li>)}
        </ol>
      )}
      {!!parts.length && (
        <section className="preview-section">
          <h4>分问</h4>
          <ol className="question-parts">
            {parts.map((part, index) => (
              <li key={part.id || index}>
                <div><strong>{part.label || `(${index + 1})`}</strong><span>{part.score || '—'} 分</span></div>
                <p><Blocks blocks={part.prompt} /></p>
              </li>
            ))}
          </ol>
        </section>
      )}
      <section className="preview-section">
        <h4>解题过程</h4>
        <ol className="solution-steps">
          {steps.map((step, index) => (
            <li key={step.id || index}>
              <div><Blocks blocks={step.description} /></div>
              {step.expression && <MathBlock content={step.expression} display />}
              {step.conclusion !== undefined && step.conclusion !== null && (
                <div className="step-conclusion"><Blocks blocks={step.conclusion} /></div>
              )}
            </li>
          ))}
        </ol>
      </section>
      <section className="preview-section">
        <h4>最终答案</h4>
        <div><Blocks blocks={solution.final_answer} /></div>
      </section>
      <section className="preview-section">
        <h4>评分点</h4>
        <ol className="rubric-items">
          {items.map((item, index) => (
            <li key={item.id || index}>
              <div><Blocks blocks={item.description} /> <strong>{item.score || '—'} 分</strong></div>
              {!!item.partial_credit?.length && (
                <ul>{item.partial_credit.map((level, levelIndex) => <li key={levelIndex}>{level.score ?? '—'} 分：{level.condition || '—'}</li>)}</ul>
              )}
            </li>
          ))}
        </ol>
      </section>
    </div>
  )
}

export default function QuestionWorkspace({ runId, questions, onRunCreated }: Props) {
  const queryClient = useQueryClient()
  const ordered = useMemo(() => [...questions].sort((a, b) => a.question_number - b.question_number), [questions])
  const [selected, setSelected] = useState<number | null>(null)
  const [mode, setMode] = useState<'preview' | 'json'>('preview')
  const [draft, setDraft] = useState('')
  const [feedback, setFeedback] = useState('')
  const [localError, setLocalError] = useState('')
  const [selectedChild, setSelectedChild] = useState('')

  useEffect(() => {
    if (selected === null && ordered.length) setSelected(ordered[0].question_number)
  }, [ordered, selected])

  const questionQuery = useQuery({
    queryKey: ['question', runId, selected],
    queryFn: () => api.question(runId, selected!),
    enabled: selected !== null && !!ordered.find((item) => item.question_number === selected)?.editable_path,
    retry: false,
  })

  useEffect(() => {
    if (questionQuery.data) setDraft(JSON.stringify(questionQuery.data.bundle, null, 2))
  }, [questionQuery.data])

  const saveMutation = useMutation({
    mutationFn: async () => {
      if (!questionQuery.data || selected === null) throw new Error('题目尚未加载')
      let bundle: Record<string, unknown>
      try {
        bundle = JSON.parse(draft) as Record<string, unknown>
      } catch (error) {
        throw new Error(`JSON 解析失败：${String(error)}`)
      }
      return api.saveQuestion(runId, selected, { ...questionQuery.data, bundle })
    },
    onSuccess: (data) => {
      setDraft(JSON.stringify(data.bundle, null, 2))
      setLocalError('')
      queryClient.setQueryData(['question', runId, selected], data)
    },
    onError: (error) => setLocalError(error.message),
  })

  const rerunMutation = useMutation({
    mutationFn: () => api.rerunQuestion(runId, selected!, feedback),
    onSuccess: ({ run }) => {
      onRunCreated(run.id)
      setFeedback('')
    },
  })

  const assemblyMutation = useMutation({
    mutationFn: () => api.assembleEdited(runId),
    onSuccess: ({ run }) => onRunCreated(run.id),
  })

  const current = ordered.find((item) => item.question_number === selected)
  const versions = useMemo(() => {
    if (!current) return []
    const candidates = [
      current.run_id ? { run_id: current.run_id, status: current.status, current: true } : null,
      ...(current.replacement_history || []).map((item) => ({ ...item, current: false })),
    ].filter((item): item is { run_id: string; status?: QuestionRecord['status']; current: boolean } => !!item?.run_id)
    return candidates.filter((item, index) => candidates.findIndex((candidate) => candidate.run_id === item.run_id) === index)
  }, [current])

  useEffect(() => {
    setSelectedChild(current?.run_id || '')
  }, [current?.run_id, selected])

  const publishMutation = useMutation({
    mutationFn: () => api.publishQuestion(runId, selected!, selectedChild),
    onSuccess: (data) => {
      setDraft(JSON.stringify(data.bundle, null, 2))
      setLocalError('')
      queryClient.setQueryData(['question', runId, selected], data)
      queryClient.invalidateQueries({ queryKey: ['snapshot', runId] })
    },
  })

  return (
    <div className="question-workspace">
      <div className="question-list-panel">
        <div className="panel-toolbar">
          <div><strong>题目</strong><span>{ordered.length} 个 slot</span></div>
          <button className="button secondary compact" disabled={!ordered.length || assemblyMutation.isPending} onClick={() => assemblyMutation.mutate()}>
            <PackageCheck size={15} />重新组卷
          </button>
        </div>
        <div className="question-list">
          {ordered.map((record) => (
            <button key={record.question_number} className={`question-row ${selected === record.question_number ? 'selected' : ''}`} onClick={() => setSelected(record.question_number)}>
              <span className="question-number">{record.question_number}</span>
              <div><StatusPill status={record.status} /><small>{record.error || (record.editable_path ? '可编辑' : '等待产物')}</small></div>
            </button>
          ))}
          {!ordered.length && <div className="empty-state">题目规划后会在这里逐题出现</div>}
        </div>
      </div>
      <div className="question-editor-panel">
        {current ? (
          <>
            <div className="panel-toolbar editor-toolbar">
              <div><strong>第 {current.question_number} 题</strong><span>{current.plan_id || '尚无计划 ID'}</span></div>
              <div className="segmented" aria-label="题目视图">
                <button className={mode === 'preview' ? 'active' : ''} onClick={() => setMode('preview')}><Eye size={15} />预览</button>
                <button className={mode === 'json' ? 'active' : ''} onClick={() => setMode('json')}><Braces size={15} />JSON</button>
              </div>
            </div>
            <div className="editor-body">
              {questionQuery.isLoading && <div className="empty-state">正在加载题目…</div>}
              {questionQuery.error && <div className="empty-state error-text">{questionQuery.error.message}</div>}
              {questionQuery.data && mode === 'preview' && <QuestionPreview bundle={questionQuery.data.bundle} />}
              {questionQuery.data && mode === 'json' && <textarea className="json-editor" spellCheck={false} value={draft} onChange={(e) => setDraft(e.target.value)} />}
              {!questionQuery.isLoading && !questionQuery.data && !questionQuery.error && <div className="empty-state">该题尚未产生 editable Bundle</div>}
            </div>
            {!!versions.length && (
              <div className="version-strip">
                <label>
                  <span>题目版本</span>
                  <select value={selectedChild} onChange={(event) => setSelectedChild(event.target.value)}>
                    {versions.map((version) => <option key={version.run_id} value={version.run_id}>{version.current ? '当前' : '历史'} · {version.run_id.slice(0, 8)} · {version.status || 'unknown'}</option>)}
                  </select>
                </label>
                <button className="button secondary compact" disabled={!selectedChild || selectedChild === current.run_id || publishMutation.isPending} onClick={() => publishMutation.mutate()}><Upload size={14} />发布所选版本</button>
              </div>
            )}
            <div className="editor-footer">
              <textarea rows={2} placeholder="可选：给单题重跑的命题反馈" value={feedback} onChange={(e) => setFeedback(e.target.value)} />
              <div className="footer-actions">
                {(localError || rerunMutation.error || publishMutation.error || assemblyMutation.error) && <span className="error-text">{localError || rerunMutation.error?.message || publishMutation.error?.message || assemblyMutation.error?.message}</span>}
                <button className="button secondary" disabled={!questionQuery.data || saveMutation.isPending} onClick={() => saveMutation.mutate()}><Save size={15} />保存编辑</button>
                <button className="button primary" disabled={rerunMutation.isPending} onClick={() => rerunMutation.mutate()}><Play size={15} />单题重跑</button>
              </div>
            </div>
          </>
        ) : <div className="empty-state">选择一道题查看详情</div>}
      </div>
    </div>
  )
}
