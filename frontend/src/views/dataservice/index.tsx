import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/dataservice'
const columns = [
  "服务编号",
  "服务名称",
  "接口路径",
  "查询参数",
  "返回字段",
  "调用频次限制",
  "缓存策略",
  "服务版本"
]
const actions = [
  "提交服务评审",
  "上线服务",
  "下线服务"
]
const statuses = [
  "草稿",
  "服务评审中",
  "已上线",
  "已下线"
]
const stats = [
  {
    "label": "服务接口总数",
    "value": 0
  },
  {
    "label": "已上线服务",
    "value": 0
  },
  {
    "label": "评审中服务",
    "value": 0
  }
]
const contractColumns = [
  "服务编号",
  "接口路径",
  "查询参数",
  "返回字段",
  "服务版本",
  "契约版本",
  "缓存签名",
  "status",
  "冻结时间"
]
const snapshotColumns = [
  "服务编号",
  "接口路径",
  "契约版本",
  "服务版本",
  "返回字段",
  "缓存签名",
  "请求参数",
  "生成时间"
]

type CellValue = string | number | null | undefined | Record<string, unknown> | unknown[]
type Row = Record<string, CellValue>

function fmt(value: CellValue): string {
  if (value === null || value === undefined || value === '') return '—'
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

export default function DataserviceView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [contracts, setContracts] = useState<Row[]>([])
  const [snapshots, setSnapshots] = useState<Row[]>([])
  const [errorMessage, setErrorMessage] = useState('')
  const [notice, setNotice] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const [listResp, contractResp, snapshotResp] = await Promise.all([
        request(`${ENDPOINT}?${query}`),
        request(`${ENDPOINT}/contracts?size=100`),
        request(`${ENDPOINT}/snapshots?size=100`),
      ])
      if (!listResp.ok) {
        throw new Error('服务接口列表读取失败')
      }
      const payload = await listResp.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
      if (contractResp.ok) {
        const contractPayload = await contractResp.json()
        setContracts(contractPayload.items ?? [])
      }
      if (snapshotResp.ok) {
        const snapshotPayload = await snapshotResp.json()
        setSnapshots(snapshotPayload.items ?? [])
      }
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '数据服务列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ values: { action } }),
      })
      const payload = await response.json()
      if (!response.ok || !payload.ok) {
        throw new Error(payload.message ?? payload.detail ?? '数据服务动作未生效，请稍后重试')
      }
      setNotice(payload.message ?? '操作已完成')
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '数据服务操作失败')
    }
  }

  async function queryService(row: Row) {
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/query`, {
        method: 'POST',
        body: JSON.stringify({ values: {} }),
      })
      const payload = await response.json()
      if (!response.ok || !payload.ok) {
        throw new Error(payload.message ?? payload.detail ?? '服务查询未生效，请稍后重试')
      }
      const hit = payload.cache_hit ? '（命中既有缓存快照）' : '（已生成新响应快照）'
      setNotice(`${payload.message ?? '查询完成'}${hit}`)
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '数据服务查询失败')
    }
  }

  return (
    <section className="page" data-module="dataservice">
      <header className="page-head">
        <div>
          <h2>数据服务管理</h2>
          <p className="page-desc">管理对外暴露的数据分析服务接口，提供指标查询和维度下钻能力。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('服务接口登记入口尚未接入审批流')}>
            登记服务接口
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出数据服务清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {stats.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              {columns.map((column) => <td key={column}>{fmt(row[column])}</td>)}
              <td className="row-actions">
                {actions.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
                <button className="link" type="button" onClick={() => void queryService(row)}>
                  查询服务
                </button>
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 1}>暂无数据服务数据，可先登记服务接口</td>
            </tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>共 {total} 条数据服务记录</span>
        {notice && <span>{notice}</span>}
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>

      <header className="page-head">
        <div>
          <h2>接口契约快照</h2>
          <p className="page-desc">上线前冻结的接口路径、入参字段、返回字段与版本；缓存签名随契约版本变化。</p>
        </div>
      </header>
      <table className="data-table">
        <thead>
          <tr>
            {contractColumns.map((column) => <th key={column}>{column}</th>)}
          </tr>
        </thead>
        <tbody>
          {contracts.map((row) => (
            <tr key={String(row.id)}>
              {contractColumns.map((column) => <td key={column}>{fmt(row[column])}</td>)}
            </tr>
          ))}
          {contracts.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={contractColumns.length}>暂无接口契约快照，上线服务后自动冻结生成</td>
            </tr>
          )}
        </tbody>
      </table>

      <header className="page-head">
        <div>
          <h2>数据服务响应快照</h2>
          <p className="page-desc">查询数据服务结果生成的响应数据快照，接口版本、返回字段与缓存签名可追溯。</p>
        </div>
      </header>
      <table className="data-table">
        <thead>
          <tr>
            {snapshotColumns.map((column) => <th key={column}>{column}</th>)}
          </tr>
        </thead>
        <tbody>
          {snapshots.map((row) => (
            <tr key={String(row.id)}>
              {snapshotColumns.map((column) => <td key={column}>{fmt(row[column])}</td>)}
            </tr>
          ))}
          {snapshots.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={snapshotColumns.length}>暂无响应数据快照，对已上线服务执行「查询服务」后生成</td>
            </tr>
          )}
        </tbody>
      </table>
    </section>
  )
}
