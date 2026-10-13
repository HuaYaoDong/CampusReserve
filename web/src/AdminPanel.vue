<script setup lang="ts">
import { computed, onMounted, reactive, ref } from "vue";

type Role = "STUDENT" | "VENUE_ADMIN" | "PLATFORM_ADMIN";
type User = { display_name: string; role: Role };
type Venue = { id: number; name: string; address: string; notice: string | null; status: string };
type Court = { id: number; code: string; name: string; status: string; facilities: Record<string, unknown> };
type Template = { id: number; name: string; weekday_mask: number; valid_from: string; valid_to: string | null; status: string };
type Exception = { id: number; exception_date: string; court_id: number | null; type: string; reason: string };
type Batch = { id: number; service_date: string; release_at: string; status: string; slot_count: number };
type Overview = { venue: Venue; courts: Court[]; templates: Template[]; exceptions: Exception[]; batches: Batch[] };
type Preview = { service_date: string; release_at: string; slot_count: number; court_count: number; existing_batch_id: number | null; batch: Batch | null };
type Booking = { id: number; booking_no: string; username: string; display_name: string; status: string; service_date: string; courts: string; start_at: string; end_at: string; checked_in_at: string | null };
type Audit = { id: number; action: string; actor_id: number | null; resource_type: string; resource_id: string; created_at: string };

const props = defineProps<{ token: string; user: User }>();
const emit = defineEmits<{ logout: [] }>();
const overview = ref<Overview | null>(null);
const preview = ref<Preview | null>(null);
const bookings = ref<Booking[]>([]);
const audits = ref<Audit[]>([]);
const busy = ref("");
const message = ref("");
const error = ref("");
const section = ref("overview");
const sectionLabels: Record<string, string> = { overview: "管理总览", venue: "场馆与场地", rules: "开放规则", batches: "库存与放号", bookings: "预约与签到", audit: "审计日志" };
const courtFacilitiesDraft = reactive<Record<number, string>>({});
const venueForm = reactive({ name: "", address: "", notice: "", status: "ACTIVE" });
const templateForm = reactive({ name: "", weekday_mask: 127, valid_from: "", valid_to: "", status: "DRAFT" });
const exceptionForm = reactive({ exception_date: "", court_id: "", type: "CLOSED", custom_open_time: "", custom_close_time: "", reason: "" });
const generationDate = ref("");
const bookingFilter = reactive({ booking_no: "", username: "", service_date: "", court_id: "" });
const checkInNote = ref("");
const isPlatform = computed(() => props.user.role === "PLATFORM_ADMIN");
const openCount = computed(() => overview.value?.batches.filter((item) => item.status === "OPEN").length ?? 0);

async function api<T>(path: string, method = "GET", body?: unknown): Promise<T> {
  const response = await fetch(`/api/v1/admin${path}`, {
    method,
    headers: { Authorization: `Bearer ${props.token}`, ...(body === undefined ? {} : { "Content-Type": "application/json" }) },
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: "no-store",
  });
  if (response.status === 401) {
    emit("logout");
    throw new Error("登录已过期，请重新登录");
  }
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new Error(data.detail?.message ?? `操作失败（${response.status}）`);
  }
  return (await response.json()) as T;
}

async function run(label: string, action: () => Promise<void>) {
  busy.value = label;
  message.value = "";
  error.value = "";
  try {
    await action();
    message.value = `${label}成功`;
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : "操作失败";
  } finally {
    busy.value = "";
  }
}

async function loadOverview() {
  overview.value = await api<Overview>("/overview");
  Object.assign(venueForm, {
    name: overview.value.venue.name,
    address: overview.value.venue.address,
    notice: overview.value.venue.notice ?? "",
    status: overview.value.venue.status,
  });
  for (const court of overview.value.courts) courtFacilitiesDraft[court.id] = JSON.stringify(court.facilities);
}

function saveVenue() {
  void run("保存场馆", async () => {
    await api("/venue", "PATCH", { ...venueForm });
    await loadOverview();
  });
}

function saveCourt(court: Court) {
  void run("保存场地", async () => {
    const facilities = JSON.parse(courtFacilitiesDraft[court.id] || "{}") as Record<string, unknown>;
    if (Array.isArray(facilities) || facilities === null || typeof facilities !== "object") throw new Error("设施信息必须是 JSON 对象");
    await api(`/courts/${court.id}`, "PATCH", { name: court.name, status: court.status, facilities });
    await loadOverview();
  });
}

function saveTemplateEdit(item: Template) {
  void run("更新模板", async () => {
    await api(`/opening-templates/${item.id}`, "PATCH", {
      name: item.name, weekday_mask: item.weekday_mask,
      valid_from: item.valid_from, valid_to: item.valid_to || null,
    });
    await loadOverview();
  });
}

function saveTemplate() {
  void run("保存模板", async () => {
    const payload = { ...templateForm, valid_to: templateForm.valid_to || null };
    await api("/opening-templates", "POST", payload);
    templateForm.name = "";
    await loadOverview();
  });
}

function setTemplateStatus(item: Template, status: string) {
  void run("更新模板", async () => {
    await api(`/opening-templates/${item.id}`, "PATCH", { status });
    await loadOverview();
  });
}

function saveException() {
  void run("保存例外日期", async () => {
    await api("/calendar-exceptions", "PUT", {
      exception_date: exceptionForm.exception_date,
      court_id: exceptionForm.court_id ? Number(exceptionForm.court_id) : null,
      type: exceptionForm.type,
      custom_open_time: exceptionForm.type === "CUSTOM_HOURS" ? exceptionForm.custom_open_time : null,
      custom_close_time: exceptionForm.type === "CUSTOM_HOURS" ? exceptionForm.custom_close_time : null,
      reason: exceptionForm.reason,
    });
    await loadOverview();
  });
}

function generate(previewOnly: boolean) {
  void run(previewOnly ? "预览库存" : "生成库存", async () => {
    preview.value = await api<Preview>("/slot-generation-jobs", "POST", { service_date: generationDate.value, preview_only: previewOnly });
    if (!previewOnly) await loadOverview();
  });
}

function batchAction(batch: Batch, action: string) {
  const labels: Record<string, string> = { publish: "发布", open: "放号", pause: "暂停", resume: "恢复", close: "关闭" };
  const label = action === "open" && batch.status === "OPEN" ? "同步库存" : labels[action] ?? action;
  if (action === "close" && !window.confirm(`确认关闭 ${batch.service_date} 的批次？`)) return;
  void run(label === "同步库存" ? label : `${label}批次`, async () => {
    await api(`/release-batches/${batch.id}/${action}`, "POST");
    await loadOverview();
  });
}

function searchBookings() {
  void run("查询预约", async () => {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(bookingFilter)) if (value) query.set(key, value);
    bookings.value = await api<Booking[]>(`/bookings?${query}`);
  });
}

function checkIn(booking: Booking) {
  void run("现场签到", async () => {
    await api(`/bookings/${booking.id}/check-in`, "POST", { note: checkInNote.value || null });
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(bookingFilter)) if (value) query.set(key, value);
    bookings.value = await api<Booking[]>(`/bookings?${query}`);
  });
}

function loadAudits() {
  void run("读取审计记录", async () => { audits.value = await api<Audit[]>("/audit-logs"); });
}

onMounted(() => { void run("加载管理数据", loadOverview); });
</script>

<template>
  <main class="admin-page">
    <header class="admin-topbar">
      <div class="admin-brand"><span>CR</span><strong>CampusReserve 管理台</strong></div>
      <div class="admin-account"><small>{{ user.role === "PLATFORM_ADMIN" ? "平台管理员" : "场馆管理员" }}</small><strong>{{ user.display_name }}</strong><button type="button" @click="emit('logout')">退出</button></div>
    </header>

    <div class="admin-shell">
      <aside class="admin-nav" aria-label="管理导航">
        <button v-for="item in [['overview','总览'],['venue','场馆与场地'],['rules','开放规则'],['batches','库存与放号'],['bookings','预约与签到'],['audit','审计日志']]" :key="item[0]" type="button" :class="{ active: section === item[0] }" @click="section = item[0]; if (item[0] === 'audit') loadAudits(); if (item[0] === 'bookings') searchBookings()">{{ item[1] }}</button>
      </aside>

      <div class="admin-main">
        <div class="admin-heading"><div><p class="eyebrow">场馆运营</p><h1>{{ sectionLabels[section] }}</h1></div><button type="button" class="secondary" :disabled="!!busy" @click="run('刷新数据', loadOverview)">刷新数据</button></div>
        <p v-if="error" class="admin-alert error" role="alert">{{ error }}</p>
        <p v-if="message" class="admin-alert success" role="status">{{ message }}</p>
        <p v-if="!overview" class="admin-empty">正在加载管理数据…</p>

        <template v-if="overview">
          <section v-if="section === 'overview'" class="admin-grid">
            <article class="admin-card stat"><span>场馆状态</span><strong>{{ overview.venue.status }}</strong><small>{{ overview.venue.name }}</small></article>
            <article class="admin-card stat"><span>场地数量</span><strong>{{ overview.courts.length }}</strong><small>{{ overview.courts.filter(c => c.status === 'ACTIVE').length }} 片启用</small></article>
            <article class="admin-card stat"><span>开放批次</span><strong>{{ openCount }}</strong><small>最近 {{ overview.batches.length }} 个批次</small></article>
            <article class="admin-card wide"><h2>最近放号批次</h2><div class="table-scroll"><table><thead><tr><th>预约日期</th><th>时段数</th><th>放号时间</th><th>状态</th></tr></thead><tbody><tr v-for="batch in overview.batches.slice(0, 8)" :key="batch.id"><td>{{ batch.service_date }}</td><td>{{ batch.slot_count }}</td><td>{{ new Date(batch.release_at).toLocaleString('zh-CN') }}</td><td><span class="pill">{{ batch.status }}</span></td></tr></tbody></table></div></article>
          </section>

          <section v-if="section === 'venue'" class="admin-stack">
            <article class="admin-card"><h2>场馆资料</h2><form class="admin-form" @submit.prevent="saveVenue"><label>名称<input v-model="venueForm.name" required /></label><label>地址<input v-model="venueForm.address" /></label><label>状态<select v-model="venueForm.status"><option value="ACTIVE">开放</option><option value="CLOSED">关闭</option><option value="MAINTENANCE">维护</option></select></label><label class="wide-field">公告<textarea v-model="venueForm.notice" rows="3" /></label><button type="submit" :disabled="!!busy">保存场馆</button></form></article>
            <article class="admin-card"><h2>场地管理</h2><div class="court-list"><div v-for="court in overview.courts" :key="court.id" class="court-row"><strong>{{ court.code }}</strong><input v-model="court.name" :aria-label="`${court.code} 名称`" /><select v-model="court.status" :aria-label="`${court.code} 状态`"><option value="ACTIVE">启用</option><option value="DISABLED">停用</option><option value="MAINTENANCE">维护</option></select><input v-model="courtFacilitiesDraft[court.id]" :aria-label="`${court.code} 设施 JSON`" placeholder="设施 JSON" /><button type="button" :disabled="!!busy" @click="saveCourt(court)">保存</button></div></div></article>
          </section>

          <section v-if="section === 'rules'" class="admin-stack">
            <article class="admin-card"><h2>开放模板</h2><p class="hint">固定 08:00—22:00，每小时一场，提前 1 天 08:00 放号。平台管理员可以维护模板。</p><div class="table-scroll"><table><thead><tr><th>名称</th><th>开始日期</th><th>结束日期</th><th>星期掩码</th><th>状态</th><th>操作</th></tr></thead><tbody><tr v-for="item in overview.templates" :key="item.id"><td><input v-if="isPlatform" v-model="item.name" class="table-input" /><span v-else>{{ item.name }}</span></td><td><input v-if="isPlatform" v-model="item.valid_from" class="table-input" type="date" /><span v-else>{{ item.valid_from }}</span></td><td><input v-if="isPlatform" v-model="item.valid_to" class="table-input" type="date" /><span v-else>{{ item.valid_to ?? '长期' }}</span></td><td><input v-if="isPlatform" v-model.number="item.weekday_mask" class="table-input" type="number" min="1" max="127" /><span v-else>{{ item.weekday_mask }}</span></td><td>{{ item.status }}</td><td class="actions"><button v-if="isPlatform" type="button" :disabled="!!busy" @click="saveTemplateEdit(item)">保存</button><button v-if="isPlatform && item.status !== 'ACTIVE'" type="button" :disabled="!!busy" @click="setTemplateStatus(item, 'ACTIVE')">启用</button><button v-if="isPlatform && item.status === 'ACTIVE'" type="button" :disabled="!!busy" @click="setTemplateStatus(item, 'ARCHIVED')">归档</button></td></tr></tbody></table></div><form v-if="isPlatform" class="admin-form" @submit.prevent="saveTemplate"><label>模板名称<input v-model="templateForm.name" required /></label><label>开始日期<input v-model="templateForm.valid_from" type="date" required /></label><label>结束日期<input v-model="templateForm.valid_to" type="date" /></label><label>星期掩码（1-127）<input v-model.number="templateForm.weekday_mask" type="number" min="1" max="127" required /></label><label>状态<select v-model="templateForm.status"><option value="DRAFT">草稿</option><option value="ACTIVE">启用</option></select></label><button type="submit" :disabled="!!busy">创建模板</button></form></article>
            <article class="admin-card"><h2>例外日期</h2><p class="hint">可设置全馆或单片场地关闭、临时调整开放时间。已生成库存的日期不能修改。</p><form class="admin-form" @submit.prevent="saveException"><label>日期<input v-model="exceptionForm.exception_date" type="date" required /></label><label>范围<select v-model="exceptionForm.court_id"><option value="">整个场馆</option><option v-for="court in overview.courts" :key="court.id" :value="String(court.id)">{{ court.name }}</option></select></label><label>类型<select v-model="exceptionForm.type"><option value="CLOSED">关闭</option><option value="CUSTOM_HOURS">自定义开放时间</option></select></label><template v-if="exceptionForm.type === 'CUSTOM_HOURS'"><label>开放时间<input v-model="exceptionForm.custom_open_time" type="time" required /></label><label>关闭时间<input v-model="exceptionForm.custom_close_time" type="time" required /></label></template><label>原因<input v-model="exceptionForm.reason" required /></label><button type="submit" :disabled="!!busy">保存例外日期</button></form><p v-for="item in overview.exceptions" :key="item.id" class="exception-item">{{ item.exception_date }} · {{ item.court_id ? `场地 ${item.court_id}` : '全馆' }} · {{ item.type }} · {{ item.reason }}</p></article>
          </section>

          <section v-if="section === 'batches'" class="admin-stack">
            <article class="admin-card"><h2>生成时段</h2><p class="hint">先预览，再生成草稿，发布后在放号时间开放。</p><div class="inline-form"><input v-model="generationDate" type="date" aria-label="预约日期" /><button type="button" :disabled="!!busy || !generationDate" @click="generate(true)">预览</button><button type="button" :disabled="!!busy || !generationDate" @click="generate(false)">生成草稿</button></div><p v-if="preview" class="preview">{{ preview.service_date }}：{{ preview.court_count }} 片场地，{{ preview.slot_count }} 个时段；{{ preview.existing_batch_id ? `已有批次 #${preview.existing_batch_id}` : '尚未生成' }}</p></article>
            <article class="admin-card"><h2>批次操作</h2><div class="table-scroll"><table><thead><tr><th>日期</th><th>批次</th><th>库存</th><th>状态</th><th>操作</th></tr></thead><tbody><tr v-for="batch in overview.batches" :key="batch.id"><td>{{ batch.service_date }}</td><td>#{{ batch.id }}</td><td>{{ batch.slot_count }}</td><td><span class="pill">{{ batch.status }}</span></td><td class="actions"><button v-if="batch.status === 'DRAFT'" type="button" :disabled="!!busy" @click="batchAction(batch, 'publish')">发布</button><button v-if="batch.status === 'READY'" type="button" :disabled="!!busy" @click="batchAction(batch, 'open')">放号</button><button v-if="batch.status === 'OPEN'" type="button" :disabled="!!busy" @click="batchAction(batch, 'pause')">暂停</button><button v-if="batch.status === 'PAUSED'" type="button" :disabled="!!busy" @click="batchAction(batch, 'resume')">恢复</button><button v-if="batch.status !== 'DRAFT' && batch.status !== 'CLOSED'" type="button" :disabled="!!busy" @click="batchAction(batch, 'close')">关闭</button></td></tr></tbody></table></div></article>
          </section>

          <section v-if="section === 'batches' && overview.batches.some(batch => batch.status === 'OPEN')" class="admin-card admin-repair">
            <h2>库存同步</h2>
            <p class="hint">若放号后提示 Redis 库存未就绪，可对开放批次重新同步。</p>
            <button v-for="batch in overview.batches.filter(batch => batch.status === 'OPEN')" :key="batch.id" type="button" :disabled="!!busy" @click="batchAction(batch, 'open')">同步 {{ batch.service_date }} 库存</button>
          </section>

          <section v-if="section === 'bookings'" class="admin-stack"><article class="admin-card"><h2>现场预约查询</h2><form class="admin-form" @submit.prevent="searchBookings"><label>预约编号<input v-model="bookingFilter.booking_no" /></label><label>用户账号<input v-model="bookingFilter.username" /></label><label>使用日期<input v-model="bookingFilter.service_date" type="date" /></label><label>场地<select v-model="bookingFilter.court_id"><option value="">全部</option><option v-for="court in overview.courts" :key="court.id" :value="String(court.id)">{{ court.name }}</option></select></label><button type="submit" :disabled="!!busy">查询</button></form><label class="note-field">签到备注<input v-model="checkInNote" placeholder="选填" /></label><div class="table-scroll"><table><thead><tr><th>预约编号</th><th>用户</th><th>日期 / 场地</th><th>时间</th><th>状态</th><th>操作</th></tr></thead><tbody><tr v-for="booking in bookings" :key="booking.id"><td>{{ booking.booking_no }}</td><td>{{ booking.display_name }}<small>{{ booking.username }}</small></td><td>{{ booking.service_date }}<small>{{ booking.courts }}</small></td><td>{{ new Date(booking.start_at).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }) }}—{{ new Date(booking.end_at).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }) }}</td><td>{{ booking.status }}</td><td><button v-if="booking.status === 'CONFIRMED'" type="button" :disabled="!!busy" @click="checkIn(booking)">签到</button><span v-else-if="booking.checked_in_at">已签到</span></td></tr></tbody></table><p v-if="!bookings.length" class="hint">没有匹配的预约。</p></div></article></section>

          <section v-if="section === 'audit'" class="admin-card"><h2>管理员操作审计</h2><div class="table-scroll"><table><thead><tr><th>时间</th><th>操作</th><th>资源</th><th>操作人 ID</th></tr></thead><tbody><tr v-for="item in audits" :key="item.id"><td>{{ new Date(item.created_at).toLocaleString('zh-CN') }}</td><td>{{ item.action }}</td><td>{{ item.resource_type }} #{{ item.resource_id }}</td><td>{{ item.actor_id ?? '系统' }}</td></tr></tbody></table></div></section>
        </template>
      </div>
    </div>
  </main>
</template>

<style scoped>
.admin-page{min-height:100vh;background:#f3f7f5;color:#193a33}.admin-topbar{height:70px;background:#fff;border-bottom:1px solid #dce8e4;display:flex;align-items:center;justify-content:space-between;padding:0 4vw}.admin-brand,.admin-account{display:flex;align-items:center;gap:12px}.admin-brand span{display:grid;place-items:center;width:38px;height:38px;border-radius:10px;background:#117a64;color:white;font-weight:900}.admin-account small{color:#738a82}.admin-account button,.secondary{border:1px solid #cbdcd6;border-radius:9px;background:#fff;color:#315b4f;padding:9px 13px}.admin-shell{max-width:1480px;margin:auto;display:grid;grid-template-columns:200px minmax(0,1fr);gap:28px;padding:30px 4vw}.admin-nav{display:flex;flex-direction:column;gap:7px}.admin-nav button{border:0;border-radius:10px;background:transparent;text-align:left;padding:13px 15px;color:#46645b;font-weight:700}.admin-nav button.active{background:#dff2eb;color:#0e6b55}.admin-main{min-width:0}.admin-heading{display:flex;justify-content:space-between;align-items:end;margin:0 0 25px}.admin-heading h1{font-size:32px}.admin-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}.admin-card{background:#fff;border:1px solid #dce8e4;border-radius:16px;padding:24px;box-shadow:0 10px 30px #204f4010}.admin-card h2{font-size:20px;margin-bottom:20px}.admin-card.wide{grid-column:1/-1}.stat{display:grid;gap:8px}.stat span,.stat small,.hint{color:#6b8078}.stat strong{font-size:29px;color:#126e57}.admin-stack{display:grid;gap:18px}.admin-form{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:15px;align-items:end}.admin-form label,.note-field{display:grid;gap:7px;font-size:13px;font-weight:700;color:#4b665c}.admin-form .wide-field{grid-column:1/-1}.admin-form input,.admin-form select,.admin-form textarea,.inline-form input,.court-row input,.court-row select,.note-field input,.table-input{width:100%;margin:0;padding:10px 11px;border:1px solid #cbdcd6;border-radius:8px;background:white;color:#193a33;font:inherit}.admin-form button,.inline-form button,.court-row button,td button{justify-self:start;border:0;border-radius:8px;padding:10px 14px;background:#137c67;color:white;font-weight:700;white-space:nowrap}.admin-form button:disabled,.inline-form button:disabled,.court-row button:disabled,td button:disabled{opacity:.5;cursor:wait}.court-list{display:grid;gap:9px}.court-row{display:grid;grid-template-columns:80px 1fr 130px 1fr auto;align-items:center;gap:12px;padding:10px 0;border-bottom:1px solid #ecf1ef}.table-scroll{overflow-x:auto}table{width:100%;border-collapse:collapse;min-width:620px;text-align:left}th,td{padding:12px 10px;border-bottom:1px solid #e8efec;vertical-align:middle}th{color:#667c73;font-size:12px}td{font-size:13px}td small{display:block;color:#7c8f87;margin-top:5px}.actions{display:flex;gap:7px}.pill{display:inline-block;border-radius:20px;background:#e5f5ee;color:#14725a;padding:5px 9px;font-size:11px;font-weight:800}.inline-form{display:flex;gap:10px;max-width:500px}.preview,.exception-item{background:#f2f8f5;border-radius:9px;padding:11px;color:#315b4f;margin:14px 0 0}.admin-alert{padding:12px 15px;border-radius:9px;margin-bottom:15px}.admin-alert.error{background:#fce9e9;color:#9e3737}.admin-alert.success{background:#e1f5eb;color:#0d7659}.admin-empty{color:#6a8178}.note-field{max-width:370px;margin:20px 0}.hint{font-size:13px;line-height:1.6}@media(max-width:800px){.admin-shell{display:block}.admin-nav{flex-direction:row;overflow:auto;margin-bottom:20px}.admin-nav button{white-space:nowrap}.admin-grid{grid-template-columns:1fr}.admin-form{grid-template-columns:1fr}.court-row{grid-template-columns:65px 1fr 110px auto}.admin-account small{display:none}}@media(max-width:520px){.admin-topbar{padding:0 16px}.admin-brand strong{font-size:14px}.admin-account strong{display:none}.admin-shell{padding:20px 16px}.admin-heading h1{font-size:27px}.court-row{grid-template-columns:1fr 1fr}.court-row strong{grid-column:1/-1}.admin-card{padding:18px}}
.admin-repair{margin-top:18px}.admin-repair button{margin:0 8px 8px 0;border:0;border-radius:8px;padding:10px 14px;background:#137c67;color:#fff;font-weight:700}.admin-repair button:disabled{opacity:.5}
</style>
