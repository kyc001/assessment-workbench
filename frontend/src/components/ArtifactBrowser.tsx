import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Download, FileCode2 } from 'lucide-react'
import { api } from '../api'
import type { ArtifactRef } from '../types'

function formatBytes(value: number) {
  if (value < 1024) return `${value} B`
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`
  return `${(value / 1024 / 1024).toFixed(1)} MB`
}

export default function ArtifactBrowser({ artifacts }: { artifacts: ArtifactRef[] }) {
  const [selected, setSelected] = useState<string | null>(null)
  const content = useQuery({
    queryKey: ['artifact', selected],
    queryFn: () => api.artifact(selected!),
    enabled: !!selected,
  })
  return (
    <div className="artifact-browser">
      <div className="artifact-table-wrap">
        <table className="data-table">
          <thead><tr><th>Artifact</th><th>版本</th><th>类型</th><th>大小</th><th></th></tr></thead>
          <tbody>
            {artifacts.map((artifact) => (
              <tr key={artifact.id} className={selected === artifact.id ? 'selected' : ''}>
                <td><button className="link-button" onClick={() => setSelected(artifact.id)}>{artifact.logical_name}</button><small>{artifact.created_by_phase || '—'}</small></td>
                <td>v{artifact.version}</td><td>{artifact.media_type}</td><td>{formatBytes(artifact.size_bytes)}</td>
                <td><a className="icon-button" title="下载 Artifact" href={api.artifactUrl(artifact.id)} download><Download size={15} /></a></td>
              </tr>
            ))}
          </tbody>
        </table>
        {!artifacts.length && <div className="empty-state">当前运行没有 Artifact</div>}
      </div>
      <aside className="artifact-preview">
        {selected ? (
          content.isLoading ? <div className="empty-state">加载中…</div> : content.error ? (
            <div className="empty-state error-text">{content.error.message}</div>
          ) : content.data?.kind === 'binary' ? (
            <div className="empty-state"><FileCode2 size={24} />二进制产物请下载查看</div>
          ) : (
            <pre>{typeof content.data?.content === 'string' ? content.data.content : JSON.stringify(content.data?.content, null, 2)}</pre>
          )
        ) : <div className="empty-state">选择 Artifact 查看内容</div>}
      </aside>
    </div>
  )
}
