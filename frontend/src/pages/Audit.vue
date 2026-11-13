<script setup>
import { onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { getJSON } from '../api'

const route = useRoute()
const batches = ref([])
const batchId = ref(null)
const summary = ref(null)
const err = ref('')
const show = (v) => (v ? v : '(空)')

async function loadBatches() {
  batches.value = await getJSON('/api/batches?limit=30')
  if (route.query.batch_id) batchId.value = Number(route.query.batch_id)
  else if (!batchId.value && batches.value.length) batchId.value = batches.value[0].id
}

async function loadSummary() {
  summary.value = null
  if (!batchId.value) return
  summary.value = await getJSON(`/api/audit/batches/${batchId.value}`)
}

onMounted(async () => {
  try {
    await loadBatches()
    await loadSummary()
  } catch (e) { err.value = String(e.message || e) }
})
watch(batchId, async () => {
  try { await loadSummary() } catch (e) { err.value = String(e.message || e) }
})
</script>
<template>
  <div class="page">
    <h1>审计摘要</h1>
    <p class="muted">按批次互证：增量行数、对齐结果、水位值与批次报告交叉核对。导出包留给后题；当前 export_ready=false。</p>
    <p v-if="err" class="tag danger">{{ err }}</p>
    <div class="panel">
      <label>
        批次
        <select v-model.number="batchId" style="display:block;margin-top:0.35rem;min-width:20rem">
          <option v-for="b in batches" :key="b.id" :value="b.id">{{ b.batch_no }} · {{ b.pair_title }} · {{ b.status }}</option>
        </select>
      </label>
    </div>
    <div class="panel" v-if="summary">
      <p>
        批次 <span class="mono">{{ summary.batch.batch_no }}</span> · {{ summary.pair?.title }} ·
        状态 <span class="mono">{{ summary.batch.status }}</span>
      </p>
      <p class="mono" style="margin:0.25rem 0">
        水位：左 {{ show(summary.batch.old_left_wm) }} → {{ show(summary.batch.new_left_wm) }} ·
        右 {{ show(summary.batch.old_right_wm) }} → {{ show(summary.batch.new_right_wm) }}
      </p>
      <p style="margin:0.25rem 0">原因：{{ summary.batch.reason }}</p>
      <div class="stat-grid">
        <div class="stat"><div class="stat-num">{{ summary.batch.left_rows }}/{{ summary.batch.right_rows }}</div><div class="stat-label">增量行(左/右)</div></div>
        <div class="stat"><div class="stat-num">{{ summary.diff_count }}</div><div class="stat-label">差异行</div></div>
        <div class="stat"><div class="stat-num">{{ summary.kinds.left_only }}</div><div class="stat-label">仅左有</div></div>
        <div class="stat"><div class="stat-num">{{ summary.kinds.right_only }}</div><div class="stat-label">仅右有</div></div>
        <div class="stat"><div class="stat-num">{{ summary.kinds.field_mismatch }}</div><div class="stat-label">字段不一致</div></div>
      </div>

      <h3>互证核对 <span class="tag" :class="summary.checks_ok ? 'ok' : 'danger'">{{ summary.checks_ok ? '全部通过' : '有不符' }}</span></h3>
      <table>
        <thead>
          <tr><th>核对项</th><th>应为</th><th>实际</th><th>结果</th></tr>
        </thead>
        <tbody>
          <tr v-for="c in summary.checks" :key="c.name">
            <td>{{ c.name }}</td>
            <td class="mono">{{ show(String(c.expect)) }}</td>
            <td class="mono">{{ show(String(c.actual)) }}</td>
            <td><span class="tag" :class="c.ok ? 'ok' : 'danger'">{{ c.ok ? '符' : '不符' }}</span></td>
          </tr>
        </tbody>
      </table>
      <p style="margin-top:0.85rem" class="muted">{{ summary.note }}</p>
      <p>可导出 <span class="tag" :class="summary.export_ready ? 'ok' : 'warn'">{{ summary.export_ready ? '是' : '否' }}</span></p>
    </div>
  </div>
</template>
