<script setup>
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'

const RAW_API_BASE = import.meta.env.VITE_API_BASE || ''
const API_BASE = RAW_API_BASE.endsWith('/api') ? RAW_API_BASE.slice(0, -4) : (RAW_API_BASE || 'http://127.0.0.1:8000')

const page = ref('dashboard')
const authChecked = ref(false)
const authenticated = ref(false)
const authLoading = ref(false)
const authError = ref('')
const authForm = reactive({ username: 'kziii71', password: '' })

const form = reactive({
  total_accounts: 3,
  max_workers: 3,
  proxy: '',
  output_file: 'registered_accounts.txt',
})

const loading = ref(false)
const activeTaskId = ref('')
const activeStatus = ref('idle')
const logs = ref([])
const tasks = ref([])
const wsRef = ref(null)
const errorMsg = ref('')

const accounts = ref([])
const accountSummary = reactive({
  total_accounts: 0,
  normal_accounts: 0,
  abnormal_accounts: 0,
})
const accountError = ref('')
const selectedAccount = ref(null)
const selectedToken = ref(null)
const tokenLoading = ref(false)
const accountFilter = ref('all')
const checkingAllTokens = ref(false)
const accountRefreshing = ref(false)
const checkingAccounts = ref({})
const checkingTokenFiles = ref({})
const selectedEmails = ref([])
const pageAnimKey = ref(0)
const exportCount = ref(10)
const exportingAccounts = ref(false)

const settingsError = ref('')
const autoMaintainError = ref('')
const autoMaintain = reactive({
  enabled: false,
  running: false,
  interval_seconds: 0,
  target_count: 0,
  max_workers: 0,
  remote_valid_count: 0,
  last_started_at: null,
  last_finished_at: null,
  last_error: '',
  logs: [],
})
const checkingCodexTarget = ref(false)
const checkingRemoteStatus = ref(false)
const deletingRemoteFiles = ref(false)
const pushingCodexTokens = ref(false)
const remoteCheckResults = ref([])
const remoteCheckProgress = reactive({
  total: 0,
  done: 0,
  invalid: 0,
})
const codexPushForm = reactive({
  api_base_url: '',
  api_key: '',
  delete_local_after_upload: true,
  push_count: 0,
})
const localCodexFiles = ref([])
const remoteCodexSummary = reactive({
  remote_codex_total: 0,
  remote_overlap_total: 0,
})
const selectedCodexFiles = ref([])
const codexPushProgress = reactive({
  total: 0,
  done: 0,
  success: 0,
  failed: 0,
})

const currentStats = reactive({
  started: 0,
  success: 0,
  fail: 0,
  done: 0,
  total: 0,
})

const opNotice = ref({ show: false, text: '', type: 'info' })
let noticeTimer = null
let taskPollTimer = null
let wsReconnectTimer = null
let closingWsManually = false
let autoMaintainTimer = null
const lastLiveKey = ref('')
const lastLiveAt = ref(0)

const statusLabel = computed(() => {
  if (!activeTaskId.value) return '未运行'
  return `${activeStatus.value} 路 ${activeTaskId.value}`
})

const canStop = computed(() => ['running', 'pending', 'stopping'].includes(activeStatus.value))
const canStart = computed(() => !canStop.value && !loading.value)
const filteredAccounts = computed(() => {
  if (accountFilter.value === 'normal') return accounts.value.filter((x) => String(x.token_status?.status || '').toLowerCase() === 'active')
  if (accountFilter.value === 'abnormal') return accounts.value.filter((x) => ['expired', 'invalid', 'missing'].includes(String(x.token_status?.status || '').toLowerCase()))
  return accounts.value
})
const isAllSelected = computed(() => {
  if (filteredAccounts.value.length === 0) return false
  return filteredAccounts.value.every((x) => selectedEmails.value.includes(x.email))
})
const successRate = computed(() => {
  if (!currentStats.total) return 0
  return Math.round((currentStats.success / currentStats.total) * 100)
})
const progressPercent = computed(() => {
  if (!currentStats.total) return 0
  return Math.max(0, Math.min(100, Math.round((currentStats.done / currentStats.total) * 100)))
})
const progressAnimated = computed(() => canStop.value)
const spinnerStep = ref(0)
const loadingSteps = ['建立会话', '创建邮箱', '提交注册', '等待验证码', '完成资料', '获取Token']
const loadingText = computed(() => loadingSteps[spinnerStep.value % loadingSteps.length])

const activityItems = computed(() => {
  return logs.value
    .slice(-120)
    .reverse()
    .map((line, i) => {
      const m = line.match(/^\[([^\]]+)\]/)
      const account = m ? m[1] : 'SYS'
      const low = line.toLowerCase()
      let state = 'running'
      if (low.includes('success') || low.includes('注册成功')) state = 'success'
      else if (low.includes('failed') || low.includes('exception') || low.includes('失败')) state = 'failed'
      else if (low.includes('started') || low.includes('worker_boot')) state = 'started'
      return { key: `${i}-${line}`, account, state, text: line }
    })
})

async function checkSession() {
  try {
    const res = await fetch(`${API_BASE}/api/auth/session`, { credentials: 'include' })
    const data = await res.json().catch(() => ({}))
    authenticated.value = !!data?.authenticated
  } catch (e) {
    authenticated.value = false
  } finally {
    authChecked.value = true
  }
}

async function login() {
  authLoading.value = true
  authError.value = ''
  try {
    const res = await fetch(`${API_BASE}/api/auth/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify(authForm),
    })
    const data = await res.json().catch(() => ({}))
    if (!res.ok) throw new Error(data?.detail || '登录失败')
    authenticated.value = true
    showNotice('登录成功', 'success')
    await fetchTasks().catch(() => {})
    await fetchAutoMaintain().catch(() => {})
    if (page.value === 'accounts') await fetchAccounts().catch(() => {})
  } catch (e) {
    authError.value = e.message || '登录失败'
  } finally {
    authLoading.value = false
    authChecked.value = true
  }
}

async function logout() {
  try {
    await fetch(`${API_BASE}/api/auth/logout`, { method: 'POST', credentials: 'include' })
  } catch (e) {}
  authenticated.value = false
  authForm.password = ''
}

async function fetchTasks() {
  const res = await fetch(`${API_BASE}/api/tasks`, { credentials: 'include' })
  if (!res.ok) throw new Error('加载历史任务失败')
  tasks.value = await res.json()
}

async function fetchAccounts() {
  const prevEmail = selectedAccount.value?.email || ''
  const res = await fetch(`${API_BASE}/api/accounts`, { credentials: 'include' })
  if (!res.ok) throw new Error('加载账号失败')
  const data = await res.json()
  accounts.value = Array.isArray(data?.accounts) ? data.accounts : []
  accountSummary.total_accounts = Number(data?.summary?.total_accounts || 0)
  accountSummary.normal_accounts = Number(data?.summary?.normal_accounts || 0)
  accountSummary.abnormal_accounts = Number(data?.summary?.abnormal_accounts || 0)
  const valid = new Set(accounts.value.map((x) => x.email))
  selectedEmails.value = selectedEmails.value.filter((x) => valid.has(x))
  if (prevEmail) {
    selectedAccount.value = accounts.value.find((x) => x.email === prevEmail) || null
  }
}

async function fetchAutoMaintainStatus() {
  const res = await fetch(`${API_BASE}/api/auto-maintain`, { credentials: 'include' })
  if (!res.ok) throw new Error('加载自动维护状态失败')
  const data = await res.json()
  autoMaintain.enabled = !!data?.enabled
  autoMaintain.running = !!data?.running
  autoMaintain.interval_seconds = Number(data?.interval_seconds || 0)
  autoMaintain.target_count = Number(data?.target_count || 0)
  autoMaintain.max_workers = Number(data?.max_workers || 0)
  autoMaintain.remote_valid_count = Number(data?.remote_valid_count || 0)
  autoMaintain.last_started_at = data?.last_started_at || null
  autoMaintain.last_finished_at = data?.last_finished_at || null
  autoMaintain.last_error = data?.last_error || ''
  autoMaintain.logs = Array.isArray(data?.logs) ? data.logs : []
}

function startAutoMaintainPolling() {
  stopAutoMaintainPolling()
  fetchAutoMaintainStatus().catch((e) => {
    autoMaintainError.value = e.message
  })
  autoMaintainTimer = setInterval(() => {
    if (page.value !== 'auto-maintain') return
    fetchAutoMaintainStatus().catch((e) => {
      autoMaintainError.value = e.message
    })
  }, 5000)
}

function stopAutoMaintainPolling() {
  if (autoMaintainTimer) {
    clearInterval(autoMaintainTimer)
    autoMaintainTimer = null
  }
}

function formatUnixTime(ts) {
  if (!ts) return '-'
  const d = new Date(Number(ts) * 1000)
  if (Number.isNaN(d.getTime())) return '-'
  return d.toLocaleString()
}


function codexPushPercent() {
  if (!codexPushProgress.total) return 0
  return Math.max(0, Math.min(100, Math.round((codexPushProgress.done / codexPushProgress.total) * 100)))
}

function remoteCheckPercent() {
  if (!remoteCheckProgress.total) return 0
  return Math.max(0, Math.min(100, Math.round((remoteCheckProgress.done / remoteCheckProgress.total) * 100)))
}

function applyCodexCountSelection() {
  const n = Number(codexPushForm.push_count || 0)
  if (!Number.isFinite(n) || n <= 0) {
    selectedCodexFiles.value = []
    return
  }
  const use = Math.min(n, localCodexFiles.value.length)
  selectedCodexFiles.value = localCodexFiles.value.slice(0, use).map((x) => x.name)
}

function toggleCodexFile(name) {
  if (selectedCodexFiles.value.includes(name)) {
    selectedCodexFiles.value = selectedCodexFiles.value.filter((x) => x !== name)
  } else {
    selectedCodexFiles.value = [...selectedCodexFiles.value, name]
  }
}

async function checkCodexPushTarget() {
  checkingCodexTarget.value = true
  settingsError.value = ''
  try {
    const res = await fetch(`${API_BASE}/api/codex-push/check`, {
      credentials: 'include',
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
      }),
    })
    const data = await res.json().catch(() => ({}))
    if (!res.ok) throw new Error(data?.detail || '预检失败')

    localCodexFiles.value = Array.isArray(data?.local_files) ? data.local_files : []
    remoteCodexSummary.remote_codex_total = Number(data?.remote_codex_total || 0)
    remoteCodexSummary.remote_overlap_total = Number(data?.remote_overlap_total || 0)
    codexPushForm.push_count = localCodexFiles.value.length
    applyCodexCountSelection()

    if (data?.remote_error) {
      settingsError.value = data.remote_error
      showNotice(`仅本地可用：本地 ${localCodexFiles.value.length}，远端读取失败`, 'error')
    } else {
      showNotice(`预检通过：本地 ${localCodexFiles.value.length}，远端 codex ${remoteCodexSummary.remote_codex_total}`, 'success')
    }
  } catch (e) {
    localCodexFiles.value = []
    selectedCodexFiles.value = []
    settingsError.value = e.message || '预检失败'
    showNotice('管理预检失败，已终止上传', 'error')
  } finally {
    checkingCodexTarget.value = false
  }
}

async function pushCodexTokensToProxy() {
  if (!localCodexFiles.value.length) {
    settingsError.value = '请先执行预检并加载本地 codex tokens'
    return
  }
  const targets = localCodexFiles.value
    .filter((x) => selectedCodexFiles.value.includes(x.name))
    .map((x) => x.name)
  if (!targets.length) {
    settingsError.value = '请至少选择一个 token 文件'
    return
  }

  pushingCodexTokens.value = true
  settingsError.value = ''
  codexPushProgress.total = targets.length
  codexPushProgress.done = 0
  codexPushProgress.success = 0
  codexPushProgress.failed = 0

  try {
    for (const name of targets) {
      const res = await fetch(`${API_BASE}/api/codex-push/single`, {
        credentials: 'include',
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          filename: name,
          delete_local_after_upload: !!codexPushForm.delete_local_after_upload,
        }),
      })
      const data = await res.json().catch(() => ({}))
      codexPushProgress.done += 1
      if (res.ok && data?.ok) codexPushProgress.success += 1
      else codexPushProgress.failed += 1
    }

    const type = codexPushProgress.failed > 0 ? 'error' : 'success'
    showNotice(`推送完成 ${codexPushProgress.done}/${codexPushProgress.total}，成功 ${codexPushProgress.success}，失败 ${codexPushProgress.failed}`, type)
    await checkCodexPushTarget()
    if (page.value === 'accounts') await fetchAccounts().catch(() => {})
  } catch (e) {
    settingsError.value = e.message || '推送失败'
    showNotice('推送 Codex 认证文件失败', 'error')
  } finally {
    pushingCodexTokens.value = false
  }
}

async function checkRemoteStatus() {
  checkingRemoteStatus.value = true
  settingsError.value = ''
  remoteCheckResults.value = []
  remoteCheckProgress.total = 0
  remoteCheckProgress.done = 0
  remoteCheckProgress.invalid = 0

  try {
    const precheckRes = await fetch(`${API_BASE}/api/codex-push/check`, {
      credentials: 'include',
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
      }),
    })
    const precheckData = await precheckRes.json().catch(() => ({}))
    if (!precheckRes.ok) throw new Error(precheckData?.detail || '检测失败')

    const remoteNames = Array.isArray(precheckData?.remote_codex_names) ? precheckData.remote_codex_names : []
    if (remoteNames.length === 0) {
      showNotice('远端没有 codex 文件', 'info')
      return
    }

    remoteCheckProgress.total = remoteNames.length

    const ordered = new Array(remoteNames.length)
    const chunkSize = 20
    const chunks = []
    for (let i = 0; i < remoteNames.length; i += chunkSize) {
      chunks.push({ start: i, names: remoteNames.slice(i, i + chunkSize) })
    }

    const batchConcurrency = Math.min(3, Math.max(1, chunks.length))
    let chunkCursor = 0

    async function batchWorker() {
      while (true) {
        const currentChunk = chunkCursor
        chunkCursor += 1
        if (currentChunk >= chunks.length) return

        const chunk = chunks[currentChunk]
        try {
          const res = await fetch(`${API_BASE}/api/codex-push/check-remote-status-batch`, {
              credentials: 'include',
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              filenames: chunk.names,
              max_workers: 16,
            }),
          })
          const data = await res.json().catch(() => ({}))
          const rows = (res.ok && Array.isArray(data?.results)) ? data.results : []
          for (let j = 0; j < chunk.names.length; j += 1) {
            const idx = chunk.start + j
            const fallback = { name: chunk.names[j], is_invalid: true, status_code: null, message: res.ok ? '检测失败' : (data?.detail || '检测失败') }
            ordered[idx] = rows[j] || fallback
            const row = ordered[idx]
            remoteCheckResults.value = [...remoteCheckResults.value, row]
            if (row.is_invalid) remoteCheckProgress.invalid += 1
            remoteCheckProgress.done += 1
          }
        } catch (e) {
          for (let j = 0; j < chunk.names.length; j += 1) {
            const idx = chunk.start + j
            ordered[idx] = { name: chunk.names[j], is_invalid: true, status_code: null, message: e?.message || '检测失败' }
            const row = ordered[idx]
            remoteCheckResults.value = [...remoteCheckResults.value, row]
            if (row.is_invalid) remoteCheckProgress.invalid += 1
            remoteCheckProgress.done += 1
          }
        }
      }
    }

    await Promise.all(Array.from({ length: batchConcurrency }, () => batchWorker()))
    remoteCheckResults.value = ordered.filter(Boolean)
    showNotice(`远端检测完毕：共 ${remoteCheckProgress.total} 个文件，其中 ${remoteCheckProgress.invalid} 个无效(401)`, 'success')
  } catch (e) {
    settingsError.value = e.message || '检测远端状态失败'
    showNotice('检测远端状态失败', 'error')
  } finally {
    checkingRemoteStatus.value = false
  }
}
async function deleteInvalidRemoteFiles() {
  const invalidFiles = remoteCheckResults.value
    .filter(x => Number(x.status_code) !== 200)
    .map(x => x.name)
  if (!invalidFiles.length) {
    showNotice('没有检测到非200文件需要删除', 'info')
    return
  }
  
  if (!confirm(`确定要删除 ${invalidFiles.length} 个非200文件吗？此操作不可逆。`)) return

  deletingRemoteFiles.value = true
  settingsError.value = ''
  
  try {
    const res = await fetch(`${API_BASE}/api/codex-push/delete-remote-files`, {
      credentials: 'include',
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        filenames: invalidFiles
      }),
    })
    const data = await res.json().catch(() => ({}))
    if (!res.ok) throw new Error(data?.detail || '删除失败')
    
    showNotice(`删除完成：成功 ${data.deleted?.length || 0} 个，失败 ${data.failed?.length || 0} 个`, 'success')
    if (data.failed && data.failed.length > 0) {
      settingsError.value = `有 ${data.failed.length} 个文件删除失败: ${data.failed[0].error}`
    }
    
    // Clear list and refresh counts
    remoteCheckResults.value = []
    await checkCodexPushTarget()
  } catch (e) {
    settingsError.value = e.message || '删除远端文件失败'
    showNotice('删除远端文件失败', 'error')
  } finally {
    deletingRemoteFiles.value = false
  }
}

function tokenStatusLabel(status) {
  const s = String(status || 'unknown').toLowerCase()
  if (s === 'active') return '正常'
  if (s === 'expiring') return '即将过期'
  if (s === 'expired') return '已过期'
  if (s === 'missing') return '缺失'
  if (s === 'invalid') return '无效'
  return '未知'
}

function tokenStatusClass(status) {
  const s = String(status || 'unknown').toLowerCase()
  if (s === 'active') return 'pill-active'
  if (s === 'expiring') return 'pill-expiring'
  if (s === 'expired' || s === 'missing' || s === 'invalid') return 'pill-bad'
  return 'pill-unknown'
}

function quotaDisplayPercent(quotaItem) {
  if (!quotaItem || typeof quotaItem !== 'object') return null
  if (quotaItem.ok) return 100
  return null
}

function quotaDisplayLabel(quotaItem) {
  const p = quotaDisplayPercent(quotaItem)
  return p === null ? '--' : `${p}%`
}

function quotaResetLabel(v) {
  const n = Number(v)
  if (!Number.isFinite(n) || n <= 0) return '-'
  if (n >= 1000000000) {
    const d = new Date(n * 1000)
    return Number.isNaN(d.getTime()) ? '-' : d.toLocaleString()
  }
  const sec = Math.floor(n)
  const d = Math.floor(sec / 86400)
  const h = Math.floor((sec % 86400) / 3600)
  const m = Math.floor((sec % 3600) / 60)
  if (d > 0) return `${d}天${h}小时`
  if (h > 0) return `${h}小时${m}分钟`
  return `${m}分钟`
}

function quotaBarClass(percent) {
  if (percent === null) return 'quota-unknown'
  if (percent >= 60) return 'quota-good'
  if (percent >= 20) return 'quota-warn'
  return 'quota-bad'
}

function appendLog(line) {
  if (!line) return
  logs.value.push(line)
  if (logs.value.length > 1200) logs.value = logs.value.slice(-1200)
}

function applyTaskSnapshot(data) {
  if (!data) return
  if (typeof data.status !== 'undefined') activeStatus.value = data.status || activeStatus.value
  if (typeof data.started_count !== 'undefined') currentStats.started = data.started_count || 0
  if (typeof data.success_count !== 'undefined') currentStats.success = data.success_count || 0
  if (typeof data.fail_count !== 'undefined') currentStats.fail = data.fail_count || 0
  if (typeof data.completed_count !== 'undefined') currentStats.done = data.completed_count || 0
  if (typeof data.total_accounts !== 'undefined') currentStats.total = data.total_accounts || 0
}

function stopTaskPolling() {
  if (taskPollTimer) {
    clearInterval(taskPollTimer)
    taskPollTimer = null
  }
}

function startTaskPolling(taskId) {
  stopTaskPolling()
  taskPollTimer = setInterval(async () => {
    try {
      const res = await fetch(`${API_BASE}/api/tasks/${taskId}`, { credentials: 'include' })
      if (!res.ok) return
      const data = await res.json()
      applyTaskSnapshot(data)
      if (['completed', 'failed', 'stopped'].includes(data.status)) {
        stopTaskPolling()
        const text = `注册完成：成功 ${data.success_count || 0}，失败 ${data.fail_count || 0}`
        showNotice(text, (data.fail_count || 0) > 0 ? 'error' : 'success')
        fetchTasks().catch(() => {})
        fetchAccounts().catch(() => {})
      }
    } catch (_) {}
  }, 1000)
}

function showNotice(text, type = 'info') {
  if (noticeTimer) clearTimeout(noticeTimer)
  opNotice.value = { show: true, text, type }
  noticeTimer = setTimeout(() => {
    opNotice.value = { ...opNotice.value, show: false }
  }, 1800)
}

function closeWs() {
  stopTaskPolling()
  if (wsReconnectTimer) {
    clearTimeout(wsReconnectTimer)
    wsReconnectTimer = null
  }
  if (wsRef.value) {
    closingWsManually = true
    wsRef.value.close()
    wsRef.value = null
    setTimeout(() => {
      closingWsManually = false
    }, 0)
  }
}

function connectWs(taskId) {
  closeWs()
  startTaskPolling(taskId)
}

async function startTask() {
  errorMsg.value = ''
  loading.value = true
  logs.value = []
  stopTaskPolling()
  try {
    const res = await fetch(`${API_BASE}/api/tasks`, {
      credentials: 'include',
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        total_accounts: Number(form.total_accounts),
        max_workers: Number(form.max_workers),
        proxy: form.proxy || null,
        output_file: form.output_file || 'registered_accounts.txt',
      }),
    })
    if (!res.ok) throw new Error(`启动失败: ${res.status}`)
    const data = await res.json()
    activeTaskId.value = data.task_id
    activeStatus.value = data.status
    currentStats.started = 0
    currentStats.success = 0
    currentStats.fail = 0
    currentStats.done = 0
    currentStats.total = Number(form.total_accounts)
    appendLog(`[UI] 任务已启动 ${data.task_id}`)
    connectWs(data.task_id)
    await fetchTasks()
  } catch (e) {
    errorMsg.value = e.message
  } finally {
    loading.value = false
  }
}

async function stopTask() {
  if (!activeTaskId.value) return
  errorMsg.value = ''
  activeStatus.value = 'stopping'
  appendLog('[UI] 正在发送停止请求...')
  try {
    const res = await fetch(`${API_BASE}/api/tasks/${activeTaskId.value}/stop`, { method: 'POST', credentials: 'include' })
    if (!res.ok) throw new Error('停止任务失败')
    const data = await res.json()
    activeStatus.value = data.status
    appendLog(`[UI] 停止请求已发送 ${data.status}`)
  } catch (e) {
    errorMsg.value = e.message
    appendLog(`[UI] 停止请求失败: ${e.message}`)
  }
}

async function inspectTask(taskId) {
  errorMsg.value = ''
  try {
    const res = await fetch(`${API_BASE}/api/tasks/${taskId}`, { credentials: 'include' })
    if (!res.ok) throw new Error('加载任务详情失败')
    const data = await res.json()
    activeTaskId.value = data.task_id
    logs.value = []
    applyTaskSnapshot(data)
    if (['running', 'pending'].includes(data.status)) connectWs(taskId)
    else closeWs()
  } catch (e) {
    errorMsg.value = e.message
  }
}

function switchPage(next) {
  page.value = next
  pageAnimKey.value += 1
  if (next === 'accounts') {
    accountError.value = ''
    fetchAccounts().catch((e) => {
      accountError.value = e.message
    })
  }
  if (next === 'settings') {
    settingsError.value = ''
  }
  if (next === 'auto-maintain') {
    autoMaintainError.value = ''
    startAutoMaintainPolling()
  } else {
    stopAutoMaintainPolling()
  }
}

function selectAccount(item) {
  selectedAccount.value = item
  selectedToken.value = null
}

async function openToken(tokenName) {
  tokenLoading.value = true
  accountError.value = ''
  try {
    const res = await fetch(`${API_BASE}/api/tokens/${encodeURIComponent(tokenName)}`, { credentials: 'include' })
    if (!res.ok) throw new Error('加载 token 文件失败')
    selectedToken.value = await res.json()
  } catch (e) {
    accountError.value = e.message
  } finally {
    tokenLoading.value = false
  }
}

function applyAccountStatus(email, tokenStatus) {
  const idx = accounts.value.findIndex((x) => x.email === email)
  if (idx >= 0) {
    accounts.value[idx] = { ...accounts.value[idx], token_status: tokenStatus }
  }
  if (selectedAccount.value?.email === email) {
    selectedAccount.value = { ...selectedAccount.value, token_status: tokenStatus }
  }
}

function applyTokenFileStatus(tokenName, tokenStatus) {
  if (!selectedAccount.value) return
  const files = Array.isArray(selectedAccount.value.token_files) ? [...selectedAccount.value.token_files] : []
  const i = files.findIndex((x) => x.name === tokenName)
  if (i >= 0) {
    files[i] = {
      ...files[i],
      status: tokenStatus?.status,
      message: tokenStatus?.message,
      error_code: tokenStatus?.error_code,
      expired_at: tokenStatus?.expired_at,
    }
    selectedAccount.value = { ...selectedAccount.value, token_files: files, token_status: tokenStatus }
    const idx = accounts.value.findIndex((x) => x.email === selectedAccount.value.email)
    if (idx >= 0) {
      accounts.value[idx] = { ...accounts.value[idx], token_files: files, token_status: tokenStatus }
    }
  }
}

async function checkTokenFileStatus(tokenName) {
  checkingTokenFiles.value[tokenName] = true
  accountError.value = ''
  try {
    const res = await fetch(`${API_BASE}/api/tokens/${encodeURIComponent(tokenName)}/check`, { method: 'POST', credentials: 'include' })
    if (!res.ok) throw new Error('检测 token 文件状态失败')
    const tokenStatus = await res.json()
    applyTokenFileStatus(tokenName, tokenStatus)
    showNotice(`检测完成 ${tokenName}`, 'success')
  } catch (e) {
    accountError.value = e.message
    showNotice(`检测失败 ${tokenName}`, 'error')
  } finally {
    checkingTokenFiles.value[tokenName] = false
  }
}

async function refreshAccountsList() {
  accountRefreshing.value = true
  accountError.value = ''
  try {
    await fetchAccounts()
    showNotice('账号列表已刷新', 'success')
  } catch (e) {
    accountError.value = e.message
    showNotice('刷新账号列表失败', 'error')
  } finally {
    accountRefreshing.value = false
  }
}

async function checkAllTokenStatus() {
  checkingAllTokens.value = true
  accountError.value = ''

  const targets = filteredAccounts.value.map((x) => x.email)
  for (const email of targets) checkingAccounts.value[email] = true
  if (selectedAccount.value?.token_files?.length) {
    for (const f of selectedAccount.value.token_files) checkingTokenFiles.value[f.name] = true
  }

  const limit = Math.min(20, Math.max(1, targets.length))
  let cursor = 0
  let failed = 0

  async function runOne(email) {
    try {
      const res = await fetch(`${API_BASE}/api/accounts/${encodeURIComponent(email)}/check`, { method: 'POST', credentials: 'include' })
      if (!res.ok) throw new Error('账号检测失败')
      const data = await res.json()
      applyAccountStatus(email, data?.token_status || {})
    } catch (_) {
      failed += 1
    } finally {
      checkingAccounts.value[email] = false
      if (selectedAccount.value?.email === email && selectedAccount.value?.token_files?.length) {
        for (const f of selectedAccount.value.token_files) checkingTokenFiles.value[f.name] = false
      }
    }
  }

  async function worker() {
    while (cursor < targets.length) {
      const i = cursor
      cursor += 1
      await runOne(targets[i])
    }
  }

  try {
    await Promise.all(Array.from({ length: limit }, () => worker()))
    const normal = accounts.value.filter((x) => String(x.token_status?.status || '').toLowerCase() === 'active').length
    const abnormal = accounts.value.filter((x) => ['expired', 'invalid', 'missing'].includes(String(x.token_status?.status || '').toLowerCase())).length
    accountSummary.total_accounts = accounts.value.length
    accountSummary.normal_accounts = normal
    accountSummary.abnormal_accounts = abnormal
    if (failed > 0) {
      showNotice(`检测完成，失败 ${failed} 个`, 'error')
    } else {
      showNotice('全部 token 状态检测完成', 'success')
    }
  } catch (e) {
    accountError.value = e.message
    showNotice('一键检测失败', 'error')
  } finally {
    checkingAllTokens.value = false
    for (const k of Object.keys(checkingAccounts.value)) checkingAccounts.value[k] = false
    for (const k of Object.keys(checkingTokenFiles.value)) checkingTokenFiles.value[k] = false
  }
}

async function checkAccountStatus(email) {
  checkingAccounts.value[email] = true
  accountError.value = ''
  try {
    const res = await fetch(`${API_BASE}/api/accounts/${encodeURIComponent(email)}/check`, { method: 'POST', credentials: 'include' })
    if (!res.ok) throw new Error('账号检测失败')
    const data = await res.json()
    applyAccountStatus(email, data?.token_status || {})
    const normal = accounts.value.filter((x) => String(x.token_status?.status || '').toLowerCase() === 'active').length
    const abnormal = accounts.value.filter((x) => ['expired', 'invalid', 'missing'].includes(String(x.token_status?.status || '').toLowerCase())).length
    accountSummary.total_accounts = accounts.value.length
    accountSummary.normal_accounts = normal
    accountSummary.abnormal_accounts = abnormal
    showNotice(`账号检测完成 ${email}`, 'success')
  } catch (e) {
    accountError.value = e.message
    showNotice(`账号检测失败 ${email}`, 'error')
  } finally {
    checkingAccounts.value[email] = false
  }
}

async function removeAccount(email) {
  if (!window.confirm(`确认删除账号 ${email} ?`)) return
  accountError.value = ''
  try {
    const res = await fetch(`${API_BASE}/api/accounts/${encodeURIComponent(email)}`, { method: 'DELETE', credentials: 'include' })
    if (!res.ok) {
      let detail = '删除失败'
      try {
        const body = await res.json()
        if (body?.detail) detail = body.detail
      } catch (_) {}
      throw new Error(detail)
    }
    if (selectedAccount.value?.email === email) {
      selectedAccount.value = null
      selectedToken.value = null
    }
    selectedEmails.value = selectedEmails.value.filter((x) => x !== email)
    await fetchAccounts()
  } catch (e) {
    accountError.value = e.message
  }
}

function toggleSelectAll() {
  if (isAllSelected.value) {
    const inView = new Set(filteredAccounts.value.map((x) => x.email))
    selectedEmails.value = selectedEmails.value.filter((x) => !inView.has(x))
  } else {
    const merged = new Set([...selectedEmails.value, ...filteredAccounts.value.map((x) => x.email)])
    selectedEmails.value = Array.from(merged)
  }
}

function toggleSelectEmail(email) {
  if (selectedEmails.value.includes(email)) {
    selectedEmails.value = selectedEmails.value.filter((x) => x !== email)
  } else {
    selectedEmails.value = [...selectedEmails.value, email]
  }
}

async function batchDeleteSelected() {
  if (selectedEmails.value.length === 0) {
    accountError.value = '请先选择账号'
    return
  }
  if (!window.confirm(`确认删除已选中的 ${selectedEmails.value.length} 个账号?`)) return
  accountError.value = ''
  try {
    const res = await fetch(`${API_BASE}/api/accounts/batch-delete`, {
      credentials: 'include',
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ emails: selectedEmails.value }),
    })
    if (!res.ok) {
      let detail = '批量删除失败'
      try {
        const body = await res.json()
        if (body?.detail) detail = body.detail
      } catch (_) {}
      throw new Error(detail)
    }
    selectedEmails.value = []
    selectedAccount.value = null
    selectedToken.value = null
    await fetchAccounts()
  } catch (e) {
    accountError.value = e.message
  }
}

async function clearAllAccounts() {
  if (!window.confirm('确认清空所有账号？此操作不可撤销。')) return
  accountError.value = ''
  try {
    const res = await fetch(`${API_BASE}/api/accounts`, { method: 'DELETE', credentials: 'include' })
    if (!res.ok) {
      let detail = '清空失败'
      try {
        const body = await res.json()
        if (body?.detail) detail = body.detail
      } catch (_) {}
      throw new Error(detail)
    }
    selectedEmails.value = []
    selectedAccount.value = null
    selectedToken.value = null
    await fetchAccounts()
  } catch (e) {
    accountError.value = e.message
  }
}

async function clearAbnormalAccounts() {
  if (!window.confirm('确认一键删除所有异常账号（已过期/无效/缺失）?')) return
  accountError.value = ''
  try {
    const res = await fetch(`${API_BASE}/api/accounts/clear-abnormal`, { method: 'POST', credentials: 'include' })
    if (!res.ok) {
      let detail = '删除异常账号失败'
      try {
        const body = await res.json()
        if (body?.detail) detail = body.detail
      } catch (_) {}
      throw new Error(detail)
    }
    const data = await res.json()
    selectedEmails.value = []
    selectedAccount.value = null
    selectedToken.value = null
    await fetchAccounts()
    showNotice(`已删除异常账号 ${Number(data?.deleted || 0)} 个`, 'success')
  } catch (e) {
    accountError.value = e.message
    showNotice('删除异常账号失败', 'error')
  }
}

async function exportAccountsTxt() {
  exportingAccounts.value = true
  accountError.value = ''
  try {
    const n = Math.max(1, Number(exportCount.value || 1))
    const res = await fetch(`${API_BASE}/api/accounts/export`, {
      credentials: 'include',
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ count: n }),
    })
    if (!res.ok) {
      let detail = '导出失败'
      try {
        const body = await res.json()
        if (body?.detail) detail = body.detail
      } catch (_) {}
      throw new Error(detail)
    }

    const blob = await res.blob()
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `accounts_export_${n}.txt`
    document.body.appendChild(a)
    a.click()
    a.remove()
    URL.revokeObjectURL(url)
    showNotice(`已导出前 ${n} 条账号`, 'success')
  } catch (e) {
    accountError.value = e.message
    showNotice('导出账号失败', 'error')
  } finally {
    exportingAccounts.value = false
  }
}

let timer = null
let spinnerTimer = null
let accountsTimer = null
onMounted(async () => {
  await checkSession()
  if (!authenticated.value) return
  try {
    await fetchTasks()
  } catch (e) {
    errorMsg.value = e.message
  }
  timer = setInterval(() => {
    if (authenticated.value) fetchTasks().catch(() => {})
  }, 3000)
  spinnerTimer = setInterval(() => {
    if (canStop.value) spinnerStep.value = (spinnerStep.value + 1) % 1000
  }, 500)
  accountsTimer = setInterval(() => {
    if (authenticated.value && page.value === 'accounts') fetchAccounts().catch(() => {})
  }, 2000)
})

onUnmounted(() => {
  if (timer) clearInterval(timer)
  if (spinnerTimer) clearInterval(spinnerTimer)
  if (accountsTimer) clearInterval(accountsTimer)
  stopAutoMaintainPolling()
  if (noticeTimer) clearTimeout(noticeTimer)
  stopTaskPolling()
  closeWs()
})
</script>

<template>
  <div v-if="!authChecked" class="auth-shell">
    <div class="auth-card">
      <div class="auth-badge">0k-code-x</div>
      <div class="auth-title">正在检查登录状态</div>
      <p class="auth-subtitle">请稍候…</p>
    </div>
  </div>
  <div v-else-if="!authenticated" class="auth-shell">
    <div class="auth-card">
      <div class="auth-badge">0k-code-x</div>
      <h1 class="auth-title">欢迎回来</h1>
      <p class="auth-subtitle">请输入账号和密码后继续访问控制台。</p>
      <div class="form-row">
        <label>用户名</label>
        <input v-model="authForm.username" autocomplete="username" />
      </div>
      <div class="form-row">
        <label>密码</label>
        <input v-model="authForm.password" type="password" autocomplete="current-password" @keyup.enter="login" />
      </div>
      <p v-if="authError" class="auth-error">{{ authError }}</p>
      <button class="btn btn-primary auth-submit" :disabled="authLoading" @click="login">{{ authLoading ? '登录中...' : '进入控制台' }}</button>
    </div>
  </div>
  <div v-else class="page">
    <Transition name="notice-pop">
      <div v-if="opNotice.show" class="op-notice" :class="`notice-${opNotice.type}`">{{ opNotice.text }}</div>
    </Transition>
    <header class="header">
      <h1>ChatGPT Register Dashboard</h1>
      <div class="header-right">
        <button class="btn" :class="{ active: page === 'dashboard' }" @click="switchPage('dashboard')">控制台</button>
        <button class="btn" :class="{ active: page === 'accounts' }" @click="switchPage('accounts')">账号管理</button>
        <button class="btn" :class="{ active: page === 'auto-maintain' }" @click="switchPage('auto-maintain')">自动维护</button>
        <button class="btn" :class="{ active: page === 'settings' }" @click="switchPage('settings')">设置</button>
        <div class="status">{{ statusLabel }}</div>
        <button class="btn" @click="logout">退出</button>
      </div>
    </header>

    <Transition name="page-fade" mode="out-in">
      <div class="page-container" :key="`${page}-${pageAnimKey}`">
        <template v-if="page === 'dashboard'">
          <div class="dashboard-top-section">
            <section class="card stats-card">
              <h2>实时统计</h2>
              <div class="stats">
                <div class="stat"><span>成功</span><strong class="text-success">{{ currentStats.success }}</strong></div>
                <div class="stat"><span>失败</span><strong class="text-danger">{{ currentStats.fail }}</strong></div>
                <div class="stat"><span>进行中</span><strong>{{ Math.max(currentStats.started - currentStats.done, 0) }}</strong></div>
              </div>
              <div class="result-line" v-if="['completed','failed','stopped'].includes(activeStatus)">
                <span class="text-success">成功 {{ currentStats.success }}</span>
                <span class="text-danger">失败 {{ currentStats.fail }}</span>
                <span>成功率 {{ successRate }}%</span>
              </div>
            </section>
          </div>

          <div class="grid">
            <section class="card form-card">
              <h2>任务配置</h2>
              <div class="form-row">
                <label>注册数量</label>
                <input v-model.number="form.total_accounts" type="number" min="1" max="1000" />
              </div>
              <div class="form-row">
                <label>并发数</label>
                <input v-model.number="form.max_workers" type="number" min="1" max="100" />
              </div>
              <div class="form-row">
                <label>代理</label>
                <input v-model="form.proxy" placeholder="http://127.0.0.1:7890" />
              </div>
              <div class="form-row">
                <label>输出文件</label>
                <input v-model="form.output_file" placeholder="registered_accounts.txt" />
              </div>
              <div class="actions">
                <button class="btn btn-primary" :disabled="!canStart" @click="startTask">开始任务</button>
                <button class="btn btn-danger" :disabled="!canStop" @click="stopTask">停止任务</button>
              </div>
              <p v-if="errorMsg" class="error">{{ errorMsg }}</p>
            </section>

            <section class="card logs-card">
              <h2 class="process-title">
                执行进度
                <span v-if="canStop" class="inline-runner"><span class="spinner"></span>运行中</span>
              </h2>
              <div class="progress-shell">
                <div class="progress-label">
                  <span>进度 {{ currentStats.done }} / {{ currentStats.total || 0 }}</span>
                  <span>{{ progressPercent }}%</span>
                </div>
                <div class="progress-track">
                  <div class="progress-fill" :class="{ animated: progressAnimated }" :style="{ width: `${progressPercent}%` }"></div>
                </div>
              </div>
              <div class="loading-placeholder" v-if="canStop">
                <span class="spinner"></span>
                <span>任务执行中，等待最终结果...</span>
              </div>
              <div class="empty" v-else>实时日志已关闭，任务结束后自动显示结果。</div>
            </section>

            <section class="card history-card">
              <h2>历史任务</h2>
              <div class="table-wrap staggered-fade" style="animation-delay: 0.1s">
                <table>
                  <thead><tr><th>Task ID</th><th>状态</th><th>进度</th><th>成功</th><th>失败</th><th>操作</th></tr></thead>
                  <tbody>
                    <tr v-for="task in tasks" :key="task.task_id">
                      <td>{{ task.task_id }}</td>
                      <td>{{ task.status }}</td>
                      <td>{{ task.completed_count }}/{{ task.total_accounts }}</td>
                      <td>{{ task.success_count }}</td>
                      <td>{{ task.fail_count }}</td>
                      <td><button class="btn" @click="inspectTask(task.task_id)">查看</button></td>
                    </tr>
                    <tr v-if="tasks.length === 0"><td colspan="6" class="empty">暂无任务</td></tr>
                  </tbody>
                </table>
              </div>
            </section>
          </div>
        </template>

        <template v-else-if="page === 'accounts'">
          <div class="accounts-grid">
            <section class="card account-list">
              <h2>账号管理（删查）</h2>
              <p v-if="accountError" class="error">{{ accountError }}</p>

              <div class="account-summary-grid staggered-fade">
                <div class="summary-tile">
                  <span>总账号数量</span>
                  <strong>{{ accountSummary.total_accounts }}</strong>
                </div>
                <div class="summary-tile summary-good">
                  <span>正常账号数量</span>
                  <strong>{{ accountSummary.normal_accounts }}</strong>
                </div>
                <div class="summary-tile summary-bad">
                  <span>异常账号数量</span>
                  <strong>{{ accountSummary.abnormal_accounts }}</strong>
                </div>
              </div>

              <div class="actions action-bar staggered-fade">
                <button class="btn" :class="{ active: accountFilter === 'all' }" @click="accountFilter = 'all'">全部</button>
                <button class="btn" :class="{ active: accountFilter === 'normal' }" @click="accountFilter = 'normal'">正常</button>
                <button class="btn" :class="{ active: accountFilter === 'abnormal' }" @click="accountFilter = 'abnormal'">异常</button>
                <button class="btn" :disabled="accountRefreshing" @click="refreshAccountsList">{{ accountRefreshing ? '刷新中...' : '刷新' }}</button>
                <button class="btn" :disabled="checkingAllTokens" @click="checkAllTokenStatus">
                  <span class="btn-loading-shell"><span class="btn-spinner-slot"><span v-if="checkingAllTokens" class="spinner btn-spinner"></span></span><span>一键检测所有文件</span></span>
                </button>
                <button class="btn btn-danger" :disabled="accountSummary.abnormal_accounts === 0" @click="clearAbnormalAccounts">一键删除所有异常账号</button>
                <button class="btn" @click="toggleSelectAll">{{ isAllSelected ? '取消全选' : '全选' }}</button>
                <button class="btn btn-danger" :disabled="selectedEmails.length === 0" @click="batchDeleteSelected">一键删除已选</button>
                <button class="btn btn-danger" :disabled="accounts.length === 0" @click="clearAllAccounts">一键清空全部</button>
                <input v-model.number="exportCount" type="number" min="1" :max="Math.max(accounts.length, 1)" style="width: 110px;" />
                <button class="btn" :disabled="accounts.length === 0 || exportingAccounts" @click="exportAccountsTxt">{{ exportingAccounts ? '导出中...' : '导出TXT(前N条)' }}</button>
              </div>

              <div class="table-wrap staggered-fade" style="animation-delay: 0.1s">
                <table>
                  <thead>
                    <tr><th><input type="checkbox" :checked="isAllSelected" @change="toggleSelectAll" /></th><th>Email</th><th>账号密码</th><th>Token状态</th><th>Token文件数</th><th>操作</th></tr>
                  </thead>
                  <tbody>
                    <tr v-for="item in filteredAccounts" :key="`${item.index}-${item.email}`">
                      <td><input type="checkbox" :checked="selectedEmails.includes(item.email)" @change="toggleSelectEmail(item.email)" /></td>
                      <td>{{ item.email }}</td>
                      <td>{{ item.account_password }}</td>
                      <td><span class="status-pill" :class="tokenStatusClass(item.token_status?.status)" :title="`错误码 ${item.token_status?.error_code || '-'} | ${item.token_status?.message || '-'}`">{{ tokenStatusLabel(item.token_status?.status) }}</span></td>
                      <td>{{ item.token_files.length }}</td>
                      <td>
                        <div class="row-actions">
                          <button class="btn" @click="selectAccount(item)">详情</button>
                          <button class="btn btn-detect" :disabled="checkingAllTokens || !!checkingAccounts[item.email]" @click="checkAccountStatus(item.email)">
                            <span v-if="checkingAccounts[item.email]" class="spinner btn-spinner"></span>
                            <span v-else>检测</span>
                          </button>
                          <button class="btn btn-danger" @click="removeAccount(item.email)">删除</button>
                        </div>
                      </td>
                    </tr>
                    <tr v-if="filteredAccounts.length === 0"><td colspan="6" class="empty">暂无账号</td></tr>
                  </tbody>
                </table>
              </div>
            </section>

            <section class="card account-detail" v-if="selectedAccount">
              <h2>账号详情</h2>
              <div class="detail-row"><span>Email</span><strong>{{ selectedAccount.email }}</strong></div>
              <div class="detail-row"><span>账号密码</span><strong>{{ selectedAccount.account_password }}</strong></div>
              <div class="detail-row"><span>邮箱密码</span><strong>{{ selectedAccount.email_password || '-' }}</strong></div>
              <div class="detail-row"><span>OAuth</span><strong>{{ selectedAccount.oauth || '-' }}</strong></div>
              <div class="detail-row"><span>Token状态</span><strong>{{ tokenStatusLabel(selectedAccount.token_status?.status) }}</strong></div>
              <div class="detail-row"><span>状态说明</span><strong>{{ selectedAccount.token_status?.message || '-' }}</strong></div>
              <div class="detail-row"><span>业务码</span><strong>{{ selectedAccount.token_status?.error_code || '-' }}</strong></div>
              <div class="detail-row"><span>HTTP状态码</span><strong>{{ selectedAccount.token_status?.strict_http_status || '-' }}</strong></div>
              <div class="detail-row"><span>过期时间</span><strong>{{ selectedAccount.token_status?.expired_at || '-' }}</strong></div>

              <div class="quota-grid">
                <div class="quota-card">
                  <div class="quota-head">
                    <span>周限额</span>
                    <strong>{{ quotaDisplayLabel(selectedAccount.token_status?.quota?.weekly) }}</strong>
                  </div>
                  <div class="quota-track">
                    <div class="quota-fill" :class="quotaBarClass(quotaDisplayPercent(selectedAccount.token_status?.quota?.weekly))" :style="{ width: quotaDisplayPercent(selectedAccount.token_status?.quota?.weekly) === null ? '0%' : `${quotaDisplayPercent(selectedAccount.token_status?.quota?.weekly)}%` }"></div>
                  </div>
                  <div class="quota-meta">
                    <span>重置: {{ quotaResetLabel(selectedAccount.token_status?.quota?.weekly?.reset_after_seconds || selectedAccount.token_status?.quota?.weekly?.reset_at) }}</span>
                    <span>业务码 {{ selectedAccount.token_status?.quota?.weekly?.error_code || '-' }} / HTTP: {{ selectedAccount.token_status?.quota?.weekly?.http_status || '-' }}</span>
                  </div>
                </div>

                <div class="quota-card">
                  <div class="quota-head">
                    <span>代码审查周限额</span>
                    <strong>{{ quotaDisplayLabel(selectedAccount.token_status?.quota?.code_review_weekly) }}</strong>
                  </div>
                  <div class="quota-track">
                    <div class="quota-fill" :class="quotaBarClass(quotaDisplayPercent(selectedAccount.token_status?.quota?.code_review_weekly))" :style="{ width: quotaDisplayPercent(selectedAccount.token_status?.quota?.code_review_weekly) === null ? '0%' : `${quotaDisplayPercent(selectedAccount.token_status?.quota?.code_review_weekly)}%` }"></div>
                  </div>
                  <div class="quota-meta">
                    <span>重置: {{ quotaResetLabel(selectedAccount.token_status?.quota?.code_review_weekly?.reset_after_seconds || selectedAccount.token_status?.quota?.code_review_weekly?.reset_at) }}</span>
                    <span>业务码 {{ selectedAccount.token_status?.quota?.code_review_weekly?.error_code || '-' }} / HTTP: {{ selectedAccount.token_status?.quota?.code_review_weekly?.http_status || '-' }}</span>
                  </div>
                </div>
              </div>

              <h3>Tokens 文件</h3>
              <ul class="token-list">
                <li v-for="f in selectedAccount.token_files" :key="f.name">
                  <div class="token-row">
                    <button class="btn" @click="openToken(f.name)">{{ f.name }}</button>
                    <span class="status-pill" :class="tokenStatusClass(f.status)" :title="`错误码 ${f.error_code || '-'} | ${f.message || '-'}`">{{ tokenStatusLabel(f.status) }}</span>
                    <button class="btn btn-detect" :disabled="checkingAllTokens || !!checkingTokenFiles[f.name]" @click="checkTokenFileStatus(f.name)">
                      <span v-if="checkingTokenFiles[f.name]" class="spinner btn-spinner"></span>
                      <span v-else>检测</span>
                    </button>
                  </div>
                </li>
                <li v-if="selectedAccount.token_files.length === 0" class="empty">无 token 文件</li>
              </ul>

              <h3>Token 文件内容</h3>
              <pre class="logs" v-if="selectedToken">{{ JSON.stringify(selectedToken, null, 2) }}</pre>
              <p v-else class="empty">{{ tokenLoading ? '加载中...' : '请选择一个 token 文件' }}</p>
            </section>
          </div>
        </template>

        <template v-else-if="page === 'auto-maintain'">
          <section class="card settings-card">
            <h2>自动维护</h2>
            <p v-if="autoMaintainError" class="error">{{ autoMaintainError }}</p>

            <div class="stats auto-stats">
              <div class="stat">
                <span>启用状态</span>
                <strong>{{ autoMaintain.enabled ? '已启用' : '未启用' }}</strong>
              </div>
              <div class="stat">
                <span>运行状态</span>
                <strong>{{ autoMaintain.running ? '运行中' : '空闲' }}</strong>
              </div>
              <div class="stat">
                <span>远端有效账号</span>
                <strong>{{ autoMaintain.remote_valid_count }}</strong>
              </div>
              <div class="stat">
                <span>目标数量</span>
                <strong>{{ autoMaintain.target_count }}</strong>
              </div>
              <div class="stat">
                <span>巡检间隔</span>
                <strong>{{ autoMaintain.interval_seconds }} 秒</strong>
              </div>
              <div class="stat">
                <span>并发数</span>
                <strong>{{ autoMaintain.max_workers }}</strong>
              </div>
            </div>

            <div class="stats auto-meta">
              <div class="stat">
                <span>上次开始</span>
                <strong>{{ formatUnixTime(autoMaintain.last_started_at) }}</strong>
              </div>
              <div class="stat">
                <span>上次结束</span>
                <strong>{{ formatUnixTime(autoMaintain.last_finished_at) }}</strong>
              </div>
            </div>

            <h3>最近错误</h3>
            <p class="empty auto-error-box">{{ autoMaintain.last_error || '无' }}</p>

            <h3>最近日志</h3>
            <div class="auto-log-box">
              <div v-if="autoMaintain.logs.length === 0" class="empty">暂无日志</div>
              <pre v-else>{{ autoMaintain.logs.join('\n') }}</pre>
            </div>
          </section>
        </template>

        <template v-else>
          <section class="card settings-card">
            <h2>检测与配额接口设置</h2>
            <p class="empty" style="text-align: left; margin-bottom: 16px;">
              推送与远程状态检查将直接使用容器环境变量 `CLIPROXY_API_BASE_URL` 与 `CLIPROXY_API_KEY`。
            </p>

            <h3 style="margin-top: 16px;">推送 Codex 认证到 CLIProxyAPI</h3>
            <div class="actions">
              <button class="btn" :disabled="checkingCodexTarget || pushingCodexTokens" @click="checkCodexPushTarget">{{ checkingCodexTarget ? '预检中...' : '先预检并加载 token 列表' }}</button>
            </div>

            <div class="detail-row" style="margin-top:8px;"><span>本地 codex tokens</span><strong>{{ localCodexFiles.length }}</strong></div>
            <div class="detail-row"><span>远端 codex tokens</span><strong>{{ remoteCodexSummary.remote_codex_total }}</strong></div>
            <div class="detail-row"><span>同名已存在</span><strong>{{ remoteCodexSummary.remote_overlap_total }}</strong></div>

            <div class="form-row" style="margin-top:10px;">
              <label>按数量选择（最新 N 个）</label>
              <input v-model.number="codexPushForm.push_count" type="number" min="0" :max="localCodexFiles.length" @change="applyCodexCountSelection" />
            </div>

            <div class="token-selector" v-if="localCodexFiles.length">
              <label class="selector-title">手动勾选文件（可与数量选择叠加调整）</label>
              <div class="token-selector-list">
                <label v-for="f in localCodexFiles" :key="f.name" class="token-item">
                  <input type="checkbox" :checked="selectedCodexFiles.includes(f.name)" @change="toggleCodexFile(f.name)" />
                  <span>{{ f.name }}</span>
                </label>
              </div>
            </div>

            <div class="form-row" style="display:flex; align-items:center; gap:8px; margin-top:8px;">
              <input id="delete_local_after_upload" v-model="codexPushForm.delete_local_after_upload" type="checkbox" />
              <label for="delete_local_after_upload" style="margin:0;">上传成功后删除本地已上传 tokens</label>
            </div>

            <div class="progress-shell" v-if="pushingCodexTokens || codexPushProgress.total > 0">
              <div class="progress-label">
                <span>上传进度 {{ codexPushProgress.done }} / {{ codexPushProgress.total }}</span>
                <span>{{ codexPushPercent() }}%</span>
              </div>
              <div class="progress-track">
                <div class="progress-fill" :style="{ width: `${codexPushPercent()}%` }"></div>
              </div>
            </div>

            <div class="actions" style="display: flex; gap: 8px;">
              <button class="btn btn-primary" style="flex: 1;" :disabled="pushingCodexTokens || checkingCodexTarget" @click="pushCodexTokensToProxy">{{ pushingCodexTokens ? '推送中...' : '推送选中 Codex 认证文件' }}</button>
              <button class="btn" style="flex: 1; background-color: #f59e0b; color: white; border: none;" :disabled="checkingRemoteStatus || pushingCodexTokens" @click="checkRemoteStatus">{{ checkingRemoteStatus ? `检测中 ${remoteCheckProgress.done}/${remoteCheckProgress.total || 0}` : '检测远端状态' }}</button>
            </div>

            <div class="progress-shell" v-if="checkingRemoteStatus || remoteCheckProgress.total > 0" style="margin-top: 8px;">
              <div class="progress-label">
                <span>远端检测进度 {{ remoteCheckProgress.done }} / {{ remoteCheckProgress.total }}</span>
                <span>{{ remoteCheckPercent() }}%</span>
              </div>
              <div class="progress-track">
                <div class="progress-fill" :style="{ width: `${remoteCheckPercent()}%` }"></div>
              </div>
            </div>

            <div v-if="remoteCheckResults.length > 0" style="margin-top: 16px; border: 1px solid var(--border-color); border-radius: 8px; overflow: hidden;">
              <div style="background: var(--bg-card); padding: 8px 12px; border-bottom: 1px solid var(--border-color); display: flex; justify-content: space-between; align-items: center;">
                <span style="font-weight: 500;">远端文件状态</span>
                <button class="btn" style="background-color: #ef4444; color: white; border: none; padding: 4px 12px; font-size: 13px;" :disabled="deletingRemoteFiles" @click="deleteInvalidRemoteFiles" v-if="remoteCheckResults.some(x => Number(x.status_code) !== 200)">
                  {{ deletingRemoteFiles ? '删除中...' : '删除所有非200文件' }}
                </button>
              </div>
              <div style="max-height: 200px; overflow-y: auto;">
                <table class="data-table" style="margin: 0;">
                  <thead>
                    <tr>
                      <th style="padding: 6px 12px;">文件名</th>
                      <th style="padding: 6px 12px; text-align: right;">状态</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr v-for="f in remoteCheckResults" :key="f.name">
                      <td style="padding: 6px 12px; font-family: monospace; font-size: 13px;">{{ f.name }}</td>
                      <td style="padding: 6px 12px; text-align: right; white-space: nowrap;">
                        <span v-if="f.status_code" 
                              style="font-size: 11px; padding: 2px 6px; border-radius: 4px; margin-right: 6px;" 
                              :style="{ backgroundColor: f.status_code === 401 ? '#fee2e2' : '#f3f4f6', color: f.status_code === 401 ? '#ef4444' : '#6b7280' }">
                          HTTP {{ f.status_code }}
                        </span>
                        <span :style="{ color: f.is_invalid ? '#ef4444' : '#10b981', fontSize: '13px' }">{{ f.message }}</span>
                      </td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </div>

            <p class="empty" style="text-align:left; margin-top: 8px;">会先预检管理 Key，失败立即终止，避免重复失败触发封禁。</p>
          </section>
        </template>
      </div>
    </Transition>
  </div>
</template>


