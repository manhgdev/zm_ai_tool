import { useEffect, useLayoutEffect, useRef, useState, type FormEvent, type ReactNode } from 'react'
import { loadLocale, localize } from '../app/i18n'
import { accessText, deviceTypeText, useWebUser, WebFrame, webErrorText, type Access } from '../app/WebSession'
import { IconClock, IconKey, IconSearch, IconUser } from '../shared/components/Icons'
import { copyText } from '../shared/lib/clipboard'
import { toast } from 'sonner'

type Term = { mode: string; start: string; amount?: number; unit?: string; startsAt?: string; expiresAt?: string }
type Item = { linked?: boolean; id: string; name: string; email?: string; role?: string; issuer_id: string | null; notes: string; enabled: number; deleted: number; access: Access; term: Term | null; key?: string | null; key_masked?: string; max_devices?: number; max_accounts?: number; max_keys?: number; max_days?: number; allow_lifetime?: number }
type Device = { id: string; name: string; installation_id: string; device_type: string | null; enabled: number; created_at: number; last_seen: number }
type Audit = { id: number; actor_id: string; actor_email?: string; action: string; kind: string; target_id: string; created_at: number; changes: string }
type Form = { name: string; email: string; role: string; issuer_id: string; notes: string; enabled: boolean; max_accounts: number; max_keys: number; max_days: number; allow_lifetime: boolean; mode: string; start: string; amount: number; unit: string; begins: string; ends: string }

function AdminDialog({ title, busy, onClose, children }: { title: string; busy: boolean; onClose?: () => void; children: ReactNode }) {
  const ref = useRef<HTMLDialogElement>(null)
  useLayoutEffect(() => {
    const previous = document.activeElement as HTMLElement | null
    const element = ref.current!
    element.showModal()
    return () => { element.close(); previous?.focus() }
  }, [])
  return <dialog ref={ref} onCancel={event => { event.preventDefault(); if (!busy) onClose?.() }} aria-labelledby={title} className="admin-dialog">{onClose && <button className="admin-dialog-close" type="button" disabled={busy} onClick={onClose} aria-label={localize(loadLocale(), 'Đóng', 'Close')}>×</button>}{children}</dialog>
}

export default function AdminPage() {
  const user = useWebUser()!
  const [locale, setLocale] = useState(loadLocale)
  const t = (vi: string, en: string) => localize(locale, vi, en)
  const kind = window.location.pathname.replace(/\/$/, '').endsWith('/keys') ? 'keys' : 'accounts'
  const [items, setItems] = useState<Item[]>([])
  const [issuers, setIssuers] = useState<{ id: string; name: string; email: string }[]>([])
  const [creators, setCreators] = useState<{ id: string; name: string; email: string }[]>([])
  const [creator, setCreator] = useState('')
  const [termMode, setTermMode] = useState('')
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [search, setSearch] = useState('')
  const [query, setQuery] = useState('')
  const [state, setState] = useState('')
  const [role, setRole] = useState('')
  const [issuer, setIssuer] = useState('')
  const [refresh, setRefresh] = useState(0)
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState(false)
  const [copyingKeyId, setCopyingKeyId] = useState('')
  const [dialog, setDialog] = useState<{ item: Item | null; renew: boolean } | null>(null)
  const [form, setForm] = useState<Form | null>(null)
  const [newKey, setNewKey] = useState('')
  const [copyingNewKey, setCopyingNewKey] = useState(false)
  const [devices, setDevices] = useState<{ item: Item; rows: Device[] } | null>(null)
  const [history, setHistory] = useState<Audit[] | null>(null)
  const [confirmation, setConfirmation] = useState<{ label: string; run: () => Promise<unknown> } | null>(null)
  const roleName = (value: string) => ({ owner: t('Chủ', 'Owner'), admin: 'Admin', reseller: t('Đại lý', 'Reseller'), user: t('Người dùng', 'User') })[value] || value
  const actionName = (value: string) => ({ update: t('Tạo / sửa', 'Create / edit'), renew: t('Gia hạn', 'Renew'), delete: t('Xóa', 'Delete'), restore: t('Khôi phục', 'Restore'), rotate: t('Tạo lại key', 'Regenerate key'), 'revoke-sessions': t('Thu hồi phiên', 'Revoke sessions'), 'revoke-device': t('Thu hồi thiết bị', 'Revoke device') })[value] || value
  const fieldName = (field: string) => ({ name: t('Tên', 'Name'), email: t('Email Google', 'Google email'), role: t('Vai trò', 'Role'), enabled: t('Hoạt động', 'Enabled'), deleted: t('Đã xóa', 'Deleted'), issuer_id: t('Đại lý phụ trách', 'Assigned reseller'), notes: t('Ghi chú', 'Notes'), starts_at: t('Bắt đầu', 'Starts'), expires_at: t('Hết hạn', 'Expires'), lifetime: t('Vĩnh viễn', 'Lifetime'), max_accounts: t('Hạn mức tài khoản', 'Account allowance'), max_keys: t('Hạn mức key', 'Key allowance'), max_days: t('Thời hạn tối đa (ngày)', 'Maximum term (days)'), max_devices: t('Số thiết bị tối đa', 'Maximum devices'), allow_lifetime: t('Được cấp vĩnh viễn', 'Can grant lifetime'), device_id: t('Thiết bị', 'Device'), id: t('Mã định danh', 'Identifier'), created_at: t('Thời điểm tạo', 'Created'), term: t('Cách tính hạn', 'Term policy'), created_by: t('Người cấp', 'Issued by'), key_masked: t('Key đã che', 'Masked key') } as Record<string, string>)[field] || t('Thông tin khác', 'Other details')
  const formatChange = (field: string, value: unknown): string => {
    if (value == null) return '—'
    if (['enabled', 'deleted', 'lifetime', 'allow_lifetime'].includes(field)) return value ? t('Có', 'Yes') : t('Không', 'No')
    if (['starts_at', 'expires_at', 'created_at'].includes(field)) return date(Number(value))
    if (field === 'role') return roleName(String(value))
    if (field === 'term') {
      const term = JSON.parse(String(value)) as Term
      return (term.mode === 'lifetime' ? t('Vĩnh viễn', 'Lifetime') : term.mode === 'until' ? t('Đến ngày giờ cụ thể', 'Until a date and time') : String(term.amount) + ' ' + (units[term.unit as keyof typeof units] || '')) + ' · ' + (term.start === 'first_activation' ? t('Lần kích hoạt đầu tiên', 'First activation') : term.start === 'scheduled' ? t('Theo lịch', 'Scheduled') : t('Ngay bây giờ', 'Now'))
    }
    return String(value)
  }
  const date = (value: string | number | null) => value == null ? '—' : new Date(typeof value === 'number' ? value * 1000 : value).toLocaleString(locale === 'vi' ? 'vi-VN' : 'en-US', { timeZone: 'Asia/Ho_Chi_Minh' })
  async function request<T>(path: string, method = 'GET', body?: unknown): Promise<T> {
    const response = await fetch(path, { method, ...(body === undefined ? {} : { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }) })
    const data = await response.json()
    if (!response.ok) throw new Error(typeof data.code === 'string' ? data.code : typeof data.detail === 'string' ? data.detail : 'INVALID_FIELDS')
    return data as T
  }
  function reportError(error: unknown) {
    toast.error(webErrorText(error instanceof Error ? error.message : '', loadLocale()))
  }
  useEffect(() => {
    let active = true
    setLoading(true)
    const params = new URLSearchParams({ page: String(page), q: query, state, role, issuer_id: issuer, created_by: creator, term_mode: termMode })
    request<{ items: Item[]; total: number }>(`/api/admin/${kind}?${params}`).then(data => { if (active) { setItems(data.items); setTotal(data.total) } }).catch(e => { if (active) reportError(e) }).finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [kind, page, query, state, role, issuer, creator, termMode, refresh])
  useEffect(() => {
    if (user.role === 'reseller') return
    let active = true
    request<typeof creators>('/api/admin/issuers/options').then(data => { if (active) setCreators(data) }).catch(e => { if (active) reportError(e) })
    request<typeof issuers>('/api/admin/resellers/options').then(data => { if (active) setIssuers(data) }).catch(e => { if (active) reportError(e) })
    return () => { active = false }
  }, [user.role, refresh])
  async function perform(fn: () => Promise<unknown>, changed = true) {
    setBusy(true)
    try {
      await fn()
      if (changed) { setRefresh(x => x + 1); toast.success(t('Đã lưu thay đổi.', 'Changes saved.')); setConfirmation(null) }
    }
    catch (e) { reportError(e) }
    finally { setBusy(false) }
  }
  function edit(item: Item | null, renew = false) {
    setDialog({ item, renew })
    setForm({ name: item?.name || '', email: item?.email || '', role: item?.role || 'user', issuer_id: item?.issuer_id || '', notes: item?.notes || '', enabled: item ? Boolean(item.enabled) : true,
      max_accounts: item?.max_accounts || 0, max_keys: item?.max_keys || 0, max_days: item?.max_days || 0, allow_lifetime: Boolean(item?.allow_lifetime),
      mode: item && !renew ? 'unchanged' : 'duration', start: kind === 'keys' ? 'first_activation' : 'now', amount: 30, unit: 'days', begins: '', ends: '' })
  }
  async function submit(event: FormEvent) {
    event.preventDefault()
    if (!form || !dialog) return
    const values: Record<string, unknown> = dialog.renew ? { amount: form.amount, unit: form.unit } : {
      name: form.name, notes: form.notes, enabled: form.enabled,
      ...(kind === 'accounts' ? { email: form.email, role: form.role } : {}),
      ...(user.role !== 'reseller' ? { issuer_id: form.issuer_id || null } : {}),
      ...(kind === 'accounts' && form.role === 'reseller' && user.role !== 'reseller' ? { max_accounts: form.max_accounts, max_keys: form.max_keys, max_days: form.max_days, allow_lifetime: form.allow_lifetime } : {}),
    }
    if (!dialog.renew && form.mode !== 'unchanged') {
      const term: Term = { mode: form.mode, start: form.start }
      if (term.mode === 'duration') { term.amount = form.amount; term.unit = form.unit }
      if (term.start === 'scheduled' && !Number.isFinite(Date.parse(form.begins + '+07:00')) || term.mode === 'until' && !Number.isFinite(Date.parse(form.ends + '+07:00'))) { reportError(new Error('INVALID_TERM')); return }
      if (term.start === 'scheduled') term.startsAt = new Date(form.begins + '+07:00').toISOString()
      if (term.mode === 'until') term.expiresAt = new Date(form.ends + '+07:00').toISOString()
      values.term = term
    }
    const path = `/api/admin/${kind}${dialog.item ? '/' + dialog.item.id : ''}${dialog.renew ? '/renew' : ''}`
    await perform(async () => {
      const data = await request<Item & { key?: string }>(path, dialog.item && !dialog.renew ? 'PATCH' : 'POST', values)
      if (data.key) setNewKey(data.key)
      setDialog(null); setForm(null)
    })
  }
  function command(item: Item, action: string, label: string) {
    setConfirmation({ label, run: async () => {
      const data = await request<Item & { key?: string }>(`/api/admin/${kind}/${item.id}${action === 'delete' ? '' : '/' + action}`, action === 'delete' ? 'DELETE' : 'POST', action === 'delete' ? undefined : {})
      if (data.key) setNewKey(data.key)
    } })
  }
  async function copyNewKey() {
    setCopyingNewKey(true)
    try { await copyText(newKey, { successMessage: t('Đã sao chép key.', 'Key copied.'), errorMessage: webErrorText('KEY_COPY_FAILED', locale) }).catch(() => {}) }
    finally { setCopyingNewKey(false) }
  }
  async function copyListedKey(item: Item) {
    if (!item.key) { reportError(new Error('KEY_COPY_UNAVAILABLE')); return }
    if (copyingKeyId) return
    setCopyingKeyId(item.id)
    try { await copyText(item.key, { successMessage: t('Đã sao chép key.', 'Key copied.'), errorMessage: webErrorText('KEY_COPY_FAILED', locale) }).catch(() => {}) }
    finally { setCopyingKeyId('') }
  }
  const set = <K extends keyof Form>(key: K, value: Form[K]) => setForm(previous => previous ? { ...previous, [key]: value } : previous)
  const availableRoles = user.role === 'owner' ? ['owner', 'admin', 'reseller', 'user'] : user.role === 'admin' ? ['reseller', 'user'] : ['user']
  const units = { hours: t('Giờ', 'Hours'), days: t('Ngày', 'Days'), months: t('Tháng', 'Months'), years: t('Năm', 'Years') }
  return <WebFrame locale={locale} onLocale={setLocale}><main className="admin-page lp-container">
    <div className="admin-heading"><div><h1>{kind === 'accounts' ? t('Quản lý tài khoản', 'Account management') : t('Quản lý key', 'Key management')}</h1><span className={'admin-role is-' + user.role}>{roleName(user.role)}</span></div><button className="lp-button lp-button-primary" onClick={() => edit(null)}><span aria-hidden="true">＋</span>{kind === 'accounts' ? t('Thêm tài khoản', 'Add account') : t('Tạo key', 'Create key')}</button></div>
    <nav className="admin-tabs" aria-label={t('Quản trị', 'Administration')}><a aria-current={kind === 'accounts' ? 'page' : undefined} href="/admin/accounts"><IconUser />{t('Tài khoản', 'Accounts')}</a><a aria-current={kind === 'keys' ? 'page' : undefined} href="/admin/keys"><IconKey />{t('Key ứng dụng', 'Application keys')}</a><button onClick={() => void perform(async () => setHistory(await request<Audit[]>('/api/admin/audit/history')), false)}><IconClock />{t('Lịch sử quản lý', 'Management history')}</button></nav>
    {user.role === 'reseller' && <p className="admin-note">{t('Bạn chỉ quản lý tài khoản/key được giao trong hạn mức đã cấp.', 'You manage assigned accounts and keys within your allowance.')} {t('Tài khoản:', 'Accounts:')} {user.max_accounts || 0} · {t('Key:', 'Keys:')} {user.max_keys || 0} · {t('Tối đa (ngày):', 'Maximum (days):')} {user.max_days || 0} · {t('Cấp vĩnh viễn:', 'Lifetime grants:')} {user.allow_lifetime ? t('Có', 'Yes') : t('Không', 'No')}</p>}
    <section className="admin-panel" aria-label={kind === 'accounts' ? t('Danh sách tài khoản', 'Account list') : t('Danh sách key', 'Key list')}>
    <form className="admin-filters" onSubmit={e => { e.preventDefault(); setQuery(search); setPage(1) }}>
      <label className="admin-search">{t('Tìm kiếm', 'Search')}<span><IconSearch /><input value={search} onChange={e => setSearch(e.target.value)} placeholder={t('Tên, email hoặc key', 'Name, email or key')} /></span></label>
      <label>{t('Trạng thái', 'Status')}<select value={state} onChange={e => { setState(e.target.value); setPage(1) }}><option value="">{t('Tất cả', 'All')}</option>{['active', 'pending', 'unactivated', 'scheduled', 'expired', 'blocked', 'deleted'].map(value => <option key={value} value={value}>{accessText(value, locale)}</option>)}</select></label>
      {kind === 'accounts' && <label>{t('Vai trò', 'Role')}<select value={role} onChange={e => { setRole(e.target.value); setPage(1) }}><option value="">{t('Tất cả', 'All')}</option>{['owner', 'admin', 'reseller', 'user'].map(value => <option key={value} value={value}>{roleName(value)}</option>)}</select></label>}
      {user.role !== 'reseller' && <label>{t('Đại lý', 'Reseller')}<select value={issuer} onChange={e => { setIssuer(e.target.value); setPage(1) }}><option value="">{t('Tất cả', 'All')}</option>{issuers.map(item => <option key={item.id} value={item.id}>{item.name || item.email}</option>)}</select></label>}
      {kind === 'keys' && <><label>{t('Người cấp', 'Issued by')}<select value={creator} onChange={e => { setCreator(e.target.value); setPage(1) }}><option value="">{t('Tất cả', 'All')}</option>{creators.map(item => <option key={item.id} value={item.id}>{item.name || item.email}</option>)}</select></label><label>{t('Loại thời hạn', 'Term type')}<select value={termMode} onChange={e => { setTermMode(e.target.value); setPage(1) }}><option value="">{t('Tất cả', 'All')}</option><option value="lifetime">{t('Vĩnh viễn', 'Lifetime')}</option><option value="duration">{t('Theo thời lượng', 'Duration')}</option><option value="until">{t('Đến ngày giờ cụ thể', 'Until a date and time')}</option></select></label></>}
      <button type="submit">{t('Tìm', 'Search')}</button>
    </form>
    <div className="admin-table-wrap" aria-busy={loading}><table className="admin-table"><thead><tr><th>{kind === 'accounts' ? t('Tài khoản', 'Account') : t('Key ứng dụng', 'Application key')}</th>{kind === 'accounts' && <th>{t('Vai trò', 'Role')}</th>}<th>{t('Trạng thái', 'Status')}</th><th>{t('Thời hạn · Giờ Việt Nam', 'Term · Vietnam time')}</th><th>{t('Thao tác', 'Actions')}</th></tr></thead><tbody>
      {items.map(item => { const canEdit = user.role !== 'admin' || !['owner', 'admin'].includes(item.role || ''); return <tr key={item.id}>
        <td><div className="admin-identity"><span className="admin-avatar" aria-hidden="true">{kind === 'accounts' ? (item.name || item.email || '?').slice(0, 2).toUpperCase() : <IconKey size={18} />}</span><div><strong>{item.name || item.email || (kind === 'keys' ? t('Key ứng dụng', 'Application key') : '—')}</strong>{kind === 'keys' ? <code className="admin-key-value" role="button" tabIndex={0} aria-busy={copyingKeyId === item.id} title={t('Nháy đúp để sao chép key', 'Double-click to copy the key')} onDoubleClick={() => void copyListedKey(item)} onKeyDown={event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); void copyListedKey(item) } }}>{item.key || '—'}</code> : <small>{item.email}</small>}{item.issuer_id && <small>{t('Đại lý: ', 'Reseller: ')}{issuers.find(value => value.id === item.issuer_id)?.email || (user.role === 'reseller' ? user.email : '—')}</small>}</div></div></td>
        {kind === 'accounts' && <td className="admin-role-cell"><span className={'admin-role is-' + item.role}>{roleName(item.role || '')}</span></td>}<td className="admin-state-cell"><span className={'admin-status is-' + item.access.state}>{accessText(item.access.state, locale)}</span></td>
        <td className="admin-term-cell"><span className="admin-term">{item.access.lifetime ? t('Vĩnh viễn', 'Lifetime') : date(item.access.expiresAt)}</span><small>{t('Bắt đầu: ', 'Starts: ')}{date(item.access.startsAt)}</small>{item.term?.start === 'first_activation' && !item.access.startsAt && <small>{t('Tính từ kích hoạt đầu tiên', 'Starts on first activation')}</small>}</td>
        <td><div className="admin-row-actions">{canEdit && (item.deleted ? <button disabled={busy} onClick={() => command(item, 'restore', t('Khôi phục mục này?', 'Restore this item?'))}>{t('Khôi phục', 'Restore')}</button> : <>
            <button className="admin-edit" onClick={() => edit(item)}>{t('Sửa', 'Edit')}</button>
            <button onClick={() => edit(item, true)}>{t('Gia hạn', 'Renew')}</button>
            <button className={'admin-toggle ' + (item.enabled ? 'is-block' : 'is-unblock')} disabled={busy} onClick={() => void perform(() => request(`/api/admin/${kind}/${item.id}`, 'PATCH', { enabled: !item.enabled }))}>{item.enabled ? t('Khóa', 'Block') : t('Mở khóa', 'Unblock')}</button>
            {kind === 'accounts' ? <button disabled={busy} onClick={() => command(item, 'revoke-sessions', t('Thu hồi tất cả phiên đăng nhập của tài khoản này?', 'Revoke all sessions for this account?'))}>{t('Thu hồi phiên', 'Revoke sessions')}</button> : <>
              <button disabled={busy} onClick={() => void perform(async () => setDevices({ item, rows: await request<Device[]>(`/api/admin/keys/${item.id}/devices`) }), false)}>{t('Thiết bị', 'Devices')}</button>
              <button disabled={busy} onClick={() => command(item, 'rotate', t('Tạo lại key sẽ thu hồi key và thiết bị cũ. Tiếp tục?', 'Regenerating revokes the old key and devices. Continue?'))}>{t('Tạo lại key', 'Regenerate key')}</button>
            </>}
            <button className="admin-danger" disabled={busy} onClick={() => command(item, 'delete', t('Xóa và thu hồi quyền của mục này? Dữ liệu công việc được giữ lại.', 'Delete and revoke this item? Work data will be retained.'))}>{t('Xóa', 'Delete')}</button>
          </>)}</div></td></tr> })}
      {!items.length && <tr><td className="admin-empty" colSpan={kind === 'accounts' ? 5 : 4}><span className="admin-empty-icon" aria-hidden="true">{kind === 'accounts' ? <IconUser size={24} /> : <IconKey size={24} />}</span><strong>{loading ? t('Đang tải…', 'Loading…') : t('Chưa có dữ liệu phù hợp.', 'No matching items.')}</strong>{!loading && <p>{t('Thử thay đổi bộ lọc hoặc thêm một mục mới.', 'Try changing the filters or adding a new item.')}</p>}</td></tr>}
    </tbody></table></div>
    <div className="admin-pagination"><span className="admin-total">{total} {t('mục', 'items')}</span><button disabled={page === 1 || loading} onClick={() => setPage(page - 1)}>{t('Trước', 'Previous')}</button><span>{page} / {Math.max(1, Math.ceil(total / 25))}</span><button disabled={page * 25 >= total || loading} onClick={() => setPage(page + 1)}>{t('Sau', 'Next')}</button></div>
    </section>
    {dialog && form && <AdminDialog title="admin-form-title" busy={busy} onClose={() => { setDialog(null); setForm(null) }}><form onSubmit={e => void submit(e)}>
      <h2 id="admin-form-title">{dialog.renew ? t('Gia hạn', 'Renew') : dialog.item ? t('Sửa thông tin', 'Edit details') : kind === 'accounts' ? t('Thêm tài khoản', 'Add account') : t('Tạo key', 'Create key')}</h2>
      <div className="admin-form-grid">{!dialog.renew && <>
        <p className="admin-section-label">{kind === 'accounts' ? t('Thông tin tài khoản', 'Account details') : t('Thông tin key', 'Key details')}</p>
        <label>{t('Tên', 'Name')}<input value={form.name} maxLength={200} onChange={e => set('name', e.target.value)} /></label>
        {kind === 'accounts' ? <><label>{t('Email Google', 'Google email')}<input type="email" required value={form.email} disabled={Boolean(dialog.item && 'linked' in dialog.item && dialog.item.linked)} onChange={e => set('email', e.target.value)} /></label><label>{t('Vai trò', 'Role')}<select value={form.role} onChange={e => set('role', e.target.value)}>{availableRoles.map(value => <option key={value} value={value}>{roleName(value)}</option>)}</select></label></> : <p className="admin-note">{t('APP: 1 điện thoại, 1 máy tính bảng/iPad, 1 PC và 1 laptop. Máy mới thay máy cũ cùng loại.', 'APP: one phone, one tablet/iPad, one desktop PC and one laptop. A new machine replaces the previous one in its category.')}</p>}
        {user.role !== 'reseller' && <label>{t('Đại lý phụ trách', 'Assigned reseller')}<select value={form.issuer_id} onChange={e => set('issuer_id', e.target.value)}><option value="">{t('Quản lý trực tiếp', 'Direct management')}</option>{issuers.map(item => <option key={item.id} value={item.id}>{item.name} · {item.email}</option>)}</select></label>}
        <label className="admin-field-full">{t('Ghi chú', 'Notes')}<textarea value={form.notes} maxLength={4000} onChange={e => set('notes', e.target.value)} /></label>
        <label className="admin-check"><input type="checkbox" checked={form.enabled} onChange={e => set('enabled', e.target.checked)} />{t('Cho phép hoạt động', 'Enabled')}</label>
        {kind === 'accounts' && form.role === 'reseller' && user.role !== 'reseller' && <fieldset className="admin-quota"><legend>{t('Hạn mức đại lý', 'Reseller allowance')}</legend>{(['max_accounts', 'max_keys', 'max_days'] as const).map((key, index) => <label key={key}>{[t('Số tài khoản được cấp', 'Account slots'), t('Số key được cấp', 'Key slots'), t('Thời hạn tối đa (ngày)', 'Maximum term (days)')][index]}<input type="number" min={0} max={1000000} value={form[key]} required onChange={e => set(key, Number(e.target.value))} /></label>)}<label className="admin-check"><input type="checkbox" checked={form.allow_lifetime} onChange={e => set('allow_lifetime', e.target.checked)} />{t('Được cấp vĩnh viễn', 'Can grant lifetime access')}</label></fieldset>}
        <p className="admin-section-label">{t('Quyền sử dụng', 'Access')}</p>
        <label>{t('Thời hạn', 'Term')}<select value={form.mode} onChange={e => { set('mode', e.target.value); if (e.target.value === 'until' && form.start === 'first_activation') set('start', 'now') }}>{dialog.item && <option value="unchanged">{t('Giữ thời hạn hiện tại', 'Keep current term')}</option>}<option value="duration">{t('Theo thời lượng', 'Duration')}</option><option value="until">{t('Đến ngày giờ cụ thể', 'Until a date and time')}</option><option value="lifetime">{t('Vĩnh viễn', 'Lifetime')}</option></select></label>
        {form.mode !== 'unchanged' && <label>{t('Bắt đầu tính hạn', 'Term starts')}<select value={form.start} onChange={e => set('start', e.target.value)}><option value="now">{t('Ngay bây giờ', 'Now')}</option>{kind === 'keys' && form.mode !== 'until' && <option value="first_activation">{t('Lần kích hoạt đầu tiên', 'First activation')}</option>}<option value="scheduled">{t('Ngày giờ cụ thể', 'Scheduled date and time')}</option></select></label>}
        {form.mode !== 'unchanged' && form.start === 'scheduled' && <label>{t('Ngày giờ bắt đầu', 'Start date and time')}<input required type="datetime-local" value={form.begins} onChange={e => set('begins', e.target.value)} /></label>}
        {form.mode === 'until' && <label>{t('Ngày giờ hết hạn', 'Expiry date and time')}<input required type="datetime-local" value={form.ends} onChange={e => set('ends', e.target.value)} /></label>}
      </>}
      {(dialog.renew || form.mode === 'duration') && <><label>{t('Số lượng thời gian', 'Duration amount')}<input type="number" required min={1} max={10000} value={form.amount} onChange={e => set('amount', Number(e.target.value))} /></label><label>{t('Đơn vị', 'Unit')}<select value={form.unit} onChange={e => set('unit', e.target.value)}>{Object.entries(units).map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></label></>}
      <p className="admin-note admin-field-full">{t('Ngày giờ sử dụng múi giờ Việt Nam (UTC+7). Số lượng 0 trong hạn mức nghĩa là chưa được cấp.', 'Dates use Vietnam time (UTC+7). A quota of 0 means no allowance.')}</p></div>
      <div className="admin-dialog-actions"><button type="button" disabled={busy} onClick={() => { setDialog(null); setForm(null) }}>{t('Hủy', 'Cancel')}</button><button type="submit" disabled={busy}>{busy ? t('Đang lưu…', 'Saving…') : t('Lưu', 'Save')}</button></div>
    </form></AdminDialog>}
    {newKey && <AdminDialog title="admin-key-title" busy={busy}><h2 id="admin-key-title">{t('Key mới', 'New key')}</h2><p>{t('Sao chép và lưu key. Bạn có thể xem và sao chép lại trong danh sách Key ứng dụng.', 'Copy and save this key. You can view and copy it again in the Application keys list.')}</p><input readOnly aria-label={t('Giá trị key mới', 'New key value')} value={newKey} onFocus={e => e.target.select()} /><div className="admin-dialog-actions"><button type="button" disabled={copyingNewKey} onClick={() => void copyNewKey()}>{copyingNewKey ? t('Đang sao chép…', 'Copying…') : t('Sao chép key', 'Copy key')}</button><button type="button" onClick={() => { setNewKey('') }}>{t('Đã lưu key', 'I saved the key')}</button></div></AdminDialog>}
    {devices && <AdminDialog title="admin-devices-title" busy={busy} onClose={() => setDevices(null)}><h2 id="admin-devices-title">{t('Thiết bị kích hoạt', 'Activated devices')}</h2><p>{devices.item.name || devices.item.key_masked}</p>{devices.rows.length ? devices.rows.map(device => <div className="admin-device" key={device.id}><strong>{device.name || t('Thiết bị', 'Device')} · {deviceTypeText(device.device_type, locale)}</strong><small title={device.installation_id}>{t('Mã máy: ', 'Machine ID: ')}{device.installation_id.slice(0, 16).toUpperCase()}</small><small>{t('Lần kiểm tra cuối: ', 'Last checked: ')}{date(device.last_seen)}</small>{device.enabled ? <button disabled={busy} onClick={() => setConfirmation({ label: t('Thu hồi quyền của thiết bị này?', 'Revoke this device?'), run: async () => { await request(`/api/admin/keys/${devices.item.id}/devices/${device.id}`, 'DELETE'); setDevices({ ...devices, rows: devices.rows.map(row => row.id === device.id ? { ...row, enabled: 0 } : row) }) } })}>{t('Thu hồi', 'Revoke')}</button> : <span>{t('Đã thu hồi', 'Revoked')}</span>}</div>) : <p>{t('Chưa có thiết bị.', 'No devices yet.')}</p>}<button onClick={() => setDevices(null)}>{t('Đóng', 'Close')}</button></AdminDialog>}
    {history && <AdminDialog title="admin-history-title" busy={busy} onClose={() => setHistory(null)}><h2 id="admin-history-title">{t('Lịch sử quản lý', 'Management history')}</h2>{history.length ? history.map(entry => <details key={entry.id}><summary>{date(entry.created_at)} · {actionName(entry.action)} · {entry.kind === 'accounts' ? t('Tài khoản', 'Account') : t('Key', 'Key')}</summary><small>{t('Mã đối tượng: ', 'Target ID: ')}{entry.target_id}<br />{t('Người thực hiện: ', 'Actor: ')}{entry.actor_email || entry.actor_id}</small><dl>{Object.entries(JSON.parse(entry.changes) as Record<string, { before?: unknown; after?: unknown }>).map(([field, change]) => <div key={field}><dt>{fieldName(field)}</dt><dd>{formatChange(field, change.before)} → {formatChange(field, change.after)}</dd></div>)}</dl></details>) : <p>{t('Chưa có lịch sử.', 'No history yet.')}</p>}<button onClick={() => setHistory(null)}>{t('Đóng', 'Close')}</button></AdminDialog>}
    {confirmation && <AdminDialog title="admin-confirm-title" busy={busy} onClose={() => setConfirmation(null)}><h2 id="admin-confirm-title">{t('Xác nhận thao tác', 'Confirm action')}</h2><p>{confirmation.label}</p><div className="admin-dialog-actions"><button disabled={busy} onClick={() => setConfirmation(null)}>{t('Hủy', 'Cancel')}</button><button disabled={busy} onClick={() => void perform(confirmation.run)}>{busy ? t('Đang xử lý…', 'Processing…') : t('Xác nhận', 'Confirm')}</button></div></AdminDialog>}
  </main></WebFrame>
}
