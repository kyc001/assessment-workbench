import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle,
  Ban,
  Check,
  CircleStop,
  FileCheck2,
  FlaskConical,
  LayoutList,
  LoaderCircle,
  Play,
  RefreshCw,
  RotateCcw,
  Rows3,
  Wifi,
  WifiOff,
} from 'lucide-react'
import { api } from './api'
import type { RunSnapshot, RunStatus } from './types'
import ArtifactBrowser from './components/ArtifactBrowser'
import DocumentViewer from './components/DocumentViewer'
import NewExamDialog from './components/NewExamDialog'
import QuestionWorkspace from './components/QuestionWorkspace'
import RunSidebar from './components/RunSidebar'
import StatusPill from './components/StatusPill'
import Timeline from './components/Timeline'

type Tab = 'overview' | 'research' | 'questions' | 'review' | 'documents' | 'artifacts'

const TABS: Array<{ id: Tab; label: string; icon: typeof Rows3 }> = [
  { id: 'overview', label: '概览', icon: Rows3 },
  { id: 'research', label: '研究', icon: FlaskConical },
  { id: 'questions', label: '题目', icon: LayoutList },
  { id: 'review', label: '审核', icon: FileCheck2 },
  { id: 'documents', label: '文档', icon: Rows3 },
  { id: 'artifacts', label: 'Artifact', icon: Rows3 },
]

function shortId(value: string) {
  return value.slice(0, 8)
}

function formatDate(value: string) {
  return new Date(value).toLocaleString('zh-CN')
}

function useLiveSnapshot(runId?: string) {
  const queryClient = useQueryClient()
  const [live, setLive] = useState(false)
  const query = useQuery({
    queryKey: ['snapshot', runId],
    queryFn: () => api.snapshot(runId!),
    enabled: !!runId,
    refetchInterval: live ? false : 1800,
  })

  useEffect(() => {
    if (!runId) return
    let stopped = false
    let reconnectTimer: number | undefined
    let stream: EventSource | undefined
    const connect = () => {
      if (stopped) return
      stream = new EventSource(`/api/runs/${runId}/stream`)
      const onSnapshot = (event: MessageEvent<string>) => {
        queryClient.setQueryData(['snapshot', runId], JSON.parse(event.data) as RunSnapshot)
        setLive(true)
      }
      stream.addEventListener('snapshot', onSnapshot as EventListener)
      stream.onerror = () => {
        setLive(false)
        stream?.close()
        reconnectTimer = window.setTimeout(connect, 2500)
      }
    }
    connect()
    return () => {
      stopped = true
      stream?.close()
      if (reconnectTimer !== undefined) window.clearTimeout(reconnectTimer)
    }
  }, [queryClient, runId])

  return { ...query, live }
}

function ActionBar({ snapshot, onChanged }: { snapshot: RunSnapshot; onChanged: () => void }) {
  const run = snapshot.detail.run
  const [reason, setReason] = useState('')
  const mutation = useMutation({
    mutationFn: async (action: 'approve' | 'retry' | 'reject' | 'abort' | 'resume' | 'cancel') => {
      if (action === 'resume') return api.resume(run.id)
      if (action === 'cancel') return api.cancel(run.id)
      const result = await api.runAction(run.id, action, reason)
      if (action === 'approve' || action === 'retry') await api.resume(run.id)
      return result
    },
    onSuccess: onChanged,
  })
  const waiting = run.status === 'waiting_human'
  const resumable = run.status === 'interrupted'
  const cancellable = run.status === 'queued' || run.status === 'running'
  if (!waiting && !resumable && !cancellable) return null
  return (
    <div className="action-strip">
      {waiting && (
        <>
          <div className="review-prompt"><AlertTriangle size={16} /><span>{snapshot.detail.human_review?.prompt || '等待人工确认'}</span></div>
          <input value={reason} onChange={(e) => setReason(e.target.value)} placeholder="可选：记录决定理由" />
          <button className="button success" disabled={mutation.isPending} onClick={() => mutation.mutate('approve')}><Check size={15} />接受并继续</button>
          <button className="button secondary" disabled={mutation.isPending} onClick={() => mutation.mutate('retry')}><RotateCcw size={15} />重试</button>
          <button className="button danger ghost" disabled={mutation.isPending} onClick={() => mutation.mutate('reject')}><Ban size={15} />拒绝</button>
        </>
      )}
      {resumable && <button className="button primary" disabled={mutation.isPending} onClick={() => mutation.mutate('resume')}><Play size={15} />恢复运行</button>}
      {cancellable && <button className="button danger ghost" disabled={mutation.isPending} onClick={() => mutation.mutate('cancel')}><CircleStop size={15} />请求取消</button>}
      {mutation.error && <span className="error-text">{mutation.error.message}</span>}
    </div>
  )
}

function Overview({ snapshot }: { snapshot: RunSnapshot }) {
  const { run, children, events } = snapshot.detail
  return (
    <div className="overview-grid">
      <section className="overview-main panel-section">
        <div className="section-heading"><div><span className="eyebrow">阶段事件</span><h3>运行时间线</h3></div><span>{events.length} 条事件</span></div>
        <Timeline events={events} />
      </section>
      <aside className="overview-side">
        <section className="panel-section metric-section">
          <span className="eyebrow">运行信息</span>
          <dl className="detail-list">
            <div><dt>Workflow</dt><dd>{run.workflow}</dd></div>
            <div><dt>当前阶段</dt><dd>{run.current_phase || '—'}</dd></div>
            <div><dt>创建时间</dt><dd>{formatDate(run.created_at)}</dd></div>
            <div><dt>更新时间</dt><dd>{formatDate(run.updated_at)}</dd></div>
          </dl>
          {run.error && <div className="inline-error">{run.error}</div>}
        </section>
        <section className="panel-section child-section">
          <div className="section-heading"><div><span className="eyebrow">隔离执行</span><h3>子运行</h3></div><span>{children.length}</span></div>
          <div className="child-list">
            {children.slice(0, 24).map((child) => <div key={child.id}><StatusPill status={child.status} /><span>{child.workflow}</span><code>{shortId(child.id)}</code></div>)}
            {!children.length && <div className="empty-state compact">尚无子运行</div>}
          </div>
        </section>
      </aside>
    </div>
  )
}

function ResearchView({ snapshot }: { snapshot: RunSnapshot }) {
  const [selectedReport, setSelectedReport] = useState<string | null>(null)
  const report = useQuery({
    queryKey: ['artifact', selectedReport],
    queryFn: () => api.artifact(selectedReport!),
    enabled: !!selectedReport,
  })
  return (
    <div className="research-layout">
      <div className="research-grid">
        {snapshot.research.map((record) => (
          <section className="research-lane" key={`${record.research_role}-${record.attempt}`}>
            <header><span>{record.research_role.replaceAll('_', ' ')}</span><StatusPill status={record.status} /></header>
            <dl className="detail-list"><div><dt>Attempt</dt><dd>{record.attempt}</dd></div><div><dt>Child run</dt><dd><code>{shortId(record.run_id)}</code></dd></div><div><dt>Report</dt><dd>{record.report_artifact_id ? shortId(record.report_artifact_id) : '—'}</dd></div></dl>
            {record.report_artifact_id && <button className="button secondary compact" onClick={() => setSelectedReport(record.report_artifact_id || null)}>查看研究报告</button>}
            {record.error && <p className="error-text">{record.error}</p>}
          </section>
        ))}
        {!snapshot.research.length && <div className="empty-state full-span">固定能力包或研究尚未开始</div>}
      </div>
      {selectedReport && (
        <aside className="research-report panel-section">
          <div className="section-heading"><div><span className="eyebrow">即时产物</span><h3>研究报告</h3></div><button className="link-button" onClick={() => setSelectedReport(null)}>关闭</button></div>
          {report.isLoading ? <div className="empty-state">正在加载报告</div> : report.error ? <div className="empty-state error-text">{report.error.message}</div> : <pre>{typeof report.data?.content === 'string' ? report.data.content : JSON.stringify(report.data?.content, null, 2)}</pre>}
        </aside>
      )}
    </div>
  )
}

function ReviewView({ snapshot }: { snapshot: RunSnapshot }) {
  const events = snapshot.detail.events.filter((event) => /REVIEW|ARBITRAT/.test(event.phase))
  const artifacts = snapshot.detail.artifacts.filter((artifact) => /review|arbitrat/i.test(artifact.logical_name))
  return (
    <div className="review-layout">
      <section className="panel-section"><div className="section-heading"><div><span className="eyebrow">决策链</span><h3>审核与仲裁</h3></div></div><Timeline events={events} /></section>
      <section className="panel-section"><div className="section-heading"><div><span className="eyebrow">证据</span><h3>审核 Artifact</h3></div></div><ArtifactBrowser artifacts={artifacts} /></section>
    </div>
  )
}

export default function App() {
  const queryClient = useQueryClient()
  const [selected, setSelected] = useState<string | undefined>(() => window.location.hash.replace('#run=', '') || undefined)
  const [tab, setTab] = useState<Tab>('overview')
  const [newExamOpen, setNewExamOpen] = useState(false)
  const [notice, setNotice] = useState('')
  const workspace = useQuery({ queryKey: ['workspace'], queryFn: api.workspace })
  const runs = useQuery({ queryKey: ['runs'], queryFn: api.runs, refetchInterval: 2500 })
  const snapshot = useLiveSnapshot(selected)

  useEffect(() => {
    const roots = runs.data?.filter((item) => !item.parent_run_id) || []
    if (!selected && roots.length) setSelected(roots[0].run.id)
  }, [runs.data, selected])

  useEffect(() => {
    if (selected) window.location.hash = `run=${selected}`
  }, [selected])

  useEffect(() => {
    if (!notice) return
    const timer = window.setTimeout(() => setNotice(''), 3200)
    return () => window.clearTimeout(timer)
  }, [notice])

  const refresh = () => {
    queryClient.invalidateQueries({ queryKey: ['runs'] })
    if (selected) queryClient.invalidateQueries({ queryKey: ['snapshot', selected] })
  }
  const selectedRun = snapshot.data?.detail.run
  const loadingError = runs.error || workspace.error || snapshot.error
  const completion = useMemo(() => {
    if (!snapshot.data?.questions.length) return { done: 0, total: 0 }
    return {
      done: snapshot.data.questions.filter((item) => item.status === 'succeeded').length,
      total: snapshot.data.questions.length,
    }
  }, [snapshot.data?.questions])

  return (
    <div className="app-shell">
      <RunSidebar runs={runs.data || []} selected={selected} workspace={workspace.data} loading={runs.isFetching} onSelect={(id) => { setSelected(id); setTab('overview') }} onNew={() => setNewExamOpen(true)} onRefresh={refresh} />
      <main className="workbench-main">
        {loadingError ? (
          <div className="main-empty error-text">
            <AlertTriangle size={30} />
            <strong>工作台数据加载失败</strong>
            <p>{loadingError.message}</p>
            <button className="button secondary" onClick={refresh}><RefreshCw size={16} />重试</button>
          </div>
        ) : selectedRun && snapshot.data ? (
          <>
            <header className="run-header">
              <div className="run-title-block">
                <div className="run-title-line"><StatusPill status={selectedRun.status} /><span className="run-workflow">{selectedRun.workflow}</span>{snapshot.live ? <span className="live-indicator"><Wifi size={14} />实时</span> : <span className="live-indicator offline"><WifiOff size={14} />轮询</span>}</div>
                <h1>{selectedRun.current_phase || '运行详情'}</h1>
                <p>{selectedRun.id}</p>
              </div>
              <div className="run-metrics">
                <div><span>题目进度</span><strong>{completion.done}/{completion.total || '—'}</strong></div>
                <div><span>研究角色</span><strong>{snapshot.data.research.length}</strong></div>
                <div><span>文档视图</span><strong>{snapshot.data.documents.filter((item) => item.status === 'succeeded').length}/3</strong></div>
                <button className="icon-button" title="刷新详情" onClick={refresh}><RefreshCw className={snapshot.isFetching ? 'spin' : ''} size={18} /></button>
              </div>
            </header>
            <ActionBar snapshot={snapshot.data} onChanged={refresh} />
            <nav className="tab-bar">
              {TABS.map(({ id, label, icon: Icon }) => <button key={id} className={tab === id ? 'active' : ''} onClick={() => setTab(id)}><Icon size={15} />{label}</button>)}
            </nav>
            <div className="tab-content">
              {tab === 'overview' && <Overview snapshot={snapshot.data} />}
              {tab === 'research' && <ResearchView snapshot={snapshot.data} />}
              {tab === 'questions' && <QuestionWorkspace runId={selectedRun.id} questions={snapshot.data.questions} onRunCreated={(id) => { setNotice(`已创建子运行 ${shortId(id)}`); refresh() }} />}
              {tab === 'review' && <ReviewView snapshot={snapshot.data} />}
              {tab === 'documents' && <DocumentViewer documents={snapshot.data.documents} />}
              {tab === 'artifacts' && <ArtifactBrowser artifacts={snapshot.data.detail.artifacts} />}
            </div>
          </>
        ) : snapshot.isLoading || runs.isLoading ? (
          <div className="main-empty"><LoaderCircle className="spin" size={28} /><strong>正在加载工作台</strong></div>
        ) : (
          <div className="main-empty"><LayoutList size={32} /><strong>还没有运行</strong><p>创建一份试卷，研究和逐题结果会立即显示在这里。</p><button className="button primary" onClick={() => setNewExamOpen(true)}>新建试卷</button></div>
        )}
      </main>
      {notice && <div className="toast">{notice}</div>}
      <NewExamDialog open={newExamOpen} onClose={() => setNewExamOpen(false)} onCreated={(runId) => { setSelected(runId); setNotice('整卷运行已创建'); refresh() }} />
    </div>
  )
}
