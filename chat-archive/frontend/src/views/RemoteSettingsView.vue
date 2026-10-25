<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, provide, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";

import type { BrowserSessionSummary, RemoteDeviceSummary } from "@/api/archiveApi";
import { useArchiveApi } from "@/api/injection";
import { workspaceKey } from "@/lib/workspaceContext";
import AppShell from "@/components/AppShell.vue";
import AboutSection from "@/components/AboutSection.vue";
import BackupSettingsPanel from "@/components/BackupSettingsPanel.vue";
import QrCode from "@/components/QrCode.vue";
import RemoteReauthSection from "@/components/RemoteReauthSection.vue";
import DeviceInfoIcon from "@/components/DeviceInfoIcon.vue";
import SettingsConfirm from "@/components/SettingsConfirm.vue";
import ObjectStorageSettingsPanel from "@/components/ObjectStorageSettingsPanel.vue";
import RemoteSettingsSummary from "@/components/RemoteSettingsSummary.vue";
import ServerPairingCard from "@/components/ServerPairingCard.vue";
import { capabilityState } from "@/lib/capabilities";
import { recordBrowserClientInfo } from "@/lib/browserClientInfo";
import { formatDateTimeSeparator } from "@/utils/time";
import { useCapabilityStore } from "@/stores/capabilities";
import { useRemoteAuthStore } from "@/stores/remoteAuth";

// 这一页今天不画附件，但每一条远程路由都要说得出自己是哪一端：漏一个是
// 静默的，而共用组件里那几个按工作区分叉的词随时会走到这里来。
provide(workspaceKey, "remote");

const api = useArchiveApi();
const auth = useRemoteAuthStore();
const capabilities = useCapabilityStore();
const router = useRouter();
const route = useRoute();

const media = globalThis.matchMedia("(max-width: 47.99rem)");
const mobile = ref(media.matches);
const onMediaChange = (event: MediaQueryListEvent): void => { mobile.value = event.matches; };
media.addEventListener("change", onMediaChange);
onBeforeUnmount(() => media.removeEventListener("change", onMediaChange));
const stateOf = (id: "totp" | "browser-session" | "device-revocation" | "remote-backup" | "object-storage-config" | "backup-management" | "pairing-code") =>
  capabilityState(capabilities.deployment, capabilities.states, id);
const groups = [
  { id: "security", label: "登录与安全", capability: "browser-session" },
  { id: "devices", label: "同步设备", capability: "device-revocation" },
  { id: "remote", label: "远程与备份", capability: null },
  { id: "about", label: "关于", capability: null },
] as const;
type GroupId = (typeof groups)[number]["id"];
const visibleGroups = computed(() => groups.filter(group => group.id === "remote"
  ? stateOf("object-storage-config") !== "unsupported" || stateOf("remote-backup") !== "unsupported"
  : group.capability === null || stateOf(group.capability) !== "unsupported"));
const subpage = computed(() => route.hash === "#storage" ? "storage" : route.hash === "#backup" ? "backup" : null);
const selectedGroup = computed<GroupId | null>(() => {
  const hash = subpage.value === null ? route.hash.slice(1) : "remote";
  const group = visibleGroups.value.find(entry => entry.id === hash);
  return group?.id ?? (mobile.value ? null : visibleGroups.value[0]?.id ?? null);
});
const heading = computed(() => subpage.value === "storage" ? "附件存储" : subpage.value === "backup" ? "定期备份" : groups.find(group => group.id === selectedGroup.value)?.label ?? "设置");
const frame = ref<HTMLElement | null>(null);
const go = (group: GroupId | null): void => { void router.push({ path: "/remote/settings", hash: group === null ? "" : `#${group}` }); };
watch([() => route.hash, visibleGroups, () => capabilities.loaded, () => capabilities.authState], ([hash]) => {
  if (capabilities.loaded && capabilities.authState === "authenticated" &&
    ((subpage.value === "backup" && stateOf("remote-backup") === "unsupported") ||
     (subpage.value === "storage" && stateOf("object-storage-config") === "unsupported"))) {
    void router.replace({ path: "/remote/settings", hash: "#security" });
  }
  // Locked/anonymous responses intentionally omit capabilities; they do not retire settings categories.
  if (capabilities.loaded && capabilities.authState === "authenticated" && hash !== "" && groups.some(group => `#${group.id}` === hash) && !visibleGroups.value.some(group => `#${group.id}` === hash)) {
    void router.replace({ path: "/remote/settings", hash: visibleGroups.value[0] ? `#${visibleGroups.value[0].id}` : "" });
  }
  void nextTick(() => {
    if (frame.value && frame.value.getBoundingClientRect().top < 0) frame.value.scrollIntoView({ block: "start" });
  });
}, { immediate: true });

const sessions = ref<BrowserSessionSummary[]>([]);
const devices = ref<RemoteDeviceSummary[]>([]);
const loadError = ref<string | null>(null);
const totpCode = ref("");
const openRemoteConfig = (item: "object-storage" | "backup"): void => { void router.push({ path: "/remote/settings", hash: item === "object-storage" ? "#storage" : "#backup" }); };
const pairingActive = ref(false);
const disablePassword = ref("");
const enrollPassword = ref("");
const busy = ref(false);
const loading = ref(true);
const totpExpanded = ref(false);
const removedSessions = new Set<string>();
const removedDevices = new Set<string>();
const activeDevices = computed(() => devices.value.filter(device => device.status === "active"));
type Detail = { kind: "session"; item: BrowserSessionSummary } | { kind: "device"; item: RemoteDeviceSummary };
const detail = ref<Detail | null>(null);
const confirmation = ref<Detail | null>(null);
const confirmAll = ref(false);
const sessionName = (session: BrowserSessionSummary): string => {
  const info = session.client_info;
  if (!info) return "未记录设备";
  if (info.model) return info.model;
  if (info.platform === "iOS") return info.device_type === "tablet" ? "iPad" : "iPhone";
  const kind = info.device_type === "mobile" ? "手机" : info.device_type === "tablet" ? "平板" : "电脑";
  return info.platform ? `${info.platform} ${kind}` : "未记录设备";
};
const deviceName = (device: RemoteDeviceSummary): string => device.client_info?.computer_name ?? device.display_name;
const detailName = (target: Detail): string => target.kind === "session" ? sessionName(target.item) : deviceName(target.item);
const detailFields = computed(() => {
  if (detail.value === null) return [];
  const target = detail.value;
  if (target.kind === "session") return [
    ["浏览器", target.item.client_info?.browser ?? "未记录"],
    ["系统", target.item.client_info?.platform ?? "未记录"],
    ["设备型号", target.item.client_info?.model ?? "浏览器未提供"],
    ["登录时间", formatDateTimeSeparator(target.item.created_at)],
    ["最近活跃", formatDateTimeSeparator(target.item.last_seen_at)],
    ["闲置到期", formatDateTimeSeparator(target.item.idle_expires_at)],
    ["最晚到期", formatDateTimeSeparator(target.item.absolute_expires_at)],
  ];
  return [
    ["电脑名", target.item.client_info?.computer_name ?? "采集端尚未上报"],
    ["系统", target.item.client_info?.platform ?? "未记录"],
    ["客户端", target.item.client_info?.client_name === "chat-archive" ? "聊记桌面端" : target.item.client_info?.client_name ?? "未记录"],
    ["配对名称", target.item.display_name],
    ["配对时间", formatDateTimeSeparator(target.item.created_at)],
    ["最近连接", target.item.last_seen_at === null ? "尚未连接" : formatDateTimeSeparator(target.item.last_seen_at)],
  ];
});

const totpEnabled = computed(() => stateOf("totp") === "available");
const failureText = computed(() => auth.failure?.message ?? null);
const computerPaired = async (): Promise<void> => { capabilities.loaded = false; await capabilities.load(api); await refresh(); };

const refresh = async (): Promise<void> => {
  try {
    const [sessionPage, devicePage] = await Promise.all([
      stateOf("browser-session") === "available" ? api.listBrowserSessions() : Promise.resolve({ items: [] }),
      stateOf("device-revocation") === "available" ? api.listRemoteDevices() : Promise.resolve({ items: [] }),
    ]);
    sessions.value = sessionPage.items.filter(item => !removedSessions.has(item.id))
      .sort((left, right) => Number(right.current) - Number(left.current) || right.last_seen_at - left.last_seen_at);
    devices.value = devicePage.items.filter(item => !removedDevices.has(item.id));
    loadError.value = null;
  } catch (error: unknown) {
    loadError.value =
      error instanceof Error && error.message !== ""
        ? error.message
        : "会话与设备列表暂时读不到。";
  } finally { loading.value = false; }
};

onMounted(() => {
  void refresh();
  void recordBrowserClientInfo(api).then(recorded => { if (recorded) return refresh(); });
});

/**
 * 撤销类操作全部经这里。
 *
 * 从前它们没有任何 catch，也不写 `loadError`——一次被拒的撤销就是彻底的静默
 * 无操作，界面上唯一的错误出口在 `refresh()` 里，而那时根本走不到。
 */
const attempt = async (action: () => Promise<void>, whatFailed: string): Promise<boolean> => {
  if (busy.value) return false;
  busy.value = true;
  try {
    await action();
    loadError.value = null;
    return true;
  } catch (error: unknown) {
    loadError.value =
      error instanceof Error && error.message !== "" ? error.message : whatFailed;
    return false;
  } finally {
    busy.value = false;
  }
};

const requestRevoke = (): void => { confirmation.value = detail.value; detail.value = null; };
const revokeSelected = async (): Promise<void> => {
  const target = confirmation.value;
  if (target === null) return;
  const success = await attempt(
    () => target.kind === "session" ? api.revokeBrowserSession(target.item.id) : api.revokeRemoteSyncDevice(target.item.id),
    "撤销没有成功，请稍后重试。",
  );
  if (!success) return;
  if (target.kind === "session") {
    removedSessions.add(target.item.id);
    sessions.value = sessions.value.filter(item => item.id !== target.item.id);
  } else {
    removedDevices.add(target.item.id);
    devices.value = devices.value.filter(item => item.id !== target.item.id);
  }
  confirmation.value = null;
  await refresh();
};

const revokeAll = async (): Promise<void> => {
  if (!(await attempt(() => api.revokeAllBrowserSessions().then(() => undefined), "退出所有设备没有成功。"))) {
    return;
  }
  // 包括当前这一条，所以接下来必然是未登录状态。
  await auth.signOut(api);
  await router.replace("/login");
};

const signOut = async (): Promise<void> => {
  await auth.signOut(api);
  await router.replace("/login");
};

const startEnrollment = async (): Promise<void> => {
  if (enrollPassword.value === "") return;
  const ok = await auth.beginTotpEnrollment(api, enrollPassword.value);
  if (ok) enrollPassword.value = "";
};

const enableTotp = async (): Promise<void> => {
  if (totpCode.value.trim() === "") return;
  const ok = await auth.enableTotp(api, totpCode.value);
  if (ok) totpCode.value = "";
};

const disableTotp = async (): Promise<void> => {
  if (disablePassword.value === "") return;
  try {
    await api.disableTotp(disablePassword.value);
    disablePassword.value = "";
    auth.reset();
    capabilities.loaded = false;
    await capabilities.load(api);
  } catch (error: unknown) {
    loadError.value =
      error instanceof Error && error.message !== "" ? error.message : "解绑失败。";
  }
};
</script>
<template>
  <AppShell
    variant="app"
    mode="remote"
    :account-name="undefined"
  >
    <section
      ref="frame"
      class="settings-page remote-settings-frame"
    >
      <nav
        class="settings-sidebar"
        aria-label="设置分类"
      >
        <h1 class="settings-sidebar-title">
          设置
        </h1>
        <button
          v-for="group in visibleGroups"
          :key="group.id"
          class="settings-group-button"
          :class="{ 'is-current': selectedGroup === group.id }"
          :aria-current="selectedGroup === group.id ? 'page' : undefined"
          type="button"
          @click="go(group.id)"
        >
          {{ group.label }}
        </button>
      </nav>
      <div class="settings-workspace">
        <header class="settings-workspace-header remote-settings-header">
          <button
            v-if="subpage !== null || (mobile && selectedGroup !== null)"
            class="settings-back"
            type="button"
            :aria-label="subpage !== null ? '返回远程与备份' : '返回设置首页'"
            data-testid="remote-settings-back"
            @click="go(subpage !== null ? 'remote' : null)"
          >
            ‹
          </button>
          <component :is="mobile ? 'h1' : 'h2'">
            {{ heading }}
          </component>
        </header>
        <div class="settings-workspace-scroll remote-settings-content">
          <p
            v-if="loadError !== null"
            class="notice-warn"
            role="alert"
          >
            {{ loadError }}
            <button
              class="text-action"
              type="button"
              @click="refresh"
            >
              重新加载
            </button>
          </p>
          <p
            v-if="failureText !== null"
            class="notice-warn"
            role="alert"
          >
            {{ failureText }}
          </p>
          <section
            v-if="auth.recoveryCodes.length > 0"
            class="settings-list remote-recovery-codes"
            data-testid="settings-recovery-codes"
          >
            <p>这 {{ auth.recoveryCodes.length }} 个恢复码<strong>只显示这一次</strong>，请保存在这台设备之外。</p>
            <ul
              class="auth-recovery-codes"
              data-feedback-mask="recovery-code"
            >
              <li
                v-for="code in auth.recoveryCodes"
                :key="code"
              >
                <code>{{ code }}</code>
              </li>
            </ul>
          </section>

          <section
            v-show="selectedGroup === null"
            data-testid="remote-settings-home"
            class="remote-settings-home"
          >
            <p class="remote-settings-caption">
              管理远程归档的登录、设备与备份。
            </p>
            <div class="settings-list">
              <button
                v-for="group in visibleGroups"
                :key="group.id"
                class="remote-settings-entry"
                type="button"
                :data-testid="`settings-entry-${group.id}`"
                @click="go(group.id)"
              >
                <span><strong>{{ group.label }}</strong>
                  <small v-if="group.id === 'security'">{{ loading ? '正在读取登录设备…' : `${sessions.length} 个登录设备` }}</small>
                  <small v-else-if="group.id === 'devices'">{{ stateOf('device-revocation') === 'not_configured' ? '归档尚未配置' : loading ? '正在读取设备…' : `${activeDevices.length} 台电脑已配对` }}</small>
                  <small v-else-if="group.id === 'remote'">附件存储与定期备份</small>
                  <small v-else-if="group.id === 'about'">版本、许可证与答谢</small>
                  <small v-else>{{ stateOf('remote-backup') === 'not_configured' ? '尚未配置备份' : '查看备份结果' }}</small>
                </span><span
                  class="remote-settings-chevron"
                  aria-hidden="true"
                >›</span>
              </button>
            </div>
            <button
              class="button secondary remote-settings-sign-out"
              type="button"
              :disabled="auth.busy || busy"
              data-feedback-action="退出登录"
              @click="signOut"
            >
              退出登录
            </button>
          </section>

          <section
            v-if="stateOf('totp') !== 'unsupported'"
            v-show="selectedGroup === 'security' || auth.enrollment !== null"
            class="settings-group remote-security-panel"
          >
            <h3 class="settings-group-title">
              登录保护
            </h3>
            <div class="settings-list">
              <div class="settings-row">
                <div class="settings-row-copy">
                  <strong>两步验证</strong>
                </div>
                <span
                  class="status-badge"
                  :class="totpEnabled ? 'is-safe' : 'is-neutral'"
                >{{ totpEnabled ? '已开启' : '未开启' }}</span>
                <button
                  class="text-action"
                  type="button"
                  :aria-expanded="totpExpanded"
                  aria-controls="remote-totp-controls"
                  data-testid="settings-totp-manage"
                  @click="totpExpanded = !totpExpanded"
                >
                  {{ totpExpanded ? '收起' : '管理' }}
                </button>
              </div>
              <div
                v-show="totpExpanded || auth.enrollment !== null"
                id="remote-totp-controls"
                class="remote-auth-controls"
              >
                <template v-if="totpEnabled">
                  <p class="action-note">
                    解绑需要登录口令，恢复码会同时作废。
                  </p>
                  <form
                    class="auth-form"
                    @submit.prevent="disableTotp"
                  >
                    <label
                      class="auth-label"
                      for="settings-disable-password"
                    >登录口令</label>
                    <input
                      id="settings-disable-password"
                      v-model="disablePassword"
                      type="password"
                      autocomplete="current-password"
                    >
                    <button
                      data-feedback-action="解绑两步验证"
                      class="button secondary"
                      type="submit"
                      :disabled="disablePassword === ''"
                    >
                      解绑两步验证
                    </button>
                  </form>
                </template>
                <template v-else>
                  <p class="action-note">
                    用验证器为登录增加一道保护。
                  </p>
                  <template v-if="auth.enrollment === null">
                    <form
                      class="auth-form"
                      @submit.prevent="startEnrollment"
                    >
                      <label
                        class="auth-label"
                        for="settings-enroll-password"
                      >登录口令</label>
                      <input
                        id="settings-enroll-password"
                        v-model="enrollPassword"
                        type="password"
                        autocomplete="current-password"
                        aria-describedby="settings-enroll-note"
                      >
                      <p
                        id="settings-enroll-note"
                        class="action-note auth-note"
                      >
                        输入登录口令以绑定验证器。
                      </p>
                      <button
                        data-feedback-action="开始绑定"
                        class="button primary"
                        type="submit"
                        :disabled="auth.busy || enrollPassword === ''"
                      >
                        开始绑定
                      </button>
                    </form>
                  </template>
                  <template v-else>
                    <QrCode
                      :value="auth.enrollment.otpauth_uri"
                      label="两步验证绑定二维码"
                      data-feedback-mask="authentication-secret"
                    />
                    <p class="action-note">
                      扫不了自己的屏幕时，
                      <a
                        :href="auth.enrollment.otpauth_uri"
                        data-feedback-mask="authentication-secret"
                      >点这里唤起验证器</a>，
                      或手工录入密钥：
                    </p>
                    <p
                      class="auth-secret"
                      data-feedback-mask="authentication-secret"
                    >
                      <code>{{ auth.enrollment.secret }}</code>
                    </p>
                    <form
                      class="auth-form"
                      @submit.prevent="enableTotp"
                    >
                      <label
                        class="auth-label"
                        for="settings-totp"
                      >回填一个当前验证码</label>
                      <input
                        id="settings-totp"
                        v-model="totpCode"
                        type="text"
                        inputmode="numeric"
                        autocomplete="one-time-code"
                      >
                      <button
                        data-feedback-action="确认开启两步验证"
                        class="button primary"
                        type="submit"
                        :disabled="auth.busy || totpCode.trim() === ''"
                      >
                        确认开启
                      </button>
                    </form>
                  </template>
                </template>
              </div>
            </div>
          </section>

          <RemoteReauthSection
            v-if="stateOf('browser-session') === 'available'"
            v-show="selectedGroup === 'security'"
          />

          <section
            v-show="selectedGroup === 'security'"
            data-testid="remote-settings-security"
            class="settings-group"
          >
            <h3 class="settings-group-title">
              已登录设备 · {{ sessions.length }}
            </h3>
            <p
              v-if="loading"
              class="remote-settings-caption"
              role="status"
            >
              正在读取登录设备…
            </p>
            <ul
              v-else-if="sessions.length > 0"
              class="settings-list auth-list remote-device-list"
            >
              <li
                v-for="session in sessions"
                :key="session.id"
                class="auth-list-row remote-device-row"
                data-testid="login-device-row"
              >
                <DeviceInfoIcon :kind="session.client_info?.device_type ?? 'unknown'" />
                <div class="remote-device-copy">
                  <div class="remote-device-title">
                    <strong data-feedback-mask="device-info">{{ sessionName(session) }}</strong>
                    <span
                      v-if="session.current"
                      class="status-badge is-neutral"
                    >本机</span>
                  </div>
                  <small data-feedback-mask="device-info">{{ [session.client_info?.browser, session.client_info?.platform].filter(Boolean).join(' · ') }}{{ session.client_info ? ' · ' : '' }}{{ formatDateTimeSeparator(session.last_seen_at) }} 活跃</small>
                </div>
                <button
                  class="remote-device-detail"
                  type="button"
                  :aria-label="`${sessionName(session)}详情`"
                  data-feedback-mask="device-info"
                  @click="detail = { kind: 'session', item: session }"
                >
                  ›
                </button>
              </li>
            </ul>
            <p
              v-else-if="loadError === null"
              class="remote-settings-caption"
            >
              暂无登录设备
            </p>
            <div class="remote-settings-actions">
              <button
                class="button secondary"
                type="button"
                :disabled="auth.busy || busy"
                data-feedback-action="退出登录"
                @click="signOut"
              >
                退出登录
              </button>
              <button
                class="button secondary danger"
                type="button"
                :disabled="auth.busy || busy"
                data-feedback-action="退出所有设备"
                @click="confirmAll = true"
              >
                退出所有设备
              </button>
            </div>
          </section>

          <section
            v-show="selectedGroup === 'devices'"
            data-testid="remote-settings-devices"
            class="settings-group remote-pairing-panel"
          >
            <h3
              v-if="stateOf('pairing-code') !== 'available'"
              class="settings-group-title"
            >
              同步设备
            </h3>
            <ServerPairingCard
              v-if="stateOf('pairing-code') === 'available'"
              @paired="computerPaired"
              @active="pairingActive = $event"
            />
            <p
              v-if="stateOf('device-revocation') === 'not_configured'"
              class="info-note"
            >
              <RouterLink
                class="button"
                to="/setup"
              >
                完成首次设置
              </RouterLink>
            </p>
            <p
              v-else-if="loading"
              class="remote-settings-caption"
              role="status"
            >
              正在读取同步设备…
            </p>
            <ul
              v-else-if="!pairingActive && devices.length > 0"
              class="settings-list auth-list remote-device-list"
            >
              <li
                v-for="device in devices"
                :key="device.id"
                class="auth-list-row remote-device-row"
                data-testid="sync-device-row"
              >
                <DeviceInfoIcon
                  kind="desktop"
                  sync
                />
                <button
                  class="remote-device-copy remote-device-detail"
                  type="button"
                  :aria-label="`${deviceName(device)}详情`"
                  data-feedback-mask="device-info"
                  @click="detail = { kind: 'device', item: device }"
                >
                  <strong data-feedback-mask="device-info">{{ deviceName(device) }}</strong>
                  <small data-feedback-mask="device-info">{{ device.client_info?.platform ? `${device.client_info.platform} · ` : '' }}{{ device.last_seen_at === null ? '尚未连接' : `${formatDateTimeSeparator(device.last_seen_at)} 连接` }}</small>
                </button>
                <span
                  class="remote-device-state"
                  :class="{ revoked: device.status !== 'active' }"
                >{{ device.status === 'active' ? '在用' : '已撤销' }}</span>
                <button
                  v-if="device.status === 'active'"
                  class="remote-device-revoke"
                  type="button"
                  :disabled="busy"
                  title="停止该电脑同步，已上传记录保留"
                  :aria-label="`撤销 ${deviceName(device)}`"
                  data-feedback-mask="device-info"
                  @click="confirmation = { kind: 'device', item: device }"
                >
                  撤销
                </button>
              </li>
            </ul>
            <p
              v-else-if="!pairingActive && loadError === null"
              class="remote-settings-caption"
            >
              没有已配对的同步设备
            </p>
          </section>

          <RemoteSettingsSummary
            v-if="selectedGroup === 'remote' && subpage === null"
            mode="browser"
            :shows-object-storage="stateOf('object-storage-config') !== 'unsupported'"
            :shows-backup="stateOf('remote-backup') !== 'unsupported'"
            @open="openRemoteConfig"
          />
          <section
            v-if="selectedGroup === 'remote' && subpage === 'storage' && stateOf('object-storage-config') !== 'unsupported'"
            class="settings-group"
            data-testid="remote-settings-storage"
          >
            <ObjectStorageSettingsPanel mode="browser" />
          </section>
          <section
            v-if="selectedGroup === 'remote' && subpage === 'backup' && stateOf('remote-backup') !== 'unsupported'"
            data-testid="remote-settings-backup"
            class="settings-group remote-backup-panel"
          >
            <BackupSettingsPanel :mode="stateOf('backup-management') === 'available' ? 'browser' : 'status'" />
          </section>
          <AboutSection v-if="selectedGroup === 'about'" />
        </div>
      </div>
    </section>

    <SettingsConfirm
      v-if="detail !== null"
      :title="detailName(detail)"
      @close="detail = null"
    >
      <dl
        class="remote-device-facts"
      >
        <template
          v-for="[label, value] in detailFields"
          :key="label"
        >
          <dt>{{ label }}</dt><dd data-feedback-mask="device-info">
            {{ value }}
          </dd>
        </template>
      </dl>
      <p
        v-if="detail.kind === 'session' && detail.item.current"
        class="action-note"
      >
        当前登录用“退出登录”结束。
      </p>
      <template #actions>
        <button
          class="button secondary"
          type="button"
          @click="detail = null"
        >
          关闭
        </button>
        <button
          v-if="detail.kind === 'device' || !detail.item.current"
          class="button secondary danger"
          type="button"
          data-testid="device-detail-revoke"
          @click="requestRevoke"
        >
          撤销此设备
        </button>
      </template>
    </SettingsConfirm>
    <SettingsConfirm
      v-if="confirmation !== null"
      :title="`撤销“${detailName(confirmation)}”？`"
      @close="confirmation = null"
    >
      <p>{{ confirmation.kind === 'device' ? '需要重新配对才能继续上传，已上传记录会保留。' : '该浏览器需要重新登录。' }}</p>
      <p
        v-if="loadError !== null"
        class="notice-warn"
        role="alert"
      >
        {{ loadError }}
      </p>
      <template #actions>
        <button
          class="button secondary"
          type="button"
          :disabled="busy"
          @click="confirmation = null"
        >
          取消
        </button>
        <button
          class="button secondary danger"
          type="button"
          :disabled="busy"
          data-testid="device-confirm-revoke"
          @click="revokeSelected"
        >
          {{ busy ? '正在撤销…' : '确认撤销' }}
        </button>
      </template>
    </SettingsConfirm>
    <SettingsConfirm
      v-if="confirmAll"
      title="退出所有登录设备？"
      @close="confirmAll = false"
    >
      <p>包括当前登录，所有浏览器都需要重新登录。同步电脑的配对不受影响。</p>
      <p
        v-if="loadError !== null"
        class="notice-warn"
        role="alert"
      >
        {{ loadError }}
      </p>
      <template #actions>
        <button
          class="button secondary"
          type="button"
          :disabled="busy"
          @click="confirmAll = false"
        >
          取消
        </button>
        <button
          class="button secondary danger"
          type="button"
          :disabled="busy"
          @click="revokeAll"
        >
          确认退出所有登录设备
        </button>
      </template>
    </SettingsConfirm>
  </AppShell>
</template>

<style scoped>
.remote-device-state{display:inline-flex;align-items:center;gap:6px;white-space:nowrap;color:#1d6a45;font-size:13px}.remote-device-state::before{content:"";width:7px;height:7px;border-radius:50%;background:currentColor}.remote-device-state.revoked{color:var(--ink-soft)}.remote-device-revoke{min-height:44px;border:0;border-radius:10px;padding:4px 8px;background:none;color:var(--ink-soft);font:inherit;font-size:13px;cursor:pointer}.remote-device-revoke:hover{background:#b2412f12;color:#b2412f}.remote-device-row{flex-wrap:wrap}.remote-device-copy{flex:1;min-width:60px}.remote-device-revoke:focus-visible{outline:2px solid #1d6a45;outline-offset:2px}
.remote-pairing-panel{box-sizing:border-box;max-width:640px;padding:20px 24px;border:1px solid #e9e3d6;border-radius:18px;background:var(--surface-pop)}.remote-pairing-panel .settings-list{border:0;background:transparent;margin-top:12px}.remote-pairing-panel .remote-device-row{padding:10px 0;min-height:56px;flex-wrap:nowrap;gap:8px;border-top:1px solid var(--rule-soft)}.remote-pairing-panel .remote-device-detail{display:flex;flex-direction:column;align-items:flex-start;justify-content:center;flex:1;width:auto;min-width:0;max-width:none;padding:0;text-align:left}.remote-pairing-panel .remote-device-copy strong{max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.remote-pairing-panel .remote-device-copy small{font-weight:400;font-size:12px}.remote-pairing-panel .remote-device-revoke{flex:none}.remote-pairing-panel .remote-device-state{flex:none}@media(max-width:600px){.remote-pairing-panel{padding:16px}}
</style>
