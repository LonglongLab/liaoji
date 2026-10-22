<script setup lang="ts">
import { useQueryClient } from "@tanstack/vue-query";
import {
  computed,
  nextTick,
  onBeforeUnmount,
  onMounted,
  ref,
  watch,
} from "vue";
import { useRoute, useRouter } from "vue-router";

import { useArchiveApi } from "@/api/injection";
import brandMark from "@/assets/brand/liaoji-mark.svg";
import OpenRemoteWeb from "@/components/OpenRemoteWeb.vue";
import AvatarBadge from "@/components/AvatarBadge.vue";
import ImportProgressBanner from "@/components/ImportProgressBanner.vue";
import SyncProgressBanner from "@/components/SyncProgressBanner.vue";
import UpdateEntryPrompt from "@/components/UpdateEntryPrompt.vue";
import {
  useRemoteAccountScope,
  useRemoteArchiveStatusQuery,
} from "@/composables/remoteArchiveQueries";
import { useLocalAccountSwitch } from "@/composables/useLocalAccountSwitch";
import { useRemoteAvatars } from "@/composables/useRemoteAvatars";
import {
  capabilityState,
  topbarEntry,
  visibleNavItems,
} from "@/lib/capabilities";
import { openPlatformFeedback } from "@/lib/platformFeedback";
import { sharedAvatarStoreVia } from "@/lib/remoteAvatarStore";
import { useCapabilityStore } from "@/stores/capabilities";
import { useOnboardingStore } from "@/stores/onboarding";
import { useRemoteArchiveUiStore } from "@/stores/remoteArchiveUi";

const props = withDefaults(defineProps<{
  variant: "onboarding" | "app";
  accountName: string | undefined;
  mode?: "local" | "remote";
}>(), {
  mode: "local",
});

const route = props.variant === "app" ? useRoute() : null;
const router = props.variant === "app" ? useRouter() : null;
const onboarding = useOnboardingStore();
const remoteUi = useRemoteArchiveUiStore();
const isLocalApp = props.variant === "app" && props.mode === "local";
const isRemoteApp = props.variant === "app" && props.mode === "remote";
const api = props.variant === "app" ? useArchiveApi() : null;
const queryClient = props.variant === "app" ? useQueryClient() : null;
const switchLocalAccount = props.variant === "app" ? useLocalAccountSwitch() : null;
const accountSwitcherOpen = ref(false);
const accountSwitcherTrigger = ref<HTMLElement | null>(null);
const accountSwitcherWrap = ref<HTMLElement | null>(null);

// 远程工作区的账号维度（记录模型 v3，PSU-2.35b）。归档标识与账号清单都问
// 后端自己（与视图共享同一份查询缓存），不从视图传 prop——切换器住在外壳里，
// 数据也该在外壳里问齐。旧远端 / 未配置 / 空归档答不出账号，清单为空，
// 下面的一切回落到只读归档标识——不放假入口。
const remoteStatusQuery =
  isRemoteApp && api !== null ? useRemoteArchiveStatusQuery(api) : null;
const remoteArchiveId = computed(
  () => remoteStatusQuery?.data.value?.archive_id ?? null,
);
const remoteConfigured = computed(
  () => remoteStatusQuery?.data.value?.configured === true,
);
/**
 * **外壳与视图必须看同一个作用域**（PSU-2.65）。
 *
 * 远程会话页把 URL 里的 `account` 排在 store 之前（2.46：一条发过来的链接要在
 * 收的人那边打开同一个账号）。外壳这一层原来不看 URL，于是两边可以给出不同的
 * 答案：打开一条别人发来的链接时，顶栏印的是**默认账号**的名字，正文却是链接
 * 里那个账号的会话——顶栏在说一件不成立的事。
 */
const routeAccountId = computed(() => {
  const value: unknown = route?.query.account;
  if (typeof value === "string") return value === "" ? null : value;
  return null;
});
const remoteScope =
  isRemoteApp && api !== null
    ? useRemoteAccountScope(api, remoteArchiveId, remoteConfigured, routeAccountId)
    : null;

/** 两端切换面板共用的一行；差别全部折进这一层，模板只有一份。 */
interface SwitcherEntry {
  id: string;
  label: string;
  subLabel: string | null;
  avatarRef: string | null;
  selected: boolean;
  /** 钉住的默认账号（PSU-2.119）：每次打开都从它开始。 */
  isDefault: boolean;
}

const localEntries = computed<SwitcherEntry[]>(() =>
  onboarding.accounts.map((account) => ({
    id: account.id,
    label: account.display_name,
    subLabel: account.source_label ?? null,
    avatarRef: account.avatar_ref,
    selected: account.id === onboarding.selectedSourceId,
    isDefault: account.id === onboarding.defaultAccountId,
  })),
);

// 名字有三个来源，按「谁最有资格说」排：
//
// 1. **归档自己报的**（记录模型 v5，PSU-2.40）——那是这个账号随自己的批次
//    送上来的，远程站点上也读得到，所以它排第一；
// 2. **同一个 id 在本地档案里的名字**——id 相等即同一账号，这是本机自己的
//    知识，不是替远端断言。桌面端上这一条能补上还没同步过名字的账号；
// 3. 都没有就如实显示不透明标识，不编造。
//
// 这与「不拿本地选中账号给远程档案署名」不矛盾：那是把 A 的名字贴在 B 上，
// 这里自始至终是 A 认出了 A。头像仍只有本地档案给得出——归档里没有那份字节。
const remoteEntries = computed<SwitcherEntry[]>(() =>
  (remoteScope?.accounts.value ?? []).map((item) => {
    const local = onboarding.accounts.find(
      (account) => account.id === item.source_account_id,
    );
    return {
      id: item.source_account_id,
      label: item.display_name ?? local?.display_name ?? item.source_account_id,
      subLabel: `${item.record_count.toLocaleString("zh-CN")} 条消息`,
      // 归档自己送上来的那一张排第一（记录模型 v10）；同一个 id 的本地档案
      // 仍然兜底——那是本机认出了同一个账号，不是替远端断言。
      avatarRef: remoteAvatarSrc(item.avatar_blob_id) ?? local?.avatar_ref ?? null,
      selected: item.source_account_id === remoteScope?.selectedAccountId.value,
      isDefault: item.source_account_id === remoteScope?.pinnedAccountId.value,
    };
  }),
);

const switcherEntries = computed<SwitcherEntry[]>(() =>
  isLocalApp ? localEntries.value : remoteEntries.value,
);
// 本机端总有账号可切；远程端只有 v3 远端真报了账号清单才有。
const switcherAvailable = computed(
  () => isLocalApp || (isRemoteApp && remoteEntries.value.length > 0),
);
const selectedRemoteEntry = computed(
  () => remoteEntries.value.find((entry) => entry.selected) ?? null,
);

// 名字与头像：本机端取自 onboarding；远程端取当前选中账号（能借到本地名字
// 就用名字），没有账号维度时回落到不透明归档标识——那就是这一端能说的全部。
const accountLabel = computed(() =>
  isLocalApp
    ? (onboarding.selectedAccount?.display_name ?? props.accountName)
    : (selectedRemoteEntry.value?.label ?? props.accountName),
);
// 同名的两个账号靠头像分辨。归档里没存本账号的头像时是 null，
// `AvatarBadge` 会退回首字母——那种情况下这一栏帮不上忙，但也不会假装有图。
const accountAvatar = computed(() =>
  isLocalApp
    ? (onboarding.selectedAccount?.avatar_ref ?? null)
    : (selectedRemoteEntry.value?.avatarRef ?? null),
);
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
// 此前这里是两个写死的数组，端类型靠父组件传 prop——新增一项功能就得记得
// 回来改一次，漏一处就是一个坏按钮。现在端类型由后端下发，归属由清单决定。
const capabilities = useCapabilityStore();

/**
 * 远程账号的头像（记录模型 v10，PSU-2.55）。
 *
 * 字节与附件、与会话行上的头像走**同一条路**（预签名 URL 直取对象存储），
 * 所以「这一端能不能取附件」就是「能不能取头像」：归档配了但没配对象存储时
 * 两者一起没有，那时连订阅都不发起——发起了也只是排一次注定失败的请求。
 *
 * 此前这一栏是拿远程账号 id 去**本地档案**里借 `avatar_ref`。桌面端上偶尔借
 * 得到，而自托管远端上根本没有本地档案，于是永远只剩首字母。现在头像随账号
 * 身份上行，这一端自己就有。
 */
const remoteAttachmentsAvailable = computed(
  () =>
    isRemoteApp &&
    capabilityState(capabilities.deployment, capabilities.states, "attachment-view") ===
      "available",
);
// 全应用共用的那一份（PSU-2.122）。这一处尤其明显：`AppShell` 是**每个视图各
// 渲染一个**，此前每次导航都把顶栏那张账号头像重取一遍。
const remoteAvatars =
  isRemoteApp && api !== null
    ? useRemoteAvatars(sharedAvatarStoreVia((blobId) => api.getRemoteAttachmentUrl(blobId)))
    : null;
const remoteAvatarSrc = (blobId: string | null | undefined): string | null =>
  remoteAvatars === null || !remoteAttachmentsAvailable.value
    ? null
    : remoteAvatars.srcFor(blobId);

// 想要哪几张图，按**集合**整体换：`retain` 是替换语义，不在列表里的立即退订。
watch(
  () => ({
    blobIds: (remoteScope?.accounts.value ?? []).map((item) => item.avatar_blob_id),
    available: remoteAttachmentsAvailable.value,
  }),
  ({ blobIds, available }) => {
    remoteAvatars?.retain(available ? blobIds : []);
  },
  { immediate: true, deep: true },
);

// 换归档**不再全部回收**（PSU-2.122）：`avatar_blob_id` 是账号加盐算出来的，
// 同一个 id 的字节永远是同一份，跨归档也不会撞；而 store 现在是全应用共用的，
// 在这里清等于把会话页正在显示的图一起扔掉。上面那条 retain 已经把不再需要的
// 标识退订，该回收的照样回收。
const navItems = computed(() =>
  visibleNavItems(capabilities.deployment, capabilities.states, props.mode),
);
// 顶栏两个位置也从清单派生：远程端的设置屏是另一条路由，写死 `/settings`
// 就会把它漏掉，而漏掉的入口在界面上看不出来。
const healthEntry = computed(() =>
  topbarEntry(capabilities.deployment, capabilities.states, props.mode, "health"),
);
const settingsEntry = computed(() =>
  topbarEntry(capabilities.deployment, capabilities.states, props.mode, "settings"),
);
const showsRemoteWebEntry = computed(() =>
  capabilities.deployment === "local" &&
  capabilityState(capabilities.deployment, capabilities.states, "archive-status") !== "unsupported",
);
/**
 * 提交反馈没有自己的目的地，它是别的屏上的一个按钮，所以不走 `topbarEntry`
 * ——那一套是给「有 surface 的能力」用的。远程端按归属表判 unsupported，
 * 入口在那一端**永远不出现**（那半个界面跑在别人的手机浏览器里，它既出不去，
 * 也不该替这台机器的用户发反馈）。这是声明过的 profile 差异，不是漏实现。
 */
const feedbackAvailable = computed(
  () =>
    props.variant === "app" &&
    capabilityState(capabilities.deployment, capabilities.states, "feedback") !==
      "unsupported",
);
/**
 * 反馈 UI **由平台统一下发**（`SCN-FWB-118`），本仓只挂这一个入口。
 *
 * 这里没有 `feedbackOpen` 这类开关：widget 挂载即打开、关闭即自毁，
 * 「现在开着吗」的唯一真值是 `platformFeedback.ts` 里那个模块级句柄，
 * 而它由 widget 的 `onDestroyed` 钩子清掉。外壳再存一份布尔值就会有两个真值
 * ——关闭时只清了其中一个的症状是入口从此点不开，而且控制台一行报错都没有。
 */
const openFeedback = (): void => {
  if (api === null) return;
  void openPlatformFeedback(api, router);
};

const closeAccountSwitcher = (restoreFocus = false): void => {
  accountSwitcherOpen.value = false;
  if (restoreFocus) {
    void nextTick(() => accountSwitcherTrigger.value?.focus());
  }
};

const toggleAccountSwitcher = (): void => {
  if (!switcherAvailable.value) return;
  accountSwitcherOpen.value = !accountSwitcherOpen.value;
  if (accountSwitcherOpen.value && isLocalApp) onboarding.clearAccountSwitchError();
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

const switcherBusy = computed(() => isLocalApp && onboarding.accountSwitchBusy);

/**
 * 「打开时显示哪个号」（PSU-2.119）。两端各自把自己的那一份接上来，判断与
 * 存取都在 `lib/defaultAccount`——面板模板只有一份，这里也只该有一处分叉。
 */
const pinnedAccountId = computed(() =>
  isLocalApp ? onboarding.defaultAccountId : (remoteScope?.pinnedAccountId.value ?? null),
);
const pinnedAccountLost = computed(() =>
  isLocalApp ? onboarding.pinnedAccountLost : (remoteScope?.pinnedAccountLost.value ?? false),
);
const pinnedEntry = computed(
  () => switcherEntries.value.find((entry) => entry.isDefault) ?? null,
);
// 只有一个账号时没有「哪一个」可言，不摆一个按了也看不出区别的开关。
const pinningAvailable = computed(() => switcherEntries.value.length > 1);

// 设默认与切账号是两件事：点图钉不切换、也不关面板——钉完要能当场看见那枚标。
const togglePin = (entryId: string): void => {
  const next = pinnedAccountId.value === entryId ? null : entryId;
  if (isLocalApp) {
    onboarding.pinDefaultAccount(next);
    return;
  }
  // 远程端的账号是现算的：不先把「就停在这个号」说出来，钉一下就会把还没手动
  // 切过账号的人整个挪到另一个号的会话列表上（`holdAccount` 里有详述）。
  remoteUi.holdAccount(remoteScope?.selectedAccountId.value ?? null);
  remoteUi.pinDefaultAccount(next);
};

const selectAccount = async (accountId: string): Promise<void> => {
  if (switchLocalAccount === null || onboarding.accountSwitchBusy) return;
  if (!(await switchLocalAccount(accountId))) return;
  closeAccountSwitcher(true);
};

// 远程端的切换是纯本地状态：作用域进查询键，vue-query 自然去拉新账号的页。
// 旧账号作用域的缓存照本地切换的口径清掉——留着只是白占内存，而且切回去
// 也该重新按需加载，而不是掏出一份可能已过期的旧页。
const selectRemoteAccount = async (accountId: string): Promise<void> => {
  if (remoteScope === null || queryClient === null) return;
  const previousAccountId = remoteScope.selectedAccountId.value;
  if (previousAccountId === accountId) {
    closeAccountSwitcher(true);
    return;
  }
  remoteUi.selectAccount(accountId);
  if (previousAccountId !== null) {
    const belongsToPreviousAccount = (queryKey: readonly unknown[]): boolean =>
      queryKey.includes(previousAccountId);
    await queryClient.cancelQueries({
      predicate: (query) => belongsToPreviousAccount(query.queryKey),
    });
    queryClient.removeQueries({
      predicate: (query) => belongsToPreviousAccount(query.queryKey),
    });
  }
  closeAccountSwitcher(true);
};

const onSelectEntry = (entryId: string): void => {
  if (isLocalApp) {
    void selectAccount(entryId);
  } else {
    void selectRemoteAccount(entryId);
  }
};

const addAccount = (): void => {
  if (!isLocalApp || router === null) return;
  closeAccountSwitcher(false);
  onboarding.startAdditionalArchive();
  void router.push("/start");
};

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
      <main
        id="main-content"
        class="onboarding-step"
        tabindex="-1"
      >
        <slot />
      </main>
    </div>

    <template v-else>
      <header class="app-topbar">
        <div
          class="brand"
          :aria-label="mode === 'remote' ? '聊记，远程只读聊天档案' : '聊记，本地聊天档案'"
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
            <svg
              v-if="switcherAvailable"
              data-feedback-unmask="system-icon"
              class="account-switcher-chevron"
              width="12"
              height="12"
              viewBox="0 0 12 12"
              fill="none"
              stroke="currentColor"
              stroke-width="1.5"
              aria-hidden="true"
            >
              <path d="m3 4.5 3 3 3-3" />
            </svg>
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
                  <small>{{
                    isLocalApp
                      ? "只显示本地档案中的真实账号"
                      : "只显示已同步到这个远程归档的账号"
                  }}</small>
                </div>
                <button
                  class="account-switcher-close"
                  type="button"
                  aria-label="关闭账号面板"
                  @click="closeAccountSwitcher(true)"
                >
                  <svg
                    data-feedback-unmask="system-icon"
                    width="18"
                    height="18"
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="currentColor"
                    stroke-width="1.8"
                    stroke-linecap="round"
                    aria-hidden="true"
                  >
                    <path d="M6 6l12 12M18 6 6 18" />
                  </svg>
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
                    <svg
                      v-if="entry.selected"
                      data-feedback-unmask="system-icon"
                      width="18"
                      height="18"
                      viewBox="0 0 24 24"
                      fill="none"
                      stroke="currentColor"
                      stroke-width="2"
                      stroke-linecap="round"
                      stroke-linejoin="round"
                      aria-label="当前账号"
                    >
                      <path d="m5 12 4 4L19 6" />
                    </svg>
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
                    <svg
                      data-feedback-unmask="system-icon"
                      width="19"
                      height="19"
                      viewBox="0 0 24 24"
                      :fill="entry.isDefault ? 'currentColor' : 'none'"
                      stroke="currentColor"
                      stroke-width="1.7"
                      stroke-linecap="round"
                      stroke-linejoin="round"
                      aria-hidden="true"
                    >
                      <path d="M12 17v5" />
                      <path d="M9 10.8V4h6v6.8l2.5 3.2H6.5z" />
                    </svg>
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
                  <button
                    class="account-default-clear"
                    type="button"
                    :disabled="switcherBusy"
                    @click="togglePin(pinnedEntry.id)"
                  >
                    取消默认
                  </button>
                </template>
                <template v-else>
                  还没设默认，打开时显示上次看过的账号。点右边的图钉把某个号钉住。
                </template>
              </p>
              <p
                v-if="isLocalApp && onboarding.accountSwitchError"
                class="account-switch-error"
                role="alert"
              >
                {{ onboarding.accountSwitchError.message }}
              </p>
              <!-- 添加账号是本地档案的动作（要跑引导、扫源），远程端只读，
                   面板里不摆一个通向不存在流程的按钮。 -->
              <button
                v-if="isLocalApp"
                data-feedback-action="添加微信号"
                class="account-add"
                type="button"
                :disabled="switcherBusy"
                @click="addAccount"
              >
                添加微信号
              </button>
            </section>
          </div>
        </div>
        <!-- 只说这是哪一端，不再承诺「数据不上传」：开了远程同步以后那句不成立。
             挨着账号放，是因为它描述的是「你正在看的这份档案」，不是一个入口。 -->
        <span
          class="local-pill"
          :class="{ 'is-remote': mode === 'remote' }"
        >{{ mode === "remote" ? "远程只读" : "本机" }}</span>
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
            <svg
              data-feedback-unmask="system-icon"
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
              <path d="M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.4A8 8 0 1 1 21 12z" />
            </svg>
          </button>
          <UpdateEntryPrompt
            v-if="settingsEntry !== null && isLocalApp"
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
            <svg
              data-feedback-unmask="system-icon"
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
              <path
                d="M19.4 15a1.7 1.7 0 0 0 .34 1.87l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.7 1.7 0 0 0-1.87-.34 1.7 1.7 0 0 0-1.03 1.56V21a2 2 0 1 1-4 0v-.09a1.7 1.7 0 0 0-1.11-1.56 1.7 1.7 0 0 0-1.87.34l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.7 1.7 0 0 0 .34-1.87 1.7 1.7 0 0 0-1.56-1.03H3a2 2 0 1 1 0-4h.09a1.7 1.7 0 0 0 1.56-1.11 1.7 1.7 0 0 0-.34-1.87l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.7 1.7 0 0 0 1.87.34h.01A1.7 1.7 0 0 0 10 3.09V3a2 2 0 1 1 4 0v.09a1.7 1.7 0 0 0 1.03 1.56 1.7 1.7 0 0 0 1.87-.34l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.7 1.7 0 0 0-.34 1.87v.01A1.7 1.7 0 0 0 20.91 10H21a2 2 0 1 1 0 4h-.09a1.7 1.7 0 0 0-1.51 1z"
              />
            </svg>
            <span class="topbar-icon-label">设置</span>
          </RouterLink>
        </div>
      </header>
      <!-- 对话框不在这棵树上：widget 自己挂一个带 Shadow DOM 的宿主元素到
           `document.body`，样式与宿主页面互不影响。「只在打开时挂载」那条评审结论
           （P1-6）照旧成立，而且更彻底——关闭时整个实例连同录屏缓冲、截图附件与
           用户填的正文一起销毁，没有任何东西跟着会话常驻。 -->
      <ImportProgressBanner v-if="isLocalApp" />
      <!-- 同步与导入并列（PSU-2.107）：两个长任务可以同时在跑，
           而「哪一个在跑」是两句不同的话，合成一条只会含糊。 -->
      <SyncProgressBanner v-if="isLocalApp" />
      <div class="app-body">
        <nav
          class="icon-nav app-bottom-nav"
          :aria-label="mode === 'remote' ? '远程档案导航' : '主导航'"
        >
          <template
            v-for="item in navItems"
            :key="item.id"
          >
            <RouterLink
              data-feedback-action="侧栏导航"
              :to="item.to"
              :aria-current="currentPath === item.to ? 'page' : undefined"
              :data-capability-state="item.state"
            >
              <svg
                data-feedback-unmask="system-icon"
                width="20"
                height="20"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                stroke-width="1.8"
                stroke-linecap="round"
                stroke-linejoin="round"
                aria-hidden="true"
              >
                <path
                  v-if="item.icon === 'conversations'"
                  d="M4 5h16v10H9l-5 4V5z"
                />
                <template v-else-if="item.icon === 'search'">
                  <circle
                    cx="11"
                    cy="11"
                    r="6.5"
                  /><path d="M20 20l-4.3-4.3" />
                </template>
                <template v-else-if="item.icon === 'export'">
                  <path d="M12 3v12" /><path d="M7 8l5-5 5 5" /><path
                    d="M4 15v4a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-4"
                  />
                </template>
                <template v-else-if="item.icon === 'shares'">
                  <path d="M10 14a4 4 0 0 0 5.66 0l3-3a4 4 0 0 0-5.66-5.66l-1 1" />
                  <path d="M14 10a4 4 0 0 0-5.66 0l-3 3a4 4 0 0 0 5.66 5.66l1-1" />
                </template>
                <template v-else-if="item.icon === 'stats'">
                  <path d="M4 20V10" /><path d="M12 20V4" /><path d="M20 20v-7" />
                </template>
                <template v-else>
                  <rect
                    x="3"
                    y="3"
                    width="7"
                    height="7"
                    rx="1.5"
                  /><rect
                    x="14"
                    y="3"
                    width="7"
                    height="7"
                    rx="1.5"
                  /><rect
                    x="3"
                    y="14"
                    width="7"
                    height="7"
                    rx="1.5"
                  /><rect
                    x="14"
                    y="14"
                    width="7"
                    height="7"
                    rx="1.5"
                  />
                </template>
              </svg>
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
          <slot />
        </main>
      </div>
    </template>
  </div>
</template>
