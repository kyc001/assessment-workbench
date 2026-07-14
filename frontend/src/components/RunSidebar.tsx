import { CirclePlus, Database, RefreshCw } from 'lucide-react'
import type { RunSummary, WorkspaceInfo } from '../types'
import StatusPill from './StatusPill'

interface Props {
  runs: RunSummary[]
  selected?: string
  workspace?: WorkspaceInfo
  loading: boolean
  onSelect: (id: string) => void
  onNew: () => void
  onRefresh: () => void
}

export default function RunSidebar({ runs, selected, workspace, loading, onSelect, onNew, onRefresh }: Props) {
  const roots = runs.filter((item) => !item.parent_run_id)
  return (
    <aside className="run-sidebar">
      <div className="brand-block">
        <div className="brand-mark">AW</div>
        <div><strong>Assessment Workbench</strong><span>本地评测工作台</span></div>
      </div>
      <div className="sidebar-actions">
        <button className="button primary grow" onClick={onNew}><CirclePlus size={16} />新建试卷</button>
        <button className="icon-button" title="刷新运行" onClick={onRefresh}><RefreshCw className={loading ? 'spin' : ''} size={17} /></button>
      </div>
      <div className="run-list" aria-label="运行列表">
        {roots.map(({ run, child_count }) => (
          <button key={run.id} className={`run-item ${selected === run.id ? 'selected' : ''}`} onClick={() => onSelect(run.id)}>
            <div className="run-item-top"><StatusPill status={run.status} /><time>{new Date(run.updated_at).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })}</time></div>
            <strong>{run.current_phase || run.workflow}</strong>
            <span className="run-id">{run.id.slice(0, 8)} · {child_count} 个子运行</span>
          </button>
        ))}
        {!roots.length && <div className="empty-state compact">暂无运行</div>}
      </div>
      <div className="workspace-footer">
        <Database size={14} />
        <div><span>Workspace</span><strong title={workspace?.root}>{workspace?.root || '加载中…'}</strong></div>
      </div>
    </aside>
  )
}
