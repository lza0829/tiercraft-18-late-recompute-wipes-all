<script setup>
import { computed, onMounted, ref } from 'vue'
import { getJson, postJson } from '../api'

const domains = ref([])
const domainId = ref(1)
const jobs = ref([])
const lateDays = ref([])
const recomputeJobs = ref([])
const err = ref('')
const busy = ref(false)

const pendingDays = computed(() => lateDays.value.filter((d) => d.status === '待重算'))

// 贴源追加表单（用于模拟晚到的贴源新行）
const form = ref({ biz_day: '', store_code: '', store_name: '', category: '', amount: 0, qty: 0 })

async function refresh() {
  const [j, l, r] = await Promise.all([
    getJson(`/api/jobs?domain_id=${domainId.value}`),
    getJson(`/api/late-days?domain_id=${domainId.value}`),
    getJson(`/api/recompute-jobs?domain_id=${domainId.value}`),
  ])
  jobs.value = j
  lateDays.value = l
  recomputeJobs.value = r
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

async function run(step) {
  busy.value = true
  err.value = ''
  try {
    const url = step === '明细'
      ? '/api/jobs/paste-to-detail'
      : '/api/jobs/detail-to-summary'
    await postJson(url, { domain_id: domainId.value })
    await refresh()
  } catch (e) {
    err.value = String(e.message || e)
  } finally {
    busy.value = false
  }
}

async function recompute(days = null) {
  busy.value = true
  err.value = ''
  try {
    await postJson('/api/jobs/late-recompute', { domain_id: domainId.value, biz_days: days })
    await refresh()
  } catch (e) {
    err.value = String(e.message || e)
  } finally {
    busy.value = false
  }
}

async function rescan() {
  busy.value = true
  err.value = ''
  try {
    await postJson('/api/late-days/rescan', { domain_id: domainId.value })
    await refresh()
  } catch (e) {
    err.value = String(e.message || e)
  } finally {
    busy.value = false
  }
}

async function appendPaste() {
  busy.value = true
  err.value = ''
  try {
    const res = await postJson('/api/layers/paste', { domain_id: domainId.value, ...form.value })
    form.value = { biz_day: '', store_code: '', store_name: '', category: '', amount: 0, qty: 0 }
    await refresh()
    if (res.迟到) err.value = ''
  } catch (e) {
    err.value = String(e.message || e)
  } finally {
    busy.value = false
  }
}

function fmtAmount(v) {
  return Number(v).toFixed(2)
}
</script>
<template>
  <div>
    <h2>分层任务</h2>
    <p class="lead">按主题域推进：先贴源到明细，再明细到汇总。贴源新行晚于某业务日最近一次成功明细任务时，该业务日会被标为迟到，可只定点重算受影响业务日，不清空全表。</p>
    <div class="toolbar">
      <label>
        主题域
        <select v-model.number="domainId" @change="refresh">
          <option v-for="d in domains" :key="d.id" :value="d.id">{{ d.name }}</option>
        </select>
      </label>
      <button :disabled="busy" @click="run('明细')">跑贴源到明细（全量）</button>
      <button class="secondary" :disabled="busy" @click="run('汇总')">跑明细到汇总（全量）</button>
      <button class="secondary" :disabled="busy" @click="rescan">重新扫描迟到日</button>
    </div>
    <p v-if="err" class="err">{{ err }}</p>

    <div class="panel">
      <h3>
        迟到业务日
        <span v-if="pendingDays.length" class="badge badge-late">待重算 {{ pendingDays.length }} 日</span>
      </h3>
      <div class="toolbar">
        <button class="accent-2" :disabled="busy || !pendingDays.length" @click="recompute(null)">
          定点重算全部迟到日（{{ pendingDays.length }}）
        </button>
      </div>
      <table v-if="lateDays.length">
        <thead>
          <tr>
            <th>业务日</th><th>状态</th><th>触发贴源行</th><th>最近明细任务</th>
            <th>说明</th><th>更新时间</th><th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="d in lateDays" :key="d.id">
            <td>{{ d.biz_day }}</td>
            <td>
              <span :class="d.status === '待重算' ? 'badge badge-late' : 'badge badge-done'">{{ d.status }}</span>
            </td>
            <td>{{ d.trigger_paste_id ?? '—' }}</td>
            <td>{{ d.last_detail_job_id ?? '—' }}</td>
            <td class="note-cell">{{ d.note }}</td>
            <td>{{ d.updated_at }}</td>
            <td>
              <button
                v-if="d.status === '待重算'"
                class="secondary small"
                :disabled="busy"
                @click="recompute([d.biz_day])"
              >只重算该日</button>
              <span v-else class="muted">已收敛</span>
            </td>
          </tr>
        </tbody>
      </table>
      <p v-else class="muted">暂无迟到业务日。在下方追加一条已加工业务日的贴源新行即可模拟迟到。</p>
    </div>

    <div class="panel">
      <h3>定点重算报告</h3>
      <div v-for="job in recomputeJobs" :key="job.id" class="recompute-card">
        <div class="recompute-head">
          <strong>报告 {{ job.id }}</strong>
          <span class="muted">业务日数 {{ job.day_count }}</span>
          <span :class="job.consistent ? 'ok' : 'warn'">
            {{ job.consistent ? '与分层表一致' : '存在差异' }}
          </span>
          <span class="muted">{{ job.created_at }}</span>
        </div>
        <table>
          <thead>
            <tr>
              <th>业务日</th><th>明细行数（重算前 → 后）</th><th>汇总金额（重算前 → 后）</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="line in job.days" :key="line.id">
              <td>{{ line.biz_day }}</td>
              <td>{{ line.detail_before }} → {{ line.detail_after }}</td>
              <td>{{ fmtAmount(line.summary_amount_before) }} → {{ fmtAmount(line.summary_amount_after) }}</td>
            </tr>
          </tbody>
        </table>
        <p class="muted small-note">
          合计：明细 {{ job.detail_before }} → {{ job.detail_after }} 行；
          汇总金额 {{ fmtAmount(job.summary_amount_before) }} → {{ fmtAmount(job.summary_amount_after) }}
        </p>
      </div>
      <p v-if="!recomputeJobs.length" class="muted">暂无定点重算报告。</p>
    </div>

    <div class="panel">
      <h3>追加贴源新行（模拟晚到数据）</h3>
      <p class="muted small-note">
        若该业务日已成功跑过明细，且新行装载时间晚于其明细水位线，将自动登记为迟到；未加工过的业务日只视为待加工，不算迟到。
      </p>
      <div class="form-grid">
        <label>业务日<input v-model="form.biz_day" placeholder="2026-11-10"></label>
        <label>门店编码<input v-model="form.store_code" placeholder="S01"></label>
        <label>门店名称<input v-model="form.store_name" placeholder="城东店"></label>
        <label>品类<input v-model="form.category" placeholder="饮料"></label>
        <label>金额<input v-model.number="form.amount" type="number" step="0.01"></label>
        <label>件数<input v-model.number="form.qty" type="number"></label>
      </div>
      <div class="toolbar">
        <button :disabled="busy" @click="appendPaste">追加并检测迟到</button>
      </div>
    </div>

    <div class="panel">
      <h3>分层任务流水</h3>
      <table>
        <thead>
          <tr><th>编号</th><th>步骤</th><th>状态</th><th>说明</th><th>输入</th><th>写入</th><th>剔除</th><th>时间</th></tr>
        </thead>
        <tbody>
          <tr v-for="j in jobs" :key="j.id">
            <td>{{ j.id }}</td>
            <td>
              {{ j.step_label || j.step }}
              <span v-if="j.step === 'late_recompute'" class="badge badge-late">定点</span>
            </td>
            <td>{{ j.status }}</td>
            <td class="note-cell">{{ j.message }}</td>
            <td>{{ j.in_count }}</td>
            <td>{{ j.out_count }}</td>
            <td>{{ j.drop_count }}</td>
            <td>{{ j.created_at }}</td>
          </tr>
        </tbody>
      </table>
      <p v-if="!jobs.length" class="muted">还没有任务记录。</p>
    </div>
  </div>
</template>
