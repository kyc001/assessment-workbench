import { useMemo, useState } from 'react'
import { Download, FileWarning } from 'lucide-react'
import { api } from '../api'
import type { DocumentRecord } from '../types'
import StatusPill from './StatusPill'

const LABELS = { questions: '题目卷', solutions: '答案卷', rubric: 'Rubric' }

export default function DocumentViewer({ documents }: { documents: DocumentRecord[] }) {
  const [selected, setSelected] = useState<DocumentRecord['view']>('questions')
  const byView = useMemo(() => new Map(documents.map((item) => [item.view, item])), [documents])
  const current = byView.get(selected)
  return (
    <div className="document-viewer">
      <div className="document-tabs">
        {(Object.keys(LABELS) as DocumentRecord['view'][]).map((view) => {
          const document = byView.get(view)
          return (
            <button key={view} className={selected === view ? 'active' : ''} onClick={() => setSelected(view)}>
              <span>{LABELS[view]}</span>
              {document ? <StatusPill status={document.status} /> : <small>未开始</small>}
            </button>
          )
        })}
      </div>
      <div className="document-stage">
        {current?.pdf_artifact_id ? (
          <>
            <div className="document-commandbar">
              <span>{LABELS[selected]} · {current.page_artifact_ids.length} 页</span>
              <a className="button secondary compact" href={api.artifactUrl(current.pdf_artifact_id)} download><Download size={15} />下载 PDF</a>
            </div>
            <iframe className="pdf-frame" title={LABELS[selected]} src={api.artifactUrl(current.pdf_artifact_id)} />
          </>
        ) : current?.error ? (
          <div className="empty-state error-state"><FileWarning size={24} /><strong>文档构建失败</strong><p>{current.error}</p>{current.log_artifact_id && <a href={api.artifactUrl(current.log_artifact_id)}>查看编译日志</a>}</div>
        ) : (
          <div className="empty-state">该视图尚未生成 PDF</div>
        )}
      </div>
    </div>
  )
}
