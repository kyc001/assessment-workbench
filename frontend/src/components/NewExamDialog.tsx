import { useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { FilePlus2, X } from 'lucide-react'
import { api } from '../api'

interface Props {
  open: boolean
  onClose: () => void
  onCreated: (runId: string) => void
}

export default function NewExamDialog({ open, onClose, onCreated }: Props) {
  const [subject, setSubject] = useState('高等数学')
  const [targetLevel, setTargetLevel] = useState('本科一年级')
  const [requirements, setRequirements] = useState('生成一份结构完整、可审计的期末模拟试卷')
  const [sourceContext, setSourceContext] = useState('')
  const [humanGates, setHumanGates] = useState(true)
  const [compilePdf, setCompilePdf] = useState(true)

  const mutation = useMutation({
    mutationFn: api.createExam,
    onSuccess: ({ run }) => {
      onCreated(run.id)
      onClose()
    },
  })

  if (!open) return null
  return (
    <div className="dialog-backdrop" role="presentation" onMouseDown={onClose}>
      <section className="dialog" role="dialog" aria-modal="true" onMouseDown={(e) => e.stopPropagation()}>
        <header className="dialog-header">
          <div>
            <span className="eyebrow">新建运行</span>
            <h2>生成试卷</h2>
          </div>
          <button className="icon-button" title="关闭" onClick={onClose}><X size={18} /></button>
        </header>
        <form
          className="form-grid"
          onSubmit={(event) => {
            event.preventDefault()
            mutation.mutate({
              subject,
              target_level: targetLevel,
              requirements,
              source_context: sourceContext,
              human_gates: humanGates,
              compile_pdf: compilePdf,
            })
          }}
        >
          <label>科目<input value={subject} onChange={(e) => setSubject(e.target.value)} required /></label>
          <label>目标层级<input value={targetLevel} onChange={(e) => setTargetLevel(e.target.value)} required /></label>
          <label className="span-2">试卷要求<textarea rows={4} value={requirements} onChange={(e) => setRequirements(e.target.value)} required /></label>
          <label className="span-2">可选材料上下文<textarea rows={4} value={sourceContext} onChange={(e) => setSourceContext(e.target.value)} /></label>
          <label className="check-row"><input type="checkbox" checked={humanGates} onChange={(e) => setHumanGates(e.target.checked)} />启用人工确认节点</label>
          <label className="check-row"><input type="checkbox" checked={compilePdf} onChange={(e) => setCompilePdf(e.target.checked)} />生成并检查 PDF</label>
          {mutation.error && <div className="form-error span-2">{mutation.error.message}</div>}
          <footer className="dialog-actions span-2">
            <button type="button" className="button secondary" onClick={onClose}>取消</button>
            <button className="button primary" disabled={mutation.isPending}>
              <FilePlus2 size={16} />{mutation.isPending ? '正在创建…' : '创建运行'}
            </button>
          </footer>
        </form>
      </section>
    </div>
  )
}
