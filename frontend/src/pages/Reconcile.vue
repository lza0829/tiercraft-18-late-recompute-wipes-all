<script setup>
import { computed, onMounted, ref } from 'vue'
import { getJson, postJson } from '../api'

const domains = ref([])
const domainId = ref(1)
const reports = ref([])
const lateDays = ref([])
const err = ref('')
const busy = ref(false)

const pendingDays = computed(() => lateDays.value.filter((d) => d.status === '待重算'))

async function refresh() {
  const [r, l] = await Promise.all([
    getJson(`/api/reconcile?domain_id=${domainId.value}`),
    getJson(`/api/late-days?domain_id=${domainId.value}`),
  ])
  reports.value = r
  lateDays.value = l
}

onMounted(async () => {
  try {
    domains.value = await getJson('/api/domains')
    if (domains.value.length) domainId.value = domains.value[0].id
    await refresh()
  } catch (e) {
    err.value = String(e.message || e)
  }
})

async function run() {
  busy.value = true
  err.value = ''
  try {
    await postJson('/api/reconcile', { domain_id: domainId.value })
    await refresh()
  } catch (e) {
    err.value = String(e.message || e)
  } finally {
    busy.value = false
  }
}

async function recompute() {
  busy.value = true
  err.value = ''
  try {
    await postJson('/api/jobs/late-recompute', { domain_id: domainId.value })
    await refresh()
  } catch (e) {
    err.value = String(e.message || e)
  } finally {
    busy.value = false
  }
}
</script>
<template>
  <div>
    <h2>层间对账</h2>
    <p class="lead">核对明细行数与汇总行数合计、金额是否一致。存在迟到业务日时，应先定点重算受影响日再对账。</p>
    <div class="toolbar">
      <label>
        主题域
        <select v-model.number="domainId" @change="refresh">
          <option v-for="d in domains" :key="d.id" :value="d.id">{{ d.name }}</option>
        </select>
      </label>
      <button :disabled="busy" @click="run">生成对账报告</button>
      <button class="accent-2" :disabled="busy || !pendingDays.length" @click="recompute">
        定点重算迟到日（{{ pendingDays.length }}）
      </button>
    </div>
    <p v-if="err" class="err">{{ err }}</p>

    <div class="panel">
      <h3>迟到业务日标记</h3>
      <table v-if="lateDays.length">
        <thead>
          <tr><th>业务日</th><th>状态</th><th>说明</th><th>更新时间</th></tr>
        </thead>
        <tbody>
          <tr v-for="d in lateDays" :key="d.id">
            <td>{{ d.biz_day }}</td>
            <td>
              <span :class="d.status === '待重算' ? 'badge badge-late' : 'badge badge-done'">{{ d.status }}</span>
            </td>
            <td class="note-cell">{{ d.note }}</td>
            <td>{{ d.updated_at }}</td>
          </tr>
        </tbody>
      </table>
      <p v-else class="muted">当前主题域没有迟到业务日。</p>
    </div>

    <div class="panel" v-for="r in reports" :key="r.id">
      <h3>
        报告 {{ r.id }}
        <span :class="r.status === '一致' ? 'ok' : 'warn'"> · {{ r.status }}</span>
      </h3>
      <table>
        <tbody>
          <tr><th>贴源行数</th><td>{{ r.detail.贴源行数 }}</td></tr>
          <tr><th>明细行数</th><td>{{ r.detail.明细行数 }}</td></tr>
          <tr><th>汇总行数合计</th><td>{{ r.detail.汇总行数合计 }}</td></tr>
          <tr><th>明细金额合计</th><td>{{ r.detail.明细金额合计 }}</td></tr>
          <tr><th>汇总金额合计</th><td>{{ r.detail.汇总金额合计 }}</td></tr>
          <tr><th>键对齐</th><td>{{ r.detail.键对齐 ? '是' : '否' }}</td></tr>
          <tr><th>金额漂移</th><td>{{ r.detail.金额漂移 }}</td></tr>
          <tr><th>生成时间</th><td>{{ r.created_at }}</td></tr>
        </tbody>
      </table>
    </div>
    <p v-if="!reports.length" class="muted">暂无对账报告。</p>
  </div>
</template>
