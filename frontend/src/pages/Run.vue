<script setup>
import { computed, onMounted, ref } from 'vue'
import { getJSON, postJSON } from '../api'

const pairs = ref([])
const batches = ref([])
const pairId = ref(null)
const note = ref('')
const slowExtract = ref(false)
const withMismatch = ref(false)
const last = ref(null)
const demoMsg = ref('')
const busy = ref(false)
const err = ref('')

const STATUS_LABEL = {
  extracting: '抽取中',
  aligning: '对齐中',
  completed: '完成',
  rejected_regression: '回退拒绝',
  rejected_concurrent: '并发拒绝',
  failed: '失败',
}
const STATUS_CLASS = {
  extracting: 'warn',
  aligning: 'warn',
  completed: 'ok',
  rejected_regression: 'danger',
  rejected_concurrent: 'warn',
  failed: 'danger',
}
const statusLabel = (s) => STATUS_LABEL[s] || s
const statusClass = (s) => STATUS_CLASS[s] || ''
const show = (v) => (v ? v : '(空)')

const pair = computed(() => pairs.value.find((p) => p.id === pairId.value) || null)

async function refresh() {
  pairs.value = await getJSON('/api/pairs')
  batches.value = await getJSON('/api/batches?limit=20')
  if (pairId.value == null && pairs.value.length) pairId.value = pairs.value[0].id
}

async function run() {
  err.value = ''
  busy.value = true
  try {
    last.value = await postJSON('/api/reconcile', {
      pair_id: Number(pairId.value),
      note: note.value || null,
      extract_delay_ms: slowExtract.value ? 2000 : 0,
    })
    demoMsg.value = ''
    await refresh()
  } catch (e) {
    err.value = String(e.message || e)
    await refresh()
  } finally {
    busy.value = false
  }
}

async function demo(action) {
  err.value = ''
  demoMsg.value = ''
  busy.value = true
  try {
    const code = pair.value?.code
    if (action === 'advance') {
      const r = await postJSON(`/api/demo/advance/${code}?rows=2&mismatch=${withMismatch.value}`)
      demoMsg.value = `已给双端各追加 ${r.appended} 行（左最大水位 ${r.left_max_wm}，右最大水位 ${r.right_max_wm}），点「发起对账」看增量前进。`
    } else if (action === 'regress') {
      const r = await postJSON(`/api/demo/regress/${code}`)
      demoMsg.value = `源端已回滚到基线（左最大水位 ${r.left_max_wm}，右最大水位 ${r.right_max_wm}），再点「发起对账」将触发回退策略：整趟拒绝、水位不动。`
    } else if (action === 'concurrent') {
      const body = { pair_id: Number(pairId.value), extract_delay_ms: 2000 }
      const [a, b] = await Promise.allSettled([postJSON('/api/reconcile', body), postJSON('/api/reconcile', body)])
      const okOne = [a, b].find((r) => r.status === 'fulfilled')
      const badOne = [a, b].find((r) => r.status === 'rejected')
      demoMsg.value = `并发两趟：${okOne ? `一趟完成（${okOne.value.batch_no}）` : '无成功'}；${badOne ? `一趟被拒（${badOne.reason.message}）` : '无拒绝'}`
      if (okOne) last.value = okOne.value
    } else if (action === 'reset') {
      await postJSON('/api/demo/reset')
      last.value = null
      demoMsg.value = '演示已重置：夹具回基线，批次/差异/水位清空。'
    }
    await refresh()
  } catch (e) {
    err.value = String(e.message || e)
  } finally {
    busy.value = false
  }
}

onMounted(async () => {
  try { await refresh() } catch (e) { err.value = String(e.message || e) }
})
</script>
<template>
  <div class="page">
    <h1>对账台</h1>
    <p class="muted">
      按对维护左右水位，本趟只抽取并对齐水位之后的增量；两端水位在同一次批次提交里一起推进或一起不动。
      回退策略（固定）：整趟拒绝、不落差异、水位不动。
    </p>
    <p v-if="err" class="tag danger">{{ err }}</p>

    <div class="panel">
      <h3 style="margin-top:0">发起对账</h3>
      <div style="display:flex;flex-wrap:wrap;gap:0.75rem;align-items:flex-end">
        <label>
          对账对
          <select v-model.number="pairId" style="display:block;min-width:18rem;margin-top:0.25rem">
            <option v-for="p in pairs" :key="p.id" :value="p.id">{{ p.title }}</option>
          </select>
        </label>
        <label>
          备注（可选）
          <input v-model="note" style="display:block;width:14rem;margin-top:0.25rem" placeholder="本趟说明" />
        </label>
        <label style="display:flex;gap:0.35rem;align-items:center">
          <input type="checkbox" v-model="slowExtract" /> 慢速抽取（演示并发窗口）
        </label>
        <button :disabled="busy || !pairId" @click="run">{{ busy ? '对账中…' : '抽取并对齐' }}</button>
      </div>
      <p v-if="pair" class="mono muted" style="margin-bottom:0">
        当前水位：左 {{ show(pair.watermark.left_watermark) }}（{{ pair.left_wm_field }}）
        · 右 {{ show(pair.watermark.right_watermark) }}（{{ pair.right_wm_field }}）
      </p>
    </div>

    <div class="panel">
      <h3 style="margin-top:0">演示操作</h3>
      <div style="display:flex;flex-wrap:wrap;gap:0.75rem;align-items:center">
        <button :disabled="busy || !pair" @click="demo('advance')">① 追加增量数据</button>
        <label style="display:flex;gap:0.35rem;align-items:center">
          <input type="checkbox" v-model="withMismatch" /> 首行制造字段差
        </label>
        <button :disabled="busy || !pair" @click="demo('regress')">② 源端回滚到基线</button>
        <button :disabled="busy || !pair" @click="demo('concurrent')">③ 并发开跑测试</button>
        <button :disabled="busy" @click="demo('reset')">重置演示</button>
      </div>
      <p v-if="demoMsg" class="tag" style="margin-bottom:0">{{ demoMsg }}</p>
      <p class="muted" style="margin-bottom:0">
        演示路径：先跑一趟建立水位 → ①追加增量后再跑（前进成功）→ ②回滚基线后再跑（回退触发整趟拒绝）→ ③同时开两趟（一趟完成、一趟并发拒绝）。
      </p>
    </div>

    <div class="panel" v-if="last">
      <h3 style="margin-top:0">最近一趟</h3>
      <p>
        批次 <span class="mono">{{ last.batch_no }}</span> · {{ last.pair_title }} ·
        <span class="tag" :class="statusClass(last.status)">{{ statusLabel(last.status) }}</span>
      </p>
      <p class="mono" style="margin:0.25rem 0">
        水位：左 {{ show(last.old_left_wm) }} → {{ show(last.new_left_wm) }} ·
        右 {{ show(last.old_right_wm) }} → {{ show(last.new_right_wm) }}
      </p>
      <p style="margin:0.25rem 0">原因：{{ last.reason }}</p>
      <div class="stat-grid">
        <div class="stat"><div class="stat-num">{{ last.left_rows }}/{{ last.right_rows }}</div><div class="stat-label">增量行(左/右)</div></div>
        <div class="stat"><div class="stat-num">{{ last.matched }}</div><div class="stat-label">一致</div></div>
        <div class="stat"><div class="stat-num">{{ last.left_only }}</div><div class="stat-label">仅左有</div></div>
        <div class="stat"><div class="stat-num">{{ last.right_only }}</div><div class="stat-label">仅右有</div></div>
        <div class="stat"><div class="stat-num">{{ last.field_mismatch }}</div><div class="stat-label">字段不一致</div></div>
        <div class="stat"><div class="stat-num">{{ last.diff_total }}</div><div class="stat-label">差异合计</div></div>
      </div>
      <p style="margin-top:0.75rem">
        <router-link :to="{ path: '/diffs', query: { batch_id: last.id } }">查看本批差异 →</router-link>
        ·
        <router-link :to="{ path: '/audit', query: { batch_id: last.id } }">审计互证 →</router-link>
      </p>
    </div>

    <div class="panel">
      <h3 style="margin-top:0">历史批次</h3>
      <table>
        <thead>
          <tr>
            <th>批次</th><th>对账对</th><th>状态</th><th>模式</th>
            <th>增量行(左/右)</th><th>差异</th><th>水位(旧→新)</th><th>原因</th><th>时间</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="b in batches" :key="b.id">
            <td class="mono">{{ b.batch_no }}</td>
            <td>{{ b.pair_title }}</td>
            <td><span class="tag" :class="statusClass(b.status)">{{ statusLabel(b.status) }}</span></td>
            <td class="mono">{{ b.mode || '整表(旧)' }}</td>
            <td class="mono">{{ b.left_rows }} / {{ b.right_rows }}</td>
            <td>{{ b.diff_total }}</td>
            <td class="mono">
              左 {{ show(b.old_left_wm) }}→{{ show(b.new_left_wm) }}<br />
              右 {{ show(b.old_right_wm) }}→{{ show(b.new_right_wm) }}
            </td>
            <td style="max-width:22rem">{{ b.reason }}</td>
            <td class="mono">{{ b.created_at }}</td>
          </tr>
          <tr v-if="!batches.length"><td colspan="9" class="muted">还没有批次，先跑一趟。</td></tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
