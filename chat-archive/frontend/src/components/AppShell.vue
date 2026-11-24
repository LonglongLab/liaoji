<script setup lang="ts">
import { useQueryClient } from "@tanstack/vue-query";
import {
  BarChart2,
  Check,
  ChevronDown,
  LayoutGrid,
  Link2,
  MessageCircle,
  MessageSquare,
  Pin,
  Search,
  Settings,
  Upload,
  X,
} from "@lucide/vue";
import {
  computed,
  nextTick,
  onBeforeUnmount,
  onMounted,
  ref,
  toRef,
  watch,
} from "vue";
import { useRoute, useRouter } from "vue-router";

import { useArchiveApi } from "@/api/injection";
import brandMark from "@/assets/brand/liaoji-mark.svg";
import AppAction from "@/components/ui/AppAction.vue";
import OpenRemoteWeb from "@/components/OpenRemoteWeb.vue";
import AvatarBadge from "@/components/AvatarBadge.vue";
import ImportProgressBanner from "@/components/ImportProgressBanner.vue";
import SyncProgressBanner from "@/components/SyncProgressBanner.vue";
import UpdateEntryPrompt from "@/components/UpdateEntryPrompt.vue";
import NativeWindowControls from "@/components/NativeWindowControls.vue";
import { useAccountSwitcher } from "@/composables/useAccountSwitcher";
import {
  canOpenRemoteWeb,
  capabilityState,
  topbarEntry,
  visibleNavItems,
  workspaceShellMeta,
} from "@/lib/capabilities";
import { openPlatformFeedback } from "@/lib/platformFeedback";
import { useCapabilityStore } from "@/stores/capabilities";

const props = withDefaults(defineProps<{
  variant: "onboarding" | "app";
  accountName: string | undefined;
  mode?: "local" | "remote";
}>(), {
  mode: "local",
});

const route = props.variant === "app" ? useRoute() : null;
const router = props.variant === "app" ? useRouter() : null;
const api = props.variant === "app" ? useArchiveApi() : null;
const queryClient = props.variant === "app" ? useQueryClient() : null;
const shellMeta = computed(() => workspaceShellMeta(props.mode));
const accountSwitcherOpen = ref(false);
const nativeWindowReady = ref(false);
const nativeControls = ref<InstanceType<typeof NativeWindowControls> | null>(null);
const accountSwitcherTrigger = ref<HTMLElement | null>(null);
const accountSwitcherWrap = ref<HTMLElement | null>(null);

const closeAccountSwitcher = (restoreFocus = false): void => {
  accountSwitcherOpen.value = false;
  if (restoreFocus) {
    void nextTick(() => accountSwitcherTrigger.value?.focus());
  }
};

const switcher = useAccountSwitcher({
  enabled: props.variant === "app",
  workspace: props.mode,
  accountName: toRef(props, "accountName"),
  route,
  router,
  api,
  queryClient,
  onAfterSwitch: () => closeAccountSwitcher(true),
  onBeforeAdd: () => closeAccountSwitcher(false),
});

const switcherEntries = switcher.entries;
const switcherAvailable = switcher.available;
const workspaceAccountId = switcher.workspaceAccountId;
const accountLabel = switcher.accountLabel;
const accountAvatar = switcher.accountAvatar;
const switcherBusy = switcher.busy;
const pinnedAccountLost = switcher.pinnedAccountLost;
const pinnedEntry = switcher.pinnedEntry;
const pinningAvailable = switcher.pinningAvailable;
const togglePin = switcher.togglePin;
const onSelectEntry = switcher.selectEntry;
const addAccount = switcher.addAccount;

/**
 * 这一页自己排满整块工作区、自己管滚动吗？
 *
 * 判定从路由 `meta.canvas` 来，不再是这里写死的一串路径——PSU-2.66 把全局搜索
 * 改成三栏画布时就漏掉了它，于是那一页被套进 `.app-main` 的 36/44 留白里，
 * 范围栏没有像原型那样紧贴图标栏。加一页就得回来改一次的名单，迟早会漏。
 */
const isWorkspaceCanvas = computed(() => route?.meta.canvas === true);
const currentPath = computed(() => route?.path ?? "");

// 菜单、顶栏入口和工作区切换全部从集中清单派生（`@/lib/capabilities`）。
const capabilities = useCapabilityStore();

const navItems = computed(() =>
  visibleNavItems(capabilities.deployment, capabilities.states, props.mode),
);
const NAV_ICONS = {
  overview: LayoutGrid,
  conversations: MessageSquare,
  search: Search,
  export: Upload,
  shares: Link2,
  stats: BarChart2,
} as const;
// 顶栏两个位置也从清单派生：远程端的设置屏是另一条路由，写死 `/settings`
// 就会把它漏掉，而漏掉的入口在界面上看不出来。
const healthEntry = computed(() =>
  topbarEntry(capabilities.deployment, capabilities.states, props.mode, "health"),
);
const settingsEntry = computed(() =>
  topbarEntry(capabilities.deployment, capabilities.states, props.mode, "settings"),
);
const showsRemoteWebEntry = computed(() =>
  canOpenRemoteWeb(capabilities.deployment, capabilities.states),
);
/**
 * 提交反馈没有自己的目的地，它是别的屏上的一个按钮，所以不走 `topbarEntry`
 * ——那一套是给「有 surface 的能力」用的。远程端按归属表判 unsupported，
 * 入口在那一端永远不出现（那半个界面跑在别人的手机浏览器里，它既出不去，
 * 也不该替这台机器的用户发反馈）。这是声明过的 profile 差异，不是漏实现。
 */
const feedbackAvailable = computed(
  () =>
    props.variant === "app" &&
    capabilityState(capabilities.deployment, capabilities.states, "feedback") !==
      "unsupported",
);
/**
 * 反馈 UI 由平台统一下发（`SCN-FWB-118`），本仓只挂这一个入口。
 */
const openFeedback = (): void => {
  if (api === null) return;
  void openPlatformFeedback(api, router);
};

const toggleAccountSwitcher = (): void => {
  if (!switcherAvailable.value) return;
  accountSwitcherOpen.value = !accountSwitcherOpen.value;
  if (accountSwitcherOpen.value) switcher.clearError();
};

const openAccountSwitcher = (): void => {
  if (accountSwitcherOpen.value || !switcherAvailable.value) return;
  accountSwitcherTrigger.value?.focus();
  toggleAccountSwitcher();
};

// 按钮语义只在真能切换时挂上。远程端要等 v3 远端报出账号清单才算「真能切换」
// ——旧远端读侧没有账号维度，一个点不开的 `aria-haspopup` 会把这一枚能做什么
// 说错：「不属于这一端的入口不出现」，不是「出现了但没反应」。
const switcherTriggerBindings = computed(() =>
  switcherAvailable.value
    ? {
        type: "button",
        "aria-haspopup": "dialog",
        "aria-expanded": accountSwitcherOpen.value ? "true" : "false",
        "aria-controls": "account-switcher",
        onClick: toggleAccountSwitcher,
      }
    : {},
);

const onDocumentPointerDown = (event: PointerEvent): void => {
  const target = event.target;
  if (
    accountSwitcherOpen.value &&
    target instanceof Node &&
    accountSwitcherWrap.value !== null &&
    !accountSwitcherWrap.value.contains(target)
  ) {
    closeAccountSwitcher(false);
  }
};

const onDocumentKeyDown = (event: KeyboardEvent): void => {
  if (event.key === "Escape" && accountSwitcherOpen.value) {
    event.preventDefault();
    closeAccountSwitcher(true);
  }
};

if (route !== null) {
  watch(
    () => route.fullPath,
    () => closeAccountSwitcher(false),
  );
}

onMounted(() => {
  globalThis.document.addEventListener("pointerdown", onDocumentPointerDown);
  globalThis.document.addEventListener("keydown", onDocumentKeyDown);
});

onBeforeUnmount(() => {
  globalThis.document.removeEventListener("pointerdown", onDocumentPointerDown);
  globalThis.document.removeEventListener("keydown", onDocumentKeyDown);
});
</script>

<template>
  <div class="app-frame">
    <a
      class="skip-link"
      href="#main-content"
    >跳到主要内容</a>

    <div
      v-if="variant === 'onboarding'"
      class="onboarding-shell"
    >
      <div
        class="onboarding-topbar"
        :class="{ 'has-native-controls': nativeWindowReady }"
      >
        <div
          class="brand-row"
          aria-label="聊记，本地聊天档案"
        >
          <img
            class="brand-mark"
            :src="brandMark"
            alt=""
            aria-hidden="true"
          >
          <div class="brand-name">
            聊记
          </div>
        </div>
        <div
          v-if="nativeWindowReady"
          class="native-title-drag pywebview-drag-region"
          aria-hidden="true"
          @dblclick="nativeControls?.toggleMaximize()"
        />
        <NativeWindowControls
          ref="nativeControls"
          @ready="nativeWindowReady = $event"
        />
      </div>
      <main
        id="main-content"
        class="onboarding-step"
        tabindex="-1"
      >
        <slot />
      </main>
    </div>

    <template v-else>
      <header
        class="app-topbar"
        :class="{ 'has-native-controls': nativeWindowReady }"
      >
        <div
          class="brand"
          :aria-label="shellMeta.brandAriaLabel"
        >
          <img
            class="brand-mark"
            :src="brandMark"
            alt=""
            aria-hidden="true"
          >
          <div class="brand-name">
            聊记
          </div>
        </div>
        <div
          class="topbar-divider"
          aria-hidden="true"
        />
        <div
          v-if="accountLabel"
          ref="accountSwitcherWrap"
          class="account-switcher-wrap"
        >
          <component
            :is="switcherAvailable ? 'button' : 'div'"
            ref="accountSwitcherTrigger"
            data-feedback-action="切换账号"
            class="account-chip"
            :class="{ 'account-switcher-trigger': switcherAvailable }"
            v-bind="switcherTriggerBindings"
          >
            <AvatarBadge
              :name="accountLabel ?? ''"
              :src="accountAvatar"
              :alt="`${accountLabel ?? '当前账号'}的头像`"
              size="sm"
            />
            <span data-feedback-mask="account-name">{{ accountLabel }}</span>
            <ChevronDown
              v-if="switcherAvailable"
              data-feedback-unmask="system-icon"
              class="account-switcher-chevron"
              :size="12"
              :stroke-width="1.5"
              aria-hidden="true"
            />
          </component>
          <div
            v-if="accountSwitcherOpen"
            class="account-switcher-layer"
            @pointerdown.self="closeAccountSwitcher(false)"
          >
            <section
              id="account-switcher"
              class="account-switcher-sheet"
              role="dialog"
              aria-label="切换微信账号"
              :aria-busy="switcherBusy"
            >
              <header class="account-switcher-head">
                <div>
                  <strong>切换微信账号</strong>
                  <small>{{ shellMeta.switcherSubtitle }}</small>
                </div>
                <button
                  class="account-switcher-close"
                  type="button"
                  aria-label="关闭账号面板"
                  @click="closeAccountSwitcher(true)"
                >
                  <X
                    data-feedback-unmask="system-icon"
                    :size="18"
                    :stroke-width="1.8"
                    aria-hidden="true"
                  />
                </button>
              </header>
              <!-- 一行两个动作：切到这个号（主按钮）、把它钉成默认（图钉）。
                   所以这一行是个容器而不是按钮——按钮不能嵌套按钮。 -->
              <div class="account-switcher-list">
                <div
                  v-for="entry in switcherEntries"
                  :key="entry.id"
                  class="account-switcher-option"
                  data-feedback-mask="account-name"
                  :class="{ 'is-selected': entry.selected }"
                  role="group"
                  :aria-label="entry.label"
                >
                  <button
                    class="account-switcher-pick"
                    type="button"
                    :data-account-id="entry.id"
                    :aria-current="entry.selected ? 'true' : undefined"
                    :disabled="switcherBusy"
                    @click="onSelectEntry(entry.id)"
                  >
                    <AvatarBadge
                      :name="entry.label"
                      :src="entry.avatarRef"
                      :alt="`${entry.label}的头像`"
                      size="md"
                    />
                    <span class="account-switcher-copy">
                      <strong>
                        {{ entry.label }}
                        <span
                          v-if="entry.isDefault"
                          class="account-default-badge"
                        >默认</span>
                      </strong>
                      <small v-if="entry.subLabel">{{ entry.subLabel }}</small>
                    </span>
                    <Check
                      v-if="entry.selected"
                      data-feedback-unmask="system-icon"
                      :size="18"
                      :stroke-width="2"
                      aria-label="当前账号"
                    />
                  </button>
                  <button
                    v-if="pinningAvailable"
                    class="account-switcher-pin"
                    :class="{ 'is-pinned': entry.isDefault }"
                    type="button"
                    :data-pin-account-id="entry.id"
                    :aria-pressed="entry.isDefault ? 'true' : 'false'"
                    :aria-label="
                      entry.isDefault
                        ? `取消默认账号：${entry.label}`
                        : `设为默认账号：${entry.label}`
                    "
                    :disabled="switcherBusy"
                    @click="togglePin(entry.id)"
                  >
                    <Pin
                      data-feedback-unmask="system-icon"
                      :size="19"
                      :stroke-width="1.7"
                      :fill="entry.isDefault ? 'currentColor' : 'none'"
                      aria-hidden="true"
                    />
                  </button>
                </div>
              </div>
              <!-- 现在生效的是哪一条，如实写出来：设定只在这台设备上，
                   而「没设默认」与「设了但它没了」是两种不同的处境。 -->
              <p
                v-if="pinningAvailable"
                class="account-switcher-foot"
              >
                <template v-if="pinnedAccountLost">
                  原来设的默认账号已不在这份档案里，先显示「<span data-feedback-mask="account-name">{{ accountLabel }}</span>」。点图钉重新指定。
                </template>
                <template v-else-if="pinnedEntry !== null">
                  每次打开都从「<span data-feedback-mask="account-name">{{ pinnedEntry.label }}</span>」开始，只在这台设备上生效。
                  <AppAction
                    class="account-default-clear"
                    variant="inline"
                    type="button"
                    :disabled="switcherBusy"
                    @click="togglePin(pinnedEntry.id)"
                  >
                    取消默认
                  </AppAction>
                </template>
                <template v-else>
                  还没设默认，打开时显示上次看过的账号。点右边的图钉把某个号钉住。
                </template>
              </p>
              <p
                v-if="switcher.errorMessage.value !== null"
                class="account-switch-error"
                role="alert"
              >
                {{ switcher.errorMessage.value }}
              </p>
              <!-- 添加账号是本地档案的动作（要跑引导、扫源），远程端只读，
                   面板里不摆一个通向不存在流程的按钮。 -->
              <AppAction
                v-if="shellMeta.supportsAccountAdd"
                data-feedback-action="添加微信号"
                class="account-add"
                variant="secondary"
                type="button"
                :disabled="switcherBusy"
                @click="addAccount"
              >
                添加微信号
              </AppAction>
            </section>
          </div>
        </div>
        <!-- 只说这是哪一端，不再承诺「数据不上传」：开了远程同步以后那句不成立。
             挨着账号放，是因为它描述的是「你正在看的这份档案」，不是一个入口。 -->
        <span
          class="local-pill"
          :class="{ 'is-remote': shellMeta.isRemote }"
        >{{ shellMeta.pillLabel }}</span>
        <div
          v-if="nativeWindowReady"
          class="native-title-drag pywebview-drag-region"
          aria-hidden="true"
          @dblclick="nativeControls?.toggleMaximize()"
        />
        <div class="topbar-end">
          <RouterLink
            v-if="healthEntry !== null"
            data-feedback-action="同步健康"
            class="topbar-health"
            :to="healthEntry.to"
            :aria-current="currentPath === healthEntry.to ? 'page' : undefined"
          >
            同步健康
          </RouterLink>
          <OpenRemoteWeb
            v-if="showsRemoteWebEntry"
            compact
            class="topbar-workspace-switch"
          />
          <!-- 上面两个是去别的地方（文字），下面两个是这一端的工具（图标）。
               原来五个入口四种样式，看不出哪几个是一类。 -->
          <span
            v-if="settingsEntry !== null || feedbackAvailable"
            class="topbar-end-divider"
            aria-hidden="true"
          />
          <button
            v-if="feedbackAvailable"
            data-feedback-action="反馈"
            class="topbar-feedback topbar-icon"
            type="button"
            aria-label="反馈"
            title="反馈"
            @click="openFeedback"
          >
            <MessageCircle
              data-feedback-unmask="system-icon"
              :size="18"
              :stroke-width="1.8"
              aria-hidden="true"
            />
          </button>
          <UpdateEntryPrompt
            v-if="settingsEntry !== null && shellMeta.supportsUpdatePrompt"
            :to="settingsEntry.to"
            :current="currentPath === settingsEntry.to"
          />
          <RouterLink
            v-else-if="settingsEntry !== null"
            data-feedback-action="设置"
            class="topbar-settings topbar-icon"
            :to="settingsEntry.to"
            title="设置"
            :aria-current="currentPath === settingsEntry.to ? 'page' : undefined"
          >
            <Settings
              data-feedback-unmask="system-icon"
              :size="18"
              :stroke-width="1.8"
              aria-hidden="true"
            />
            <span class="topbar-icon-label">设置</span>
          </RouterLink>
        </div>
        <NativeWindowControls
          v-if="shellMeta.supportsLocalChrome"
          ref="nativeControls"
          @ready="nativeWindowReady = $event"
        />
      </header>
      <!-- 对话框不在这棵树上：widget 自己挂一个带 Shadow DOM 的宿主元素到
           `document.body`，样式与宿主页面互不影响。「只在打开时挂载」那条评审结论
           （P1-6）照旧成立，而且更彻底——关闭时整个实例连同录屏缓冲、截图附件与
           用户填的正文一起销毁，没有任何东西跟着会话常驻。 -->
      <ImportProgressBanner v-if="shellMeta.supportsLocalChrome" />
      <!-- 同步与导入并列（PSU-2.107）：两个长任务可以同时在跑，
           而「哪一个在跑」是两句不同的话，合成一条只会含糊。 -->
      <SyncProgressBanner v-if="shellMeta.supportsLocalChrome" />
      <div class="app-body">
        <nav
          class="icon-nav app-bottom-nav"
          :aria-label="shellMeta.navAriaLabel"
        >
          <template
            v-for="item in navItems"
            :key="item.id"
          >
            <RouterLink
              class="workspace-nav-link"
              data-feedback-action="侧栏导航"
              :to="item.to"
              :aria-current="currentPath === item.to ? 'page' : undefined"
              :data-capability-state="item.state"
            >
              <component
                :is="NAV_ICONS[item.icon]"
                data-feedback-unmask="system-icon"
                :size="20"
                :stroke-width="1.8"
                aria-hidden="true"
              />
              {{ item.label }}
              <!-- 属于本端但还没配好的入口照常出现，只是如实标出来；
                   藏起来会让用户以为软件没这功能。 -->
              <small
                v-if="item.state === 'not_configured'"
                class="nav-unconfigured"
              >未配置</small>
            </RouterLink>
          </template>
        </nav>
        <main
          id="main-content"
          class="app-main"
          :class="{ 'is-workspace-canvas': isWorkspaceCanvas }"
          :data-scroll-owner="isWorkspaceCanvas ? 'component' : 'page'"
          tabindex="-1"
        >
          <slot
            :workspace-account-id="workspaceAccountId"
            :open-account-switcher="switcherEntries.length > 1 ? openAccountSwitcher : undefined"
          />
        </main>
      </div>
    </template>
  </div>
</template>
