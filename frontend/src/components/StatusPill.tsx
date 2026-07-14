import type { RunStatus } from '../types'

const LABELS: Record<string, string> = {
  queued: '排队',
  running: '运行中',
  waiting_human: '等待确认',
  cancelling: '取消中',
  succeeded: '成功',
  failed: '失败',
  cancelled: '已取消',
  interrupted: '可恢复',
}

export default function StatusPill({ status }: { status: RunStatus | string }) {
  return <span className={`status-pill status-${status}`}>{LABELS[status] || status}</span>
}
