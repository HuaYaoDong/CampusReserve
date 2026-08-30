<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue";

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
type SlotStatus = "UNRELEASED" | "AVAILABLE" | "BOOKED" | "BLOCKED";
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

const SESSION_KEY = "campus-reserve-access-token";
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
const inventoryError = ref("");
const inventoryLoading = ref(false);
const selectedSlotIds = ref<number[]>([]);
const pendingRequestId = ref("");
const bookingLoading = ref(false);
const bookingMessage = ref("");
const bookingError = ref("");
const bookings = ref<Booking[]>([]);
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

async function login() {
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
    await Promise.all([refreshInventory(), refreshBookings()]);
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
    await Promise.all([refreshInventory(), refreshBookings()]);
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
  inventoryError.value = "";
  selectedSlotIds.value = [];
  pendingRequestId.value = "";
  bookings.value = [];
  bookingMessage.value = "";
  bookingError.value = "";
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
  if (!token.value) return;
  inventoryLoading.value = true;
  inventoryError.value = "";
  try {
    const response = await fetch("/api/v1/venues/1/inventory", {
      cache: "no-store",
      headers: { Authorization: `Bearer ${token.value}` },
    });
    if (response.status === 401) {
      logout();
      throw new Error("登录已过期，请重新登录");
    }
    if (!response.ok) throw new Error("目标日期库存尚未生成");
    inventory.value = (await response.json()) as Inventory;
  } catch (reason) {
    inventory.value = null;
    inventoryError.value = reason instanceof Error ? reason.message : "库存读取失败";
  } finally {
    inventoryLoading.value = false;
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
  } catch (reason) {
    bookingError.value = reason instanceof Error ? reason.message : "预约记录读取失败";
  }
}

function toggleSlot(slot: InventorySlot) {
  if (slot.status !== "AVAILABLE" || bookingLoading.value) return;
  bookingMessage.value = "";
  bookingError.value = "";
  if (selectedSlotIds.value.includes(slot.id)) {
    selectedSlotIds.value = selectedSlotIds.value.filter((id) => id !== slot.id);
  } else if (selectedSlotIds.value.length >= 2) {
    bookingError.value = "每天最多选择两个小时";
    return;
  } else {
    selectedSlotIds.value = [...selectedSlotIds.value, slot.id];
  }
  pendingRequestId.value = crypto.randomUUID();
}

async function submitBooking() {
  if (!token.value || selectedSlotIds.value.length === 0) return;
  bookingLoading.value = true;
  bookingMessage.value = "";
  bookingError.value = "";
  if (!pendingRequestId.value) pendingRequestId.value = crypto.randomUUID();
  try {
    const response = await fetch("/api/v1/booking-requests", {
      method: "POST",
      headers: {
        Authorization: `Bearer ${token.value}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        request_id: pendingRequestId.value,
        release_batch_id: inventory.value?.release_batch_id,
        service_date: inventory.value?.service_date,
        slot_ids: selectedSlotIds.value,
      }),
    });
    let result = (await response.json()) as BookingRequestResult & {
      detail?: { message?: string };
    };
    if (!response.ok) {
      pendingRequestId.value = "";
      throw new Error(result.detail?.message ?? result.error_message ?? "预约失败");
    }
    if (result.status === "PROCESSING") {
      bookingMessage.value = "抢位成功，正在生成预约…";
      const finalResult = await waitForBookingResult(result.request_id);
      if (finalResult) result = finalResult;
    }
    if (result.status === "FAILED") {
      pendingRequestId.value = "";
      throw new Error(result.error_message ?? "预约落库失败");
    }
    if (result.status === "PROCESSING") {
      bookingMessage.value = "请求已受理，可稍后在我的预约中查看结果";
    } else {
      bookingMessage.value = `预约成功，预约编号 ${result.booking?.booking_no ?? "已生成"}`;
    }
    selectedSlotIds.value = [];
    pendingRequestId.value = "";
    await Promise.all([refreshInventory(), refreshBookings()]);
  } catch (reason) {
    bookingError.value = reason instanceof Error ? reason.message : "预约失败，请重试";
  } finally {
    bookingLoading.value = false;
  }
}

async function waitForBookingResult(requestId: string): Promise<BookingRequestResult | null> {
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
    BOOKED: "已预约",
    BLOCKED: "已关闭",
  }[status];
}

onMounted(() => {
  void restoreSession();
  void refreshHealth();
  healthTimer = window.setInterval(refreshHealth, 15_000);
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
        <span>15 片场地</span><span>08:00—22:00</span><span>提前 2 天预约</span>
      </div>
    </section>

    <section class="login-panel">
      <div class="login-card">
        <div class="card-heading">
          <div>
            <p class="eyebrow">模拟统一身份认证</p>
            <h2>学生登录</h2>
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
        <p class="security-note">密码使用 Argon2 哈希保存；登录凭证 30 分钟后失效。</p>
      </div>
    </section>
  </main>

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
      <p class="intro">查看未来第 2 天的场地库存。预约按钮将在下一阶段接入高并发抢订接口。</p>

      <div class="dashboard-grid">
        <article class="feature-card main-feature">
          <div class="feature-icon">羽</div>
          <div>
            <p class="eyebrow">唯一场馆</p>
            <h2>{{ inventory?.venue_name ?? "校园羽毛球馆" }}</h2>
            <p>每天 08:00—22:00 开放，共 15 片场地；每天 08:00 放出未来第 2 天的库存。</p>
          </div>
          <span :class="['release-badge', inventory?.is_released ? 'open' : 'ready']">
            {{ inventory?.is_released ? "已放号" : "待放号" }}
          </span>
        </article>
        <article class="feature-card rule-card"><strong>每日额度</strong><span>最多 2 小时，可不连续</span></article>
        <article class="feature-card rule-card"><strong>目标日期</strong><span>{{ inventory ? formatDate(inventory.service_date) : "读取中" }}</span></article>
        <article class="feature-card rule-card"><strong>系统状态</strong><span :class="health?.status === 'ok' ? 'green-text' : 'orange-text'">{{ healthLabel }}</span></article>
      </div>

      <section class="inventory-section">
        <div class="inventory-heading">
          <div>
            <p class="eyebrow">实时库存</p>
            <h2>场地与时段</h2>
            <p v-if="inventory" class="inventory-summary">
              共 {{ inventory.slot_count }} 个时段库存，当前 {{ inventory.available_count }} 个可预约
            </p>
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
                      slot.status.toLowerCase(),
                      { selected: selectedSlotIds.includes(slot.id) },
                    ]"
                    :disabled="slot.status !== 'AVAILABLE' || bookingLoading"
                    :aria-label="`${court.name} ${formatTime(slot.start_at)} ${slotLabel(slot.status)}`"
                    @click="toggleSlot(slot)"
                  >
                    {{ selectedSlotIds.includes(slot.id) ? "已选择" : slotLabel(slot.status) }}
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
        <div class="inventory-legend">
          <span><i class="available" />可预约</span>
          <span><i class="unreleased" />未放号</span>
          <span><i class="booked" />已预约</span>
          <span><i class="blocked" />已关闭</span>
        </div>
      </section>

      <section class="my-bookings-section">
        <div>
          <p class="eyebrow">My bookings</p>
          <h2>我的预约</h2>
        </div>
        <p v-if="bookings.length === 0" class="empty-bookings">还没有预约记录</p>
        <template v-else>
          <article v-for="booking in bookings" :key="booking.id" class="booking-record">
            <div>
              <strong>{{ booking.booking_no }}</strong>
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
