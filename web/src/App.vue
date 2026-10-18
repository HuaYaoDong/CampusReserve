<script setup lang="ts">
// 学生入口：会话、今日/明日库存、预约请求找回和个人预约都在此组件协调。
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import AdminPanel from "./AdminPanel.vue";

type ComponentState = { status: "ok" | "error"; detail?: string | null };
type Health = {
  status: "ok" | "degraded";
  database: ComponentState;
  redis: ComponentState;
};
type UserRole = "STUDENT" | "VENUE_ADMIN" | "PLATFORM_ADMIN";
type UserProfile = {
  id: number;
  username: string;
  student_no: string | null;
  display_name: string;
  department: string | null;
  role: UserRole;
};
type TokenResponse = {
  access_token: string;
  token_type: "bearer";
  expires_in: number;
  user: UserProfile;
};
type SlotStatus = "UNRELEASED" | "AVAILABLE" | "PROCESSING" | "BOOKED" | "BLOCKED" | "EXPIRED";
type InventorySlot = {
  id: number;
  start_at: string;
  end_at: string;
  status: SlotStatus;
};
type CourtInventory = {
  id: number;
  code: string;
  name: string;
  status: "ACTIVE" | "DISABLED" | "MAINTENANCE";
  slots: InventorySlot[];
};
type Inventory = {
  venue_id: number;
  venue_name: string;
  release_batch_id: number;
  service_date: string;
  release_at: string;
  batch_status: "READY" | "OPEN" | "PAUSED" | "CLOSED";
  is_released: boolean;
  slot_count: number;
  available_count: number;
  courts: CourtInventory[];
};
type BookingItem = {
  slot_id: number;
  court_id: number;
  court_code: string;
  court_name: string;
  service_date: string;
  start_at: string;
  end_at: string;
};
type Booking = {
  id: number;
  request_id: string;
  booking_no: string;
  status: "CONFIRMED" | "CHECKED_IN" | "COMPLETED";
  confirmed_at: string | null;
  created_at: string;
  items: BookingItem[];
};
type BookingRequestResult = {
  request_id: string;
  status: "PROCESSING" | "CONFIRMED" | "FAILED";
  error_code: string | null;
  error_message: string | null;
  replayed: boolean;
  booking: Booking | null;
};
type TrackedRequest = {
  request_id: string;
  service_date: string;
  slots: string[];
  created_at: string;
  status: BookingRequestResult["status"];
  error_message: string | null;
  booking_no: string | null;
  lookup_error: string | null;
};

const SESSION_KEY = "campus-reserve-access-token";
// 请求记录按用户隔离；轮询结束后保留 UUID，便于刷新页面后继续查询。
const REQUESTS_KEY_PREFIX = "campus-reserve-booking-requests-v1-";
const SHANGHAI_TIME_ZONE = "Asia/Shanghai";
// 日期以场馆所在时区为准，不能直接使用浏览器机器的本地日期。
function shanghaiDateISO(now = new Date()) {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: SHANGHAI_TIME_ZONE,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(now);
  const value = (type: string) => parts.find((part) => part.type === type)?.value ?? "";
  return `${value("year")}-${value("month")}-${value("day")}`;
}
function nextDateISO(value: string) {
  const date = new Date(`${value}T00:00:00Z`);
  date.setUTCDate(date.getUTCDate() + 1);
  return date.toISOString().slice(0, 10);
}
const username = ref("student001");
const password = ref("Student@123");
const token = ref(sessionStorage.getItem(SESSION_KEY) ?? "");
const user = ref<UserProfile | null>(null);
const loginError = ref("");
const loginLoading = ref(false);
const restoring = ref(Boolean(token.value));
const health = ref<Health | null>(null);
const healthError = ref("");
const inventory = ref<Inventory | null>(null);
const todayServiceDate = ref(shanghaiDateISO());
const requestedServiceDate = ref(nextDateISO(todayServiceDate.value));
const selectableServiceDates = computed(() => [todayServiceDate.value, nextDateISO(todayServiceDate.value)]);
const nowMilliseconds = ref(Date.now());
const inventoryError = ref("");
const inventoryLoading = ref(false);
let inventoryFetchRevision = 0;
const selectedSlotIds = ref<number[]>([]);
const pendingRequestId = ref("");
const bookingLoading = ref(false);
const bookingMessage = ref("");
const bookingError = ref("");
const storageError = ref("");
const bookings = ref<Booking[]>([]);
const trackedRequests = ref<TrackedRequest[]>([]);
const queryingRequestIds = ref<string[]>([]);
let healthTimer: number | undefined;

const isAuthenticated = computed(() => Boolean(token.value && user.value));
const healthLabel = computed(() => {
  if (healthError.value) return "服务异常";
  if (!health.value) return "检查中";
  return health.value.status === "ok" ? "服务正常" : "部分异常";
});
const selectedSlots = computed(() => {
  const selected = new Set(selectedSlotIds.value);
  return (
    inventory.value?.courts
      .flatMap((court) => court.slots.map((slot) => ({ ...slot, court_name: court.name })))
      .filter((slot) => selected.has(slot.id)) ?? []
  );
});
const bookableSlotCount = computed(() => inventory.value?.courts.reduce((count, court) =>
  count + court.slots.filter((slot) => visibleSlotStatus(slot) === "AVAILABLE").length, 0) ?? 0);
const isTodayInventory = computed(() => inventory.value?.service_date === todayServiceDate.value);

async function login() {
  // 登录成功后还要读取本人预约与未完成请求，页面状态才算恢复完成。
  loginError.value = "";
  loginLoading.value = true;
  try {
    const response = await fetch("/api/v1/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: username.value.trim(), password: password.value }),
    });
    if (!response.ok) {
      throw new Error(response.status === 401 ? "账号或密码错误" : "登录服务暂不可用");
    }
    const result = (await response.json()) as TokenResponse;
    token.value = result.access_token;
    user.value = result.user;
    sessionStorage.setItem(SESSION_KEY, result.access_token);
    password.value = "";
    if (result.user.role === "STUDENT") {
      loadTrackedRequests();
      await Promise.all([refreshInventory(), refreshBookings()]);
      void refreshProcessingRequests();
    }
  } catch (reason) {
    loginError.value = reason instanceof Error ? reason.message : "登录失败";
  } finally {
    loginLoading.value = false;
  }
}

async function restoreSession() {
  if (!token.value) {
    restoring.value = false;
    return;
  }
  try {
    const response = await fetch("/api/v1/auth/me", {
      headers: { Authorization: `Bearer ${token.value}` },
    });
    if (!response.ok) throw new Error("登录已过期");
    user.value = (await response.json()) as UserProfile;
    if (user.value.role === "STUDENT") {
      loadTrackedRequests();
      await Promise.all([refreshInventory(), refreshBookings()]);
      void refreshProcessingRequests();
    }
  } catch {
    logout();
  } finally {
    restoring.value = false;
  }
}

function logout() {
  token.value = "";
  user.value = null;
  inventory.value = null;
  requestedServiceDate.value = nextDateISO(shanghaiDateISO());
  inventoryFetchRevision += 1;
  inventoryLoading.value = false;
  inventoryError.value = "";
  selectedSlotIds.value = [];
  pendingRequestId.value = "";
  bookings.value = [];
  trackedRequests.value = [];
  queryingRequestIds.value = [];
  bookingMessage.value = "";
  bookingError.value = "";
  storageError.value = "";
  bookingLoading.value = false;
  sessionStorage.removeItem(SESSION_KEY);
  password.value = "Student@123";
}

async function refreshHealth() {
  try {
    const response = await fetch("/api/v1/system/health", { cache: "no-store" });
    if (!response.ok) throw new Error("健康检查失败");
    health.value = (await response.json()) as Health;
    healthError.value = "";
  } catch (reason) {
    health.value = null;
    healthError.value = reason instanceof Error ? reason.message : "无法连接服务";
  }
}

async function refreshInventory() {
  // 多次切换日期时只接受最后一次请求的响应，避免旧响应覆盖新日期。
  if (!token.value) return;
  updateDateWindow();
  const revision = ++inventoryFetchRevision;
  const query = `?service_date=${encodeURIComponent(requestedServiceDate.value)}`;
  inventoryLoading.value = true;
  inventoryError.value = "";
  try {
    const response = await fetch(`/api/v1/venues/1/inventory${query}`, {
      cache: "no-store",
      headers: { Authorization: `Bearer ${token.value}` },
    });
    if (revision !== inventoryFetchRevision) return;
    if (response.status === 401) {
      logout();
      throw new Error("登录已过期，请重新登录");
    }
    if (response.status === 404) throw new Error("所选日期暂无库存记录");
    if (!response.ok) throw new Error("库存读取失败，请稍后刷新");
    const result = (await response.json()) as Inventory;
    if (revision !== inventoryFetchRevision) return;
    inventory.value = result;
  } catch (reason) {
    if (revision !== inventoryFetchRevision) return;
    inventory.value = null;
    inventoryError.value = reason instanceof Error ? reason.message : "库存读取失败";
  } finally {
    if (revision === inventoryFetchRevision) inventoryLoading.value = false;
  }
}

function selectServiceDate(serviceDate: string) {
  // 只开放今天和明天两个入口；具体时段是否可订仍由服务端校验。
  updateDateWindow();
  if (bookingLoading.value || requestedServiceDate.value === serviceDate ||
    !selectableServiceDates.value.includes(serviceDate)) return;
  requestedServiceDate.value = serviceDate;
  selectedSlotIds.value = [];
  pendingRequestId.value = "";
  bookingMessage.value = "";
  bookingError.value = "";
  inventory.value = null;
  void refreshInventory();
}

function updateDateWindow() {
  // 跨过上海时区的午夜后同步日期窗口，清除已不属于今天/明天的选择。
  nowMilliseconds.value = Date.now();
  if (inventory.value && selectedSlotIds.value.length) {
    const availableIds = new Set(inventory.value.courts.flatMap((court) => court.slots)
      .filter((slot) => visibleSlotStatus(slot) === "AVAILABLE").map((slot) => slot.id));
    const validIds = selectedSlotIds.value.filter((id) => availableIds.has(id));
    if (validIds.length !== selectedSlotIds.value.length) {
      selectedSlotIds.value = validIds;
      pendingRequestId.value = "";
    }
  }
  const currentDate = shanghaiDateISO(new Date(nowMilliseconds.value));
  if (currentDate === todayServiceDate.value) return;
  todayServiceDate.value = currentDate;
  if (!selectableServiceDates.value.includes(requestedServiceDate.value)) {
    requestedServiceDate.value = nextDateISO(currentDate);
    selectedSlotIds.value = [];
    pendingRequestId.value = "";
    inventory.value = null;
    inventoryFetchRevision += 1;
  }
}

async function refreshBookings() {
  if (!token.value) return;
  try {
    const response = await fetch("/api/v1/bookings", {
      cache: "no-store",
      headers: { Authorization: `Bearer ${token.value}` },
    });
    if (!response.ok) throw new Error("预约记录读取失败");
    bookings.value = (await response.json()) as Booking[];
    bookingError.value = "";
    for (const booking of bookings.value) {
      if (trackedRequests.value.some((request) => request.request_id === booking.request_id)) {
        updateTrackedRequest(booking.request_id, {
          status: "CONFIRMED",
          booking_no: booking.booking_no,
          error_message: null,
          lookup_error: null,
        });
      }
    }
  } catch (reason) {
    bookingError.value = reason instanceof Error ? reason.message : "预约记录读取失败";
  }
}

function requestStorageKey() {
  return user.value ? `${REQUESTS_KEY_PREFIX}${user.value.id}` : "";
}

function saveTrackedRequests() {
  // 本地记录只用于找回查询入口，不作为预约成功的事实来源。
  const key = requestStorageKey();
  if (!key) return;
  // Keep every unresolved request. Limit completed history so local storage stays small.
  const unresolved = trackedRequests.value.filter((request) => request.status === "PROCESSING");
  const finished = trackedRequests.value.filter((request) => request.status !== "PROCESSING").slice(0, 30);
  trackedRequests.value = [...unresolved, ...finished].sort((a, b) => b.created_at.localeCompare(a.created_at));
  try {
    localStorage.setItem(key, JSON.stringify(trackedRequests.value));
    storageError.value = "";
  } catch {
    storageError.value = "浏览器无法保存请求记录。请复制请求编号并妥善保存。";
  }
}

function loadTrackedRequests() {
  trackedRequests.value = [];
  const key = requestStorageKey();
  if (!key) return;
  try {
    const saved: unknown = JSON.parse(localStorage.getItem(key) ?? "[]");
    if (!Array.isArray(saved)) return;
    trackedRequests.value = saved.filter((value): value is TrackedRequest =>
      value !== null && typeof value === "object" &&
      typeof value.request_id === "string" && typeof value.service_date === "string" &&
      Array.isArray(value.slots) && typeof value.created_at === "string" &&
      ["PROCESSING", "CONFIRMED", "FAILED"].includes(value.status),
    );
  } catch {
    trackedRequests.value = [];
    storageError.value = "浏览器无法读取已保存的请求记录。";
  }
}

function updateTrackedRequest(requestId: string, changes: Partial<TrackedRequest>) {
  trackedRequests.value = trackedRequests.value.map((request) =>
    request.request_id === requestId ? { ...request, ...changes } : request,
  );
  saveTrackedRequests();
}

function applyRequestResult(result: BookingRequestResult) {
  const current = trackedRequests.value.find((request) => request.request_id === result.request_id);
  if (!current || (current.status !== "PROCESSING" && result.status === "PROCESSING") ||
    (current.status === "CONFIRMED" && result.status === "FAILED")) return;
  updateTrackedRequest(result.request_id, {
    status: result.status,
    error_message: result.error_message ?? result.error_code,
    booking_no: result.booking?.booking_no ?? null,
    lookup_error: null,
  });
}

async function queryTrackedRequest(requestId: string) {
  // 结果查询同时刷新库存和个人预约，避免只更新一张状态卡片。
  if (!token.value || queryingRequestIds.value.includes(requestId)) return;
  const userId = user.value?.id;
  queryingRequestIds.value = [...queryingRequestIds.value, requestId];
  try {
    const response = await fetch(`/api/v1/booking-requests/${encodeURIComponent(requestId)}`, {
      cache: "no-store",
      headers: { Authorization: `Bearer ${token.value}` },
    });
    if (response.status === 401) {
      logout();
      return;
    }
    if (user.value?.id !== userId) return;
    if (response.status === 404) {
      updateTrackedRequest(requestId, { lookup_error: "服务端暂未找到这个请求，请稍后再查。" });
      return;
    }
    if (!response.ok) throw new Error("查询暂不可用，请稍后重试");
    const result = (await response.json()) as BookingRequestResult;
    if (user.value?.id !== userId) return;
    applyRequestResult(result);
    if (result.status === "CONFIRMED") await Promise.all([refreshBookings(), refreshInventory()]);
  } catch (reason) {
    if (user.value?.id === userId) {
      updateTrackedRequest(requestId, {
        lookup_error: reason instanceof Error ? reason.message : "查询暂不可用，请稍后重试",
      });
    }
  } finally {
    queryingRequestIds.value = queryingRequestIds.value.filter((id) => id !== requestId);
  }
}

async function refreshProcessingRequests() {
  const requests = trackedRequests.value.filter((request) => request.status === "PROCESSING");
  for (let index = 0; index < requests.length; index += 5) {
    await Promise.all(requests.slice(index, index + 5)
      .map((request) => queryTrackedRequest(request.request_id)));
  }
}

function removeTrackedRequest(requestId: string) {
  trackedRequests.value = trackedRequests.value.filter((request) => request.request_id !== requestId);
  saveTrackedRequests();
}

function toggleSlot(slot: InventorySlot) {
  // 前端限制一次最多两个时段，后端仍会重做额度与批次检查。
  if (visibleSlotStatus(slot) !== "AVAILABLE" || bookingLoading.value) return;
  bookingMessage.value = "";
  bookingError.value = "";
  if (selectedSlotIds.value.includes(slot.id)) {
    selectedSlotIds.value = selectedSlotIds.value.filter((id) => id !== slot.id);
  } else if (selectedSlotIds.value.length >= 2) {
    bookingError.value = "每天最多选择两个时段";
    return;
  } else {
    selectedSlotIds.value = [...selectedSlotIds.value, slot.id];
  }
  pendingRequestId.value = crypto.randomUUID();
}

async function submitBooking() {
  // 先保存请求 UUID 再发起请求，网络超时也不会丢失继续查询的凭证。
  updateDateWindow();
  if (!token.value || !inventory.value || !selectableServiceDates.value.includes(inventory.value.service_date) ||
    selectedSlotIds.value.length === 0 ||
    selectedSlots.value.some((slot) => visibleSlotStatus(slot) !== "AVAILABLE")) return;
  bookingLoading.value = true;
  bookingMessage.value = "";
  bookingError.value = "";
  if (!pendingRequestId.value) pendingRequestId.value = crypto.randomUUID();
  const requestId = pendingRequestId.value;
  const userId = user.value?.id;
  const slotIds = [...selectedSlotIds.value];
  const releaseBatchId = inventory.value.release_batch_id;
  const serviceDate = inventory.value.service_date;
  trackedRequests.value = [{
    request_id: requestId,
    service_date: serviceDate,
    slots: selectedSlots.value.map((slot) =>
      `${slot.court_name} ${formatTime(slot.start_at)}—${formatTime(slot.end_at)}`),
    created_at: new Date().toISOString(),
    status: "PROCESSING",
    error_message: null,
    booking_no: null,
    lookup_error: null,
  }, ...trackedRequests.value.filter((request) => request.request_id !== requestId)];
  saveTrackedRequests();
  // Once submission starts, a second click must not create a new request for the same slots.
  selectedSlotIds.value = [];
  pendingRequestId.value = "";
  try {
    const response = await fetch("/api/v1/booking-requests", {
      method: "POST",
      headers: {
        Authorization: `Bearer ${token.value}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        request_id: requestId,
        release_batch_id: releaseBatchId,
        service_date: serviceDate,
        slot_ids: slotIds,
      }),
    });
    let result = (await response.json()) as BookingRequestResult & {
      detail?: { message?: string };
    };
    if (!response.ok) {
      const message = result.detail?.message ?? result.error_message ?? "预约失败";
      if (user.value?.id === userId && (result.status === "FAILED" || response.status < 500)) {
        updateTrackedRequest(requestId, { status: "FAILED", error_message: message });
      }
      throw new Error(message);
    }
    if (user.value?.id !== userId) return;
    applyRequestResult(result);
    if (result.status === "PROCESSING") {
      bookingMessage.value = "抢位成功，正在生成预约…";
      const finalResult = await waitForBookingResult(requestId);
      if (finalResult) {
        result = finalResult;
        applyRequestResult(result);
      }
    }
    if (result.status === "FAILED") {
      throw new Error(result.error_message ?? "预约落库失败");
    }
    if (result.status === "PROCESSING") {
      bookingMessage.value = `请求 ${requestId} 仍在处理，可在下方“预约请求记录”继续查询。`;
    } else {
      bookingMessage.value = `预约成功，预约编号 ${result.booking?.booking_no ?? "已生成"}`;
    }
    await Promise.all([refreshInventory(), refreshBookings()]);
  } catch (reason) {
    if (user.value?.id === userId) {
      const tracked = trackedRequests.value.find((request) => request.request_id === requestId);
      bookingError.value = tracked?.status === "PROCESSING"
        ? `请求 ${requestId} 的结果暂未确认，请在下方“预约请求记录”继续查询。`
        : reason instanceof Error ? reason.message : "预约失败，请重试";
    }
  } finally {
    bookingLoading.value = false;
  }
}

async function waitForBookingResult(requestId: string): Promise<BookingRequestResult | null> {
  // 短时轮询只改善交互；超时后维持处理中并允许用户稍后查询。
  for (let attempt = 0; attempt < 20; attempt += 1) {
    await new Promise((resolve) => window.setTimeout(resolve, 250));
    const response = await fetch(`/api/v1/booking-requests/${requestId}`, {
      cache: "no-store",
      headers: { Authorization: `Bearer ${token.value}` },
    });
    if (response.status === 401) {
      logout();
      throw new Error("登录已过期，请重新登录");
    }
    if (!response.ok) throw new Error("预约结果查询失败");
    const result = (await response.json()) as BookingRequestResult;
    if (result.status !== "PROCESSING") return result;
  }
  return null;
}

function formatDate(value: string) {
  return new Intl.DateTimeFormat("zh-CN", {
    month: "long",
    day: "numeric",
    weekday: "short",
    timeZone: "Asia/Shanghai",
  }).format(new Date(`${value}T00:00:00+08:00`));
}

function formatTime(value: string) {
  return new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    timeZone: "Asia/Shanghai",
  }).format(new Date(value));
}

function slotLabel(status: SlotStatus) {
  return {
    UNRELEASED: "未放号",
    AVAILABLE: "可预约",
    PROCESSING: "处理中",
    BOOKED: "已预约",
    BLOCKED: "已关闭",
    EXPIRED: "已过期",
  }[status];
}

function visibleSlotStatus(slot: InventorySlot): SlotStatus {
  // 今天过去的时段仍展示：已订出优先显示已预约，否则显示已过期。
  if (Date.parse(slot.start_at) <= nowMilliseconds.value) {
    return slot.status === "BOOKED" ? "BOOKED" : "EXPIRED";
  }
  return slot.status;
}

onMounted(() => {
  void restoreSession();
  void refreshHealth();
  healthTimer = window.setInterval(() => {
    const previousDate = todayServiceDate.value;
    updateDateWindow();
    if (todayServiceDate.value !== previousDate && user.value?.role === "STUDENT") {
      void refreshInventory();
    }
    void refreshHealth();
  }, 15_000);
});

onBeforeUnmount(() => window.clearInterval(healthTimer));
</script>

<template>
  <main v-if="restoring" class="loading-screen">
    <span class="spinner" />
    <p>正在恢复登录状态…</p>
  </main>

  <main v-else-if="!isAuthenticated" class="login-page">
    <section class="brand-panel">
      <div class="brand-mark">CR</div>
      <p class="eyebrow">Campus Reserve</p>
      <h1>校园羽毛球场<br />高并发预约系统</h1>
      <p class="intro">面向课程设计的 MVP。集中验证万人同时抢订场景下的身份认证、库存一致性与零超卖。</p>
      <div class="venue-facts">
        <span>15 片场地</span><span>08:00—22:00</span><span>提前 1 天预约</span>
      </div>
    </section>

    <section class="login-panel">
      <div class="login-card">
        <div class="card-heading">
          <div>
            <p class="eyebrow">模拟统一身份认证</p>
            <h2>账号登录</h2>
          </div>
          <span :class="['service-state', health?.status === 'ok' && !healthError ? 'ok' : 'warn']">
            {{ healthLabel }}
          </span>
        </div>

        <form @submit.prevent="login">
          <label for="username">学号或账号</label>
          <input id="username" v-model="username" autocomplete="username" required minlength="3" />

          <label for="password">密码</label>
          <input
            id="password"
            v-model="password"
            type="password"
            autocomplete="current-password"
            required
            minlength="8"
          />

          <p v-if="loginError" class="form-error" role="alert">{{ loginError }}</p>
          <button class="primary-button" type="submit" :disabled="loginLoading">
            <span v-if="loginLoading" class="button-spinner" />
            {{ loginLoading ? "登录中…" : "进入预约系统" }}
          </button>
        </form>

        <div class="demo-account">
          <strong>演示学生账号</strong>
          <span>student001 / Student@123</span>
          <button type="button" @click="username = 'student001'; password = 'Student@123'">一键填入</button>
        </div>
        <div class="demo-account">
          <strong>演示管理员账号</strong>
          <span>venue_admin / Admin@123</span>
          <button type="button" @click="username = 'venue_admin'; password = 'Admin@123'">一键填入</button>
        </div>
        <p class="security-note">密码使用 Argon2 哈希保存；登录凭证 30 分钟后失效。</p>
      </div>
    </section>
  </main>

  <AdminPanel v-else-if="user && user.role !== 'STUDENT'" :token="token" :user="user" @logout="logout" />

  <main v-else class="dashboard-page">
    <header class="topbar">
      <div class="topbar-brand"><span>CR</span><strong>校园场地预约</strong></div>
      <div class="user-menu">
        <div><strong>{{ user?.display_name }}</strong><small>{{ user?.student_no ?? user?.username }}</small></div>
        <button type="button" @click="logout">退出登录</button>
      </div>
    </header>

    <section class="dashboard-content">
      <p class="eyebrow">预约中心</p>
      <h1>你好，{{ user?.display_name }}</h1>
      <p class="intro">查看今天和明天的场地，选择可预约时段提交预约。</p>

      <div class="dashboard-grid">
        <article class="feature-card main-feature">
          <div class="feature-icon">羽</div>
          <div>
            <p class="eyebrow">唯一场馆</p>
            <h2>{{ inventory?.venue_name ?? "校园羽毛球馆" }}</h2>
            <p>每天 08:00—22:00 开放，共 15 片场地；每天 08:00 放出明天的库存。</p>
          </div>
          <span :class="['release-badge', inventory?.is_released ? 'open' : 'ready']">
            {{ inventory ? (inventory.is_released ? "已放号" : "待放号") :
              inventoryError === "所选日期暂无库存记录" ? "暂无记录" :
              inventoryError ? "读取失败" : "查询中" }}
          </span>
        </article>
        <article class="feature-card rule-card"><strong>每日额度</strong><span>最多 2 个时段，可同时段选择不同场地</span></article>
        <article class="feature-card rule-card"><strong>使用日期</strong><span>{{ formatDate(requestedServiceDate) }}</span></article>
        <article class="feature-card rule-card"><strong>系统状态</strong><span :class="health?.status === 'ok' ? 'green-text' : 'orange-text'">{{ healthLabel }}</span></article>
      </div>

      <section class="inventory-section">
        <div class="service-date-picker" role="group" aria-label="选择场地使用日期">
          <span>选择使用日期</span>
          <button v-for="(serviceDate, index) in selectableServiceDates" :key="serviceDate"
            type="button" :class="['date-option', { active: requestedServiceDate === serviceDate }]"
            :aria-pressed="requestedServiceDate === serviceDate" :disabled="bookingLoading"
            @click="selectServiceDate(serviceDate)">
            {{ index === 0 ? "今天" : "明天" }} · {{ formatDate(serviceDate) }}
          </button>
        </div>
        <div class="inventory-heading">
          <div>
            <p class="eyebrow">实时库存</p>
            <h2>场地与时段</h2>
            <p v-if="inventory" class="inventory-summary">
              共 {{ inventory.slot_count }} 个场地时段，当前 {{ bookableSlotCount }} 个可预约
            </p>
            <p v-if="isTodayInventory" class="inventory-summary">已开始的时段显示预约结果；尚未开始的空闲时段仍可预约。</p>
          </div>
          <button type="button" class="refresh-button" :disabled="inventoryLoading" @click="refreshInventory">
            {{ inventoryLoading ? "刷新中…" : "刷新库存" }}
          </button>
        </div>

        <div v-if="inventoryLoading && !inventory" class="inventory-message">
          <span class="spinner" />正在生成或读取库存…
        </div>
        <div v-else-if="inventoryError" class="inventory-message error-state">
          {{ inventoryError }}
        </div>
        <div v-else-if="inventory" class="inventory-table-wrap">
          <table class="inventory-table">
            <thead>
              <tr>
                <th>场地</th>
                <th v-for="slot in inventory.courts[0]?.slots ?? []" :key="slot.id">
                  {{ formatTime(slot.start_at) }}
                </th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="court in inventory.courts" :key="court.id">
                <th><strong>{{ court.name }}</strong><small>{{ court.code }}</small></th>
                <td v-for="slot in court.slots" :key="slot.id">
                  <button
                    type="button"
                    :class="[
                      'slot-cell',
                      visibleSlotStatus(slot).toLowerCase(),
                      { selected: selectedSlotIds.includes(slot.id) },
                    ]"
                    :disabled="visibleSlotStatus(slot) !== 'AVAILABLE' || bookingLoading"
                    :aria-label="`${court.name} ${formatTime(slot.start_at)} ${slotLabel(visibleSlotStatus(slot))}`"
                    @click="toggleSlot(slot)"
                  >
                    {{ selectedSlotIds.includes(slot.id) ? "已选择" : slotLabel(visibleSlotStatus(slot)) }}
                  </button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <div v-if="selectedSlots.length" class="booking-action-bar">
          <div>
            <strong>已选择 {{ selectedSlots.length }} 个时段</strong>
            <span v-for="slot in selectedSlots" :key="slot.id">
              {{ slot.court_name }} {{ formatTime(slot.start_at) }}—{{ formatTime(slot.end_at) }}
            </span>
          </div>
          <button type="button" :disabled="bookingLoading" @click="submitBooking">
            {{ bookingLoading ? "预约处理中…" : "确认预约" }}
          </button>
        </div>
        <p v-if="bookingMessage" class="booking-feedback success-state" role="status">
          {{ bookingMessage }}
        </p>
        <p v-if="bookingError" class="booking-feedback error-state" role="alert">
          {{ bookingError }}
        </p>
        <p v-if="storageError" class="booking-feedback error-state" role="alert">{{ storageError }}</p>
        <div class="inventory-legend">
          <span><i class="available" />可预约</span>
          <span><i class="processing" />处理中</span>
          <span><i class="unreleased" />未放号</span>
          <span><i class="booked" />已预约</span>
          <span><i class="blocked" />已关闭</span>
          <span><i class="expired" />已过期</span>
        </div>
      </section>

      <section class="my-bookings-section request-history-section">
        <div class="request-history-heading">
          <div>
            <p class="eyebrow">Booking requests</p>
            <h2>预约请求记录</h2>
            <p>提交后会在此浏览器保存请求编号；处理中或查询异常时，可继续查询结果。</p>
          </div>
          <button v-if="trackedRequests.some((request) => request.status === 'PROCESSING')"
            type="button" class="refresh-button" @click="refreshProcessingRequests">查询处理中请求</button>
        </div>
        <p v-if="trackedRequests.length === 0" class="empty-bookings">还没有预约请求</p>
        <article v-for="request in trackedRequests" :key="request.request_id" class="request-record">
          <div class="request-record-main">
            <strong class="request-id">请求编号：{{ request.request_id }}</strong>
            <span>使用日期：{{ request.service_date }}</span>
            <span v-for="slot in request.slots" :key="slot">{{ slot }}</span>
            <span v-if="request.booking_no">预约编号：{{ request.booking_no }}</span>
            <span v-if="request.error_message" class="request-error">{{ request.error_message }}</span>
            <span v-if="request.lookup_error" class="request-error">{{ request.lookup_error }}</span>
          </div>
          <div class="request-record-actions">
            <span :class="['request-status', request.status.toLowerCase()]">
              {{ request.status === 'PROCESSING' ? '处理中' : request.status === 'FAILED' ? '失败' : '已确认' }}
            </span>
            <button type="button" class="refresh-button"
              :disabled="queryingRequestIds.includes(request.request_id)"
              @click="queryTrackedRequest(request.request_id)">
              {{ queryingRequestIds.includes(request.request_id) ? '查询中…' : '继续查询' }}
            </button>
            <button v-if="request.status !== 'PROCESSING'" type="button" class="request-remove"
              @click="removeTrackedRequest(request.request_id)">移除记录</button>
          </div>
        </article>
      </section>

      <section class="my-bookings-section">
        <div>
          <p class="eyebrow">My bookings</p>
          <h2>我的预约</h2>
        </div>
        <p v-if="bookings.length === 0" class="empty-bookings">还没有已确认的预约</p>
        <template v-else>
          <article v-for="booking in bookings" :key="booking.id" class="booking-record">
            <div>
              <strong>{{ booking.booking_no }}</strong>
              <span class="booking-service-date">使用日期：{{ booking.items[0]?.service_date ?? "待确认" }}</span>
              <span>预约成功后不可取消</span>
            </div>
            <div class="booking-record-items">
              <span v-for="item in booking.items" :key="item.slot_id">
                {{ item.court_name }} · {{ formatTime(item.start_at) }}—{{ formatTime(item.end_at) }}
              </span>
            </div>
            <span class="booking-status">{{ booking.status === "CONFIRMED" ? "已确认" : booking.status }}</span>
          </article>
        </template>
      </section>
    </section>
  </main>
</template>
