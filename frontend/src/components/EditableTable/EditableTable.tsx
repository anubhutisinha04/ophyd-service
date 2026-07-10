import { useEffect, useRef, useState } from 'react'
import { CaretDown, CaretRight, Plus, Trash } from '@phosphor-icons/react'

export interface ColumnDef {
  key: string
  label: string
  type: 'text' | 'number'
}

type RowData = Record<string, unknown>
type RowValues = Record<string, string>

interface DraftRow {
  localId: string
  values: RowValues
  isNew: boolean
}

export interface EditableTableProps<T extends { edge_index: string }> {
  title: string
  columns: ColumnDef[]
  rows: T[] | undefined
  isLoading?: boolean
  loadError?: Error | null
  onCreate: (entry: T) => Promise<unknown>
  onUpdate: (edgeIndex: string, patch: Partial<T>) => Promise<unknown>
  onDelete: (edgeIndex: string) => Promise<unknown>
}

const PK = 'edge_index'

export function EditableTable<T extends { edge_index: string }>({
  title,
  columns,
  rows,
  isLoading,
  loadError,
  onCreate,
  onUpdate,
  onDelete,
}: EditableTableProps<T>) {
  const [draft, setDraft] = useState<DraftRow[]>([])
  const [deletedKeys, setDeletedKeys] = useState<string[]>([])
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)
  const [collapsed, setCollapsed] = useState(true)
  const [confirming, setConfirming] = useState(false)
  const [successSummary, setSuccessSummary] = useState('')
  const [successDetails, setSuccessDetails] = useState<
    { key: string; changes: { label: string; from: string; to: string }[] }[]
  >([])
  const originalRef = useRef<Map<string, RowValues>>(new Map())
  const newCounter = useRef(0)

  function toValues(row: T): RowValues {
    const values: RowValues = {}
    const record = row as RowData
    for (const col of columns) {
      const v = record[col.key]
      values[col.key] = v === null || v === undefined ? '' : String(v)
    }
    return values
  }

  // Re-sync draft from server data. Only runs when the fetched rows change,
  // which after a save happens once the invalidated query refetches.
  useEffect(() => {
    if (!rows) return
    const orig = new Map<string, RowValues>()
    const next = rows.map((row) => {
      const values = toValues(row)
      orig.set(values[PK], values)
      return { localId: values[PK], values, isNew: false }
    })
    originalRef.current = orig
    setDraft(next)
    setDeletedKeys([])
    setError('')
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rows])

  function isRowDirty(row: DraftRow): boolean {
    if (row.isNew) return true
    const orig = originalRef.current.get(row.localId)
    if (!orig) return true
    return columns.some((c) => row.values[c.key] !== orig[c.key])
  }

  const hasPending = deletedKeys.length > 0 || draft.some(isRowDirty)

  interface FieldChange {
    label: string
    from: string
    to: string
  }

  function fieldChanges(row: DraftRow): FieldChange[] {
    const orig = originalRef.current.get(row.localId)
    const changes: FieldChange[] = []
    for (const col of columns) {
      if (col.key === PK) continue
      const to = row.values[col.key] ?? ''
      const from = orig?.[col.key] ?? ''
      if (to !== from) {
        changes.push({
          label: col.label,
          from: from === '' ? '—' : from,
          to: to === '' ? '—' : to,
        })
      }
    }
    return changes
  }

  function pendingChanges() {
    const created = draft.filter((r) => r.isNew)
    const updated = draft.filter((r) => !r.isNew && isRowDirty(r))
    return { created, updated, deleted: deletedKeys }
  }

  function summarize(parts: { label: string; count: number }[]) {
    const items = parts.filter((p) => p.count > 0)
    if (items.length === 0) return 'No changes'
    return items.map((p) => `${p.count} ${p.label}`).join(', ')
  }

  function setCell(localId: string, key: string, value: string) {
    setSuccessSummary('')
    setSuccessDetails([])
    setDraft((d) =>
      d.map((r) =>
        r.localId === localId ? { ...r, values: { ...r.values, [key]: value } } : r,
      ),
    )
  }

  function addRow() {
    setSuccessSummary('')
    setSuccessDetails([])
    const values: RowValues = {}
    for (const col of columns) values[col.key] = ''
    const localId = `__new_${newCounter.current++}`
    setDraft((d) => [...d, { localId, values, isNew: true }])
  }

  function deleteRow(row: DraftRow) {
    setSuccessSummary('')
    setSuccessDetails([])
    if (!row.isNew) setDeletedKeys((k) => [...k, row.localId])
    setDraft((d) => d.filter((r) => r.localId !== row.localId))
  }

  function toEntry(values: RowValues): RowData {
    const entry: RowData = {}
    for (const col of columns) {
      const raw = values[col.key]
      entry[col.key] = col.type === 'number' ? Number(raw) : raw
    }
    return entry
  }

  async function saveAll() {
    const { created, updated, deleted } = pendingChanges()
    // Capture per-field diffs before the save resets the draft.
    const updateDetails = updated.map((row) => ({
      key: row.localId,
      changes: fieldChanges(row),
    }))
    setConfirming(false)
    setSaving(true)
    setError('')
    try {
      for (const key of deleted) {
        await onDelete(key)
      }
      for (const row of draft) {
        if (row.isNew) {
          await onCreate(toEntry(row.values) as unknown as T)
        } else if (isRowDirty(row)) {
          const full = toEntry(row.values)
          const patch: RowData = {}
          for (const col of columns) {
            if (col.key !== PK) patch[col.key] = full[col.key]
          }
          await onUpdate(row.localId, patch as unknown as Partial<T>)
        }
      }
      setDeletedKeys([])
      setSuccessSummary(
        `Saved: ${summarize([
          { label: 'added', count: created.length },
          { label: 'updated', count: updated.length },
          { label: 'deleted', count: deleted.length },
        ])}.`,
      )
      setSuccessDetails(updateDetails.filter((u) => u.changes.length > 0))
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to save changes.')
    } finally {
      setSaving(false)
    }
  }

  return (
    <section className="flex flex-col w-full min-w-0 bg-white border border-panel-border rounded-xl shadow-[0_4px_14px_rgba(16,92,120,0.06)] overflow-hidden">
      <header className="flex items-center justify-between gap-4 px-[1.1rem] py-[0.85rem] bg-brand-teal">
        <button
          type="button"
          className="inline-flex items-center gap-2 p-0 bg-transparent border-none text-white cursor-pointer"
          onClick={() => setCollapsed((c) => !c)}
          aria-expanded={!collapsed}
          aria-label={collapsed ? `Expand ${title}` : `Collapse ${title}`}
        >
          {collapsed ? <CaretRight size={18} weight="bold" /> : <CaretDown size={18} weight="bold" />}
          <h3 className="m-0 text-white text-[1.05rem] font-semibold">{title}</h3>
        </button>
        <div className="flex gap-2">
          <button
            type="button"
            className="inline-flex items-center gap-[0.35rem] px-3 py-[0.4rem] rounded-lg text-[0.85rem] font-semibold cursor-pointer border border-transparent transition-all disabled:opacity-50 disabled:cursor-not-allowed bg-white/[0.12] text-white border-white/40 hover:bg-white/[0.22]"
            onClick={addRow}
            disabled={saving}
          >
            <Plus size={16} weight="bold" />
            Add Row
          </button>
          <button
            type="button"
            className="inline-flex items-center gap-[0.35rem] px-3 py-[0.4rem] rounded-lg text-[0.85rem] font-semibold cursor-pointer border border-transparent transition-all disabled:opacity-50 disabled:cursor-not-allowed bg-brand-cyan text-white hover:bg-[#0b94bd]"
            onClick={() => {
              setSuccessSummary('')
              setSuccessDetails([])
              setConfirming(true)
            }}
            disabled={saving || !hasPending}
          >
            {saving ? 'Saving…' : 'Save All'}
          </button>
        </div>
      </header>

      {confirming && (() => {
        const { created, updated, deleted } = pendingChanges()
        return (
          <div className="mt-3 mx-[1.1rem] bg-[#fffaeb] border border-[#fec84b] rounded-lg p-3 flex flex-wrap items-center justify-between gap-3" role="dialog" aria-label="Confirm save">
            <div className="w-full">
              <strong className="text-[#93370d] text-[0.9rem]">Save changes to {title}?</strong>
              <ul className="mt-[0.35rem] pl-[1.1rem] text-[#7a4f0a] text-[0.85rem]">
                {created.length > 0 && <li>{created.length} row(s) added</li>}
                {deleted.length > 0 && <li>{deleted.length} row(s) deleted</li>}
              </ul>
              {updated.length > 0 && (
                <div className="mt-2 flex flex-col gap-[0.4rem] w-full">
                  {updated.map((row) => (
                    <div key={row.localId} className="bg-white/55 border border-black/[0.08] rounded-md px-[0.55rem] py-[0.35rem]">
                      <span className="font-semibold text-[0.82rem] text-[#344054]">{row.localId}</span>
                      <ul className="mt-[0.2rem] pl-4 text-[0.82rem] text-[#475467]">
                        {fieldChanges(row).map((c) => (
                          <li key={c.label}>
                            <span className="font-semibold text-[#344054]">{c.label}:</span>{' '}
                            <span className="text-[#b42318] line-through">{c.from}</span>
                            <span className="text-[#667085]"> → </span>
                            <span className="text-[#027a48] font-semibold">{c.to}</span>
                          </li>
                        ))}
                      </ul>
                    </div>
                  ))}
                </div>
              )}
            </div>
            <div className="flex gap-2">
              <button
                type="button"
                className="inline-flex items-center gap-[0.35rem] px-3 py-[0.4rem] rounded-lg text-[0.85rem] font-semibold cursor-pointer border border-transparent transition-all disabled:opacity-50 disabled:cursor-not-allowed bg-black/[0.06] text-[#5c4708] border-black/20 hover:bg-black/[0.12]"
                onClick={() => setConfirming(false)}
              >
                Cancel
              </button>
              <button
                type="button"
                className="inline-flex items-center gap-[0.35rem] px-3 py-[0.4rem] rounded-lg text-[0.85rem] font-semibold cursor-pointer border border-transparent transition-all disabled:opacity-50 disabled:cursor-not-allowed bg-brand-cyan text-white hover:bg-[#0b94bd]"
                onClick={saveAll}
              >
                Confirm Save
              </button>
            </div>
          </div>
        )
      })()}

      {!collapsed && (
        <>
          {successSummary && (
            <div className="mt-3 mx-[1.1rem] text-[#027a48] bg-[#ecfdf3] border border-[#6ce9a6] rounded-lg px-3 py-2 text-[0.85rem]">
              <div>{successSummary}</div>
              {successDetails.length > 0 && (
                <div className="mt-2 flex flex-col gap-[0.4rem] w-full">
                  {successDetails.map((u) => (
                    <div key={u.key} className="bg-white/55 border border-black/[0.08] rounded-md px-[0.55rem] py-[0.35rem]">
                      <span className="font-semibold text-[0.82rem] text-[#344054]">{u.key}</span>
                      <ul className="mt-[0.2rem] pl-4 text-[0.82rem] text-[#475467]">
                        {u.changes.map((c) => (
                          <li key={c.label}>
                            <span className="font-semibold text-[#344054]">{c.label}:</span>{' '}
                            <span className="text-[#b42318] line-through">{c.from}</span>
                            <span className="text-[#667085]"> → </span>
                            <span className="text-[#027a48] font-semibold">{c.to}</span>
                          </li>
                        ))}
                      </ul>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
          {error && <div className="mt-3 mx-[1.1rem] text-[#b42318] bg-[#fef3f2] border border-[#fda29b] rounded-lg px-3 py-2 text-[0.85rem]">{error}</div>}
          {loadError && (
            <div className="mt-3 mx-[1.1rem] text-[#b42318] bg-[#fef3f2] border border-[#fda29b] rounded-lg px-3 py-2 text-[0.85rem]">
              Failed to load data: {loadError.message}
            </div>
          )}

          <div className="overflow-x-auto bg-white px-2 pt-2 pb-3">
        <table className="w-full min-w-max border-collapse text-[0.85rem]">
          <thead>
            <tr>
              {columns.map((col) => (
                <th
                  key={col.key}
                  className={`text-left py-2 px-[0.6rem] text-brand-teal font-semibold whitespace-nowrap border-b-2 border-[#e3e8ec] ${col.key === PK ? 'sticky left-0 z-[2] bg-white shadow-[1px_0_0_#e3e8ec]' : ''}`}
                >
                  {col.label}
                </th>
              ))}
              <th className="w-[2.5rem] text-left py-2 px-[0.6rem] text-brand-teal font-semibold whitespace-nowrap border-b-2 border-[#e3e8ec]" aria-label="Row actions" />
            </tr>
          </thead>
          <tbody>
            {isLoading && (
              <tr>
                <td className="py-5 text-center text-[#6b7785]" colSpan={columns.length + 1}>
                  Loading…
                </td>
              </tr>
            )}
            {!isLoading && draft.length === 0 && (
              <tr>
                <td className="py-5 text-center text-[#6b7785]" colSpan={columns.length + 1}>
                  No rows. Use “Add Row” to create one.
                </td>
              </tr>
            )}
            {draft.map((row) => (
              <tr key={row.localId} className={isRowDirty(row) ? '[&>td]:bg-brand-cyan/[0.06] [&>.sticky-cell]:bg-[#eaf7fb]' : ''}>
                {columns.map((col) => {
                  const readOnly = col.key === PK && !row.isNew
                  return (
                    <td
                      key={col.key}
                      className={`py-[0.3rem] px-[0.4rem] border-b border-[#eef2f4] ${col.key === PK ? 'sticky-cell sticky left-0 z-[1] bg-white shadow-[1px_0_0_#e3e8ec]' : ''}`}
                    >
                      <input
                        className="w-full min-w-[5.5rem] px-2 py-[0.35rem] bg-white border border-[#9fc8d8] rounded-md text-gray-800 text-[0.85rem] outline-none tabular-nums transition-all focus:border-brand-cyan focus:shadow-[0_0_0_2px_rgba(0,173,220,0.25)] read-only:bg-[#f1f5f7] read-only:text-[#6b7785] read-only:border-panel-border [&[type=number]]:[-moz-appearance:textfield] [&[type=number]::-webkit-outer-spin-button]:appearance-none [&[type=number]::-webkit-inner-spin-button]:appearance-none"
                        type={col.type === 'number' ? 'number' : 'text'}
                        value={row.values[col.key]}
                        readOnly={readOnly}
                        onChange={(e) => setCell(row.localId, col.key, e.target.value)}
                      />
                    </td>
                  )
                })}
                <td className="py-[0.3rem] px-[0.4rem] border-b border-[#eef2f4] text-center">
                  <button
                    type="button"
                    className="inline-flex items-center justify-center p-[0.35rem] bg-transparent border-none rounded-md text-[#b42318] cursor-pointer transition-colors hover:bg-[#fef3f2] disabled:opacity-40 disabled:cursor-not-allowed"
                    onClick={() => deleteRow(row)}
                    disabled={saving}
                    aria-label="Delete row"
                  >
                    <Trash size={16} />
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
          </div>
        </>
      )}
    </section>
  )
}
