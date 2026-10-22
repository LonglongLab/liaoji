<script setup lang="ts">
import { useQuery, useQueryClient } from "@tanstack/vue-query";
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { RouterLink, useRoute, useRouter } from "vue-router";

import { ArchiveApiError, type UpdateCheckTrigger, type UpdateStatus } from "@/api/archiveApi";
import { useArchiveApi } from "@/api/injection";
import { useModalFocus } from "@/composables/useModalFocus";
import {
  dismissUpdateBubble,
  dismissUpdateReminder,
  offeredUpdateVersion,
  reminderVersion,
  readPostponed,
  UPDATE_REMINDER_POSTPONE_MS,
  stagedUpdateVersion,
  updateBubbleWasDismissed,
  updateReminderWasDismissed,
} from "@/lib/updateReminder";

// 一个页面生命周期只投递一次 startup。AppShell 每换一页会重新挂一次这个组件，
// 不该每次都算一次「刚启动」。记号放在共享 query cache 里、永不回收。
const STARTUP_POSTED_KEY = ["update-entry-startup-posted"] as const;

const props = defineProps<{
  to: string;
  current: boolean;
}>();

const api = useArchiveApi();
const router = useRouter();
const route = useRoute();
const queryClient = useQueryClient();
const settingsQuery = useQuery({
  queryKey: ["app-settings"] as const,
  queryFn: () => api.getAppSettings(),
  staleTime: Infinity,
});
// 自动检查由后端调度（PSU-2.222：每 30 分钟左右、失败退避）。这里只轮询本机状态，
// 正在检查时加快，好让新版本在检查结束后很快出现在界面上。
const statusQuery = useQuery({
  queryKey: ["update-status"] as const,
  queryFn: async () => {
    const next = await api.getUpdateStatus();
    const old = readPostponed();
    if (old !== null && updateReminderWasDismissed(old.version) &&
        next.postponed_version == null) {
      // 将已有前端期限迁入本机共享记录；不从迁移时刻重新算24小时。
      return api.postponeUpdateReminder(old.version, old.until);
    }
    return next;
  },
  staleTime: 30_000,
  refetchInterval: (query) =>
    query.state.data?.check_phase === "checking" ? 2_000 : 60_000,
});

const canCheck = computed(
  () =>
    settingsQuery.data.value?.auto_update_enabled === true &&
    statusQuery.data.value?.configured === true,
);

// 只是「投递」：后端立即返回当前状态，是否真的联网由它按间隔与 5 分钟阈值决定。
// 失败不提示：后台自己会退避重试，设置页看得到连续失败。
const postCheck = (trigger: UpdateCheckTrigger): void => {
  if (!canCheck.value) return;
  api.checkForUpdates(trigger).then(
    (next) => queryClient.setQueryData(["update-status"], next),
    () => undefined,
  );
};

watch(
  canCheck,
  (ready) => {
    if (!ready || queryClient.getQueryData(STARTUP_POSTED_KEY) === true) return;
    queryClient.setQueryDefaults(STARTUP_POSTED_KEY, { gcTime: Infinity });
    queryClient.setQueryData(STARTUP_POSTED_KEY, true);
    postCheck("startup");
  },
  { immediate: true },
);

const status = computed(() => statusQuery.data.value);
const offered = computed(() => offeredUpdateVersion(status.value));
const staged = computed(() => stagedUpdateVersion(status.value));
const version = computed(() => reminderVersion(status.value));
const enabled = computed(() => settingsQuery.data.value?.auto_update_enabled === true);
const hasUpdate = computed(() => enabled.value && version.value !== null);

const modalOpen = ref(false);
const bubbleOpen = ref(false);
const busy = ref<"download" | "restart" | null>(null);
const actionError = ref<string | null>(null);
const restarting = ref(false);
const postponing = ref(false);

const postpone = async (): Promise<boolean> => {
  if (busy.value === "restart" || restarting.value || postponing.value) return false;
  const value = version.value;
  if (value === null) return false;
  postponing.value = true;
  actionError.value = null;
  try {
    const next = await api.postponeUpdateReminder(value);
    dismissUpdateReminder(value, typeof next.postponed_until === "number"
      ? next.postponed_until - UPDATE_REMINDER_POSTPONE_MS : Date.now());
    queryClient.setQueryData(["update-status"], next);
    modalOpen.value = false;
    bubbleOpen.value = !updateBubbleWasDismissed(value);
    return true;
  } catch {
    actionError.value = "暂缓没有保存，请重试。";
    return false;
  } finally {
    postponing.value = false;
  }
};

const { container: dialog, initial, onKeydown } = useModalFocus(modalOpen, () => { void postpone(); });
void dialog;
void initial;

const evaluateReminder = (next: string | null, isEnabled: boolean): void => {
  if (route.path === props.to && route.hash === "#updates") {
    modalOpen.value = false;
    bubbleOpen.value = false;
    return;
  }
  if (!isEnabled || next === null) {
    modalOpen.value = false;
    bubbleOpen.value = false;
    return;
  }
  const until = status.value?.postponed_until;
  const remaining = typeof until === "number" ? until - Date.now() : 0;
  const sharedPostponed = status.value?.postponed_version === next &&
    remaining > 0 && remaining <= UPDATE_REMINDER_POSTPONE_MS;
  if (!sharedPostponed && !updateReminderWasDismissed(next)) {
    modalOpen.value = true;
    bubbleOpen.value = false;
    return;
  }
  modalOpen.value = false;
  bubbleOpen.value = !updateBubbleWasDismissed(next);
};

watch(
  [version, enabled, () => status.value?.postponed_until, () => route.fullPath],
  ([next, isEnabled]) => evaluateReminder(next, isEnabled),
  { immediate: true },
);

// 回到窗口 / 重新可见 / 网络恢复：各投递一次。顺便重算弹窗——「以后再说」的
// 24 小时可能在窗口一直开着的时候到期。下载/重启进行中不动弹窗。
const onReturn = (trigger: UpdateCheckTrigger): void => {
  postCheck(trigger);
  if (busy.value === null && !restarting.value && !modalOpen.value) {
    evaluateReminder(version.value, enabled.value);
  }
};
const onFocus = (): void => onReturn("focus");
const onOnline = (): void => onReturn("online");
const onVisibility = (): void => {
  if (globalThis.document.visibilityState === "visible") onReturn("visible");
};
const onOpenUpdates = (): void => {
  modalOpen.value = false;
  bubbleOpen.value = false;
  void router.push(`${props.to}#updates`);
};

onMounted(() => {
  globalThis.addEventListener("focus", onFocus);
  globalThis.addEventListener("online", onOnline);
  globalThis.addEventListener("chat-archive:open-update-settings", onOpenUpdates);
  globalThis.document.addEventListener("visibilitychange", onVisibility);
});
onBeforeUnmount(() => {
  globalThis.removeEventListener("focus", onFocus);
  globalThis.removeEventListener("online", onOnline);
  globalThis.removeEventListener("chat-archive:open-update-settings", onOpenUpdates);
  globalThis.document.removeEventListener("visibilitychange", onVisibility);
});

const remember = (next: UpdateStatus): void => {
  queryClient.setQueryData(["update-status"], next);
};

const errorText = (error: unknown, fallback: string): string =>
  error instanceof ArchiveApiError ? error.message : fallback;

const download = async (): Promise<void> => {
  const target = offered.value;
  if (target === null) return;
  busy.value = "download";
  actionError.value = null;
  try {
    remember(await api.applyUpdate(target));
  } catch (error: unknown) {
    actionError.value = errorText(error, "下载没有成功，当前版本没有变。");
  } finally {
    busy.value = null;
  }
};

const backgroundDownload = async (): Promise<void> => {
  if (!await postpone()) return;
  const value = version.value;
  if (value !== null) {
    dismissUpdateBubble(value);
  }
  modalOpen.value = false;
  bubbleOpen.value = false;
};

const restartAndInstall = async (): Promise<void> => {
  const target = staged.value;
  if (target === null) return;
  busy.value = "restart";
  actionError.value = null;
  try {
    await api.installUpdateNow(target);
    restarting.value = true;
  } catch (error: unknown) {
    actionError.value = errorText(error, "暂时无法重启，退出聊记时仍会自动安装。");
  } finally {
    busy.value = null;
  }
};

const closeBubble = (): void => {
  const value = version.value;
  if (value !== null) dismissUpdateBubble(value);
  bubbleOpen.value = false;
};

const goToSettings = (): void => {
  closeBubble();
};
</script>

<template>
  <div class="update-entry">
    <RouterLink
      data-feedback-action="设置"
      class="topbar-settings topbar-icon"
      :to="to"
      title="设置"
      :aria-current="current ? 'page' : undefined"
      @click="goToSettings"
    >
      <svg
        width="18"
        height="18"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="1.8"
        stroke-linecap="round"
        stroke-linejoin="round"
        aria-hidden="true"
      >
        <circle
          cx="12"
          cy="12"
          r="3"
        />
        <path d="M19.4 15a1.7 1.7 0 0 0 .34 1.87l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.7 1.7 0 0 0-1.87-.34 1.7 1.7 0 0 0-1.03 1.56V21a2 2 0 1 1-4 0v-.09a1.7 1.7 0 0 0-1.11-1.56 1.7 1.7 0 0 0-1.87.34l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.7 1.7 0 0 0 .34-1.87 1.7 1.7 0 0 0-1.56-1.03H3a2 2 0 1 1 0-4h.09a1.7 1.7 0 0 0 1.56-1.11 1.7 1.7 0 0 0-.34-1.87l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.7 1.7 0 0 0 1.87.34h.01A1.7 1.7 0 0 0 10 3.09V3a2 2 0 1 1 4 0v.09a1.7 1.7 0 0 0 1.03 1.56 1.7 1.7 0 0 0 1.87-.34l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.7 1.7 0 0 0-.34 1.87v.01A1.7 1.7 0 0 0 20.91 10H21a2 2 0 1 1 0 4h-.09a1.7 1.7 0 0 0-1.51 1z" />
      </svg>
      <span class="topbar-icon-label">设置</span>
      <span
        v-if="hasUpdate"
        class="update-entry-dot"
        aria-label="有新版本"
      />
    </RouterLink>

    <aside
      v-if="bubbleOpen && version !== null"
      class="update-entry-bubble"
      role="status"
    >
      <button
        type="button"
        aria-label="关闭更新提示"
        @click="closeBubble"
      >
        ×
      </button>
      <strong>新版本 {{ version }} 随时可以装</strong>
      <span>前往「设置 → 应用更新」。</span>
      <RouterLink
        :to="`${to}#updates`"
        @click="goToSettings"
      >
        去更新 ›
      </RouterLink>
    </aside>
  </div>

  <Teleport to="body">
    <div
      v-if="modalOpen && version !== null"
      class="update-reminder-scrim"
    >
      <section
        ref="dialog"
        class="update-reminder"
        role="dialog"
        aria-modal="true"
        aria-label="应用更新"
        @keydown="onKeydown"
      >
        <div
          class="update-reminder-mark"
          aria-hidden="true"
        >
          痕
        </div>
        <strong v-if="staged !== null">{{ staged }} 已下好</strong>
        <strong v-else>聊记有新版本 {{ version }}</strong>
        <p v-if="restarting">
          正在关闭聊记，安装窗口马上出现……
        </p>
        <p v-else-if="staged !== null">
          重启安装大约 1 分钟，期间会显示进度
        </p>
        <p v-else>
          当前 {{ status?.current_version }} · 下载时可以继续用
        </p>

        <span
          v-if="busy === 'download'"
          class="update-reminder-progress"
          role="progressbar"
          aria-label="正在下载新版本"
        ><i /></span>
        <p
          v-if="actionError !== null"
          class="update-reminder-error"
          role="alert"
        >
          {{ actionError }}
        </p>

        <div
          v-if="!restarting"
          class="update-reminder-actions"
        >
          <button
            ref="initial"
            class="button secondary update-reminder-later"
            type="button"
            :disabled="busy === 'restart' || postponing"
            @click="busy === 'download' ? backgroundDownload() : postpone()"
          >
            {{ postponing ? "正在保存…" : busy === "download" ? "后台下载" : staged !== null ? "退出时再装" : "以后再说" }}
          </button>
          <button
            class="button primary update-reminder-primary"
            type="button"
            :disabled="busy !== null || postponing"
            @click="staged !== null ? restartAndInstall() : download()"
          >
            {{ busy === "download" ? "正在下载…" : busy === "restart" ? "正在准备重启…" : staged !== null ? "重启并安装" : "立即更新" }}
          </button>
        </div>
      </section>
    </div>
  </Teleport>
</template>
