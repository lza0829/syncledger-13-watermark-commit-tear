<script setup>
import { onMounted, ref } from 'vue'
import { getJSON } from '../api'
const rows = ref([])
const err = ref('')
const show = (v) => (v ? v : '(空)')
onMounted(async () => {
  try { rows.value = await getJSON('/api/pairs') } catch (e) { err.value = String(e.message || e) }
})
</script>
<template>
  <div class="page">
    <h1>对账对</h1>
    <p class="muted">每对绑定左右夹具、对齐键、比对字段与各自的水位字段；左右水位按对分别维护、同批次提交一起推进。</p>
    <p v-if="err" class="tag danger">{{ err }}</p>
    <div class="panel">
      <table>
        <thead>
          <tr>
            <th>ID</th><th>编码</th><th>标题</th>
            <th>左端</th><th>右端</th><th>对齐键</th><th>比对字段</th>
            <th>水位字段(左/右)</th><th>左水位</th><th>右水位</th><th>水位更新批次</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="p in rows" :key="p.id">
            <td>{{ p.id }}</td>
            <td class="mono">{{ p.code }}</td>
            <td>{{ p.title }}</td>
            <td>{{ p.left_name }}<div class="mono muted">{{ p.left_fixture }}</div></td>
            <td>{{ p.right_name }}<div class="mono muted">{{ p.right_fixture }}</div></td>
            <td class="mono">{{ p.align_keys.join(' + ') }}</td>
            <td class="mono">{{ p.compare_fields.join(', ') }}</td>
            <td class="mono">{{ p.left_wm_field }} / {{ p.right_wm_field }}</td>
            <td class="mono">{{ show(p.watermark.left_watermark) }}</td>
            <td class="mono">{{ show(p.watermark.right_watermark) }}</td>
            <td class="mono">
              <template v-if="p.watermark.updated_by_batch">#{{ p.watermark.updated_by_batch }}</template>
              <template v-else>—</template>
              <div class="mono muted">{{ p.watermark.updated_at || '' }}</div>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
