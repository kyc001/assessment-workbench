import { AlertTriangle, Check, Clock3, LoaderCircle, X } from 'lucide-react'
import type { PhaseEvent } from '../types'

function eventIcon(event: PhaseEvent) {
  if (event.status === 'running') return <LoaderCircle className="spin" size={15} />
  if (event.status === 'failed') return <X size={15} />
  if (event.warnings.length) return <AlertTriangle size={15} />
  return <Check size={15} />
}

function formatTime(value?: string | null) {
  if (!value) return ''
  return new Date(value).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
}

export default function Timeline({ events }: { events: PhaseEvent[] }) {
  if (!events.length) return <div className="empty-state">尚无阶段事件</div>
  return (
    <div className="timeline">
      {events.map((event) => (
        <div className={`timeline-row event-${event.status}`} key={event.id}>
          <div className="timeline-marker">{eventIcon(event)}</div>
          <div className="timeline-content">
            <div className="timeline-title-row">
              <strong>{event.phase}</strong>
              <span className="round-label">第 {event.round} 轮</span>
              <time><Clock3 size={12} /> {formatTime(event.completed_at || event.started_at)}</time>
            </div>
            {event.summary && <p>{event.summary}</p>}
            {event.error && <p className="error-text">{event.error}</p>}
            {!!event.warnings.length && <p className="warning-text">{event.warnings.join('；')}</p>}
          </div>
        </div>
      ))}
    </div>
  )
}
