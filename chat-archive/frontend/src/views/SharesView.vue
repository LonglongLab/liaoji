<script setup lang="ts">
import { ChevronDown, CircleHelp, EllipsisVertical, Eye } from "@lucide/vue";
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { RouterLink } from "vue-router";

import { ArchiveApiError, type RemoteShare } from "@/api/archiveApi";
import { useArchiveApi } from "@/api/injection";
import AppShell from "@/components/AppShell.vue";
import RemoteUnconfiguredCard from "@/components/RemoteUnconfiguredCard.vue";
import { useModalFocus } from "@/composables/useModalFocus";
import { capabilityState } from "@/lib/capabilities";
import {
  shareContentWords,
  shareMoment,
  shareStatusWords,
  shareTier,
  shareViewsWords,
} from "@/lib/share/format";
import { currentOrigin, shareLink } from "@/lib/share/runShare";
import { useCapabilityStore } from "@/stores/capabilities";
import { useOnboardingStore } from "@/stores/onboarding";
import { copyText } from "@/utils/clipboard";

/**
 * 「我的分享」（PSU-2.182，原型第 4 块）。本机在本地工作区「导出」下面，远程网页在
 * 远程工作区的导航里；同一个页面，打的是同一组 `/api/v1/remote/shares*`。
 *
 * 只列生成好的分享。打开次数只有数字与时刻，不记是谁。删掉是真删：先确认，确认框里
 * 把三件事说清楚——链接立刻失效、服务器上的副本一并删除、归档里的原消息不受影响。
 */
const props = defineProps<{ mode: "local" | "remote" }>();

const api = useArchiveApi();
const capabilities = useCapabilityStore();
const onboarding = useOnboardingStore();

const accountName = computed(() =>
  props.mode === "local" ? onboarding.selectedAccount?.display_name : undefined,
);
const shareState = computed(() =>
  capabilityState(capabilities.deployment, capabilities.states, "share"),
);

const shares = ref<RemoteShare[] | null>(null);
const loadError = ref<string | null>(null);
const actionError = ref<string | null>(null);
const notice = ref<string | null>(null);
const busyToken = ref<string | null>(null);
const pendingDelete = ref<RemoteShare | null>(null);
const deleteError = ref<string | null>(null);
const page = ref<HTMLElement | null>(null);
let noticeTimer: ReturnType<typeof globalThis.setTimeout> | undefined;
let active = true;
const now = ref(Math.floor(Date.now() / 1000));
const conversationPath = computed(() => props.mode === "local" ? "/conversations" : "/remote/conversations");
const closeDelete = (): void => {
  if (busyToken.value !== null) return;
  pendingDelete.value = null;
  deleteError.value = null;
};
const { container: deleteDialog, initial: cancelButton, onKeydown: onDeleteKeydown } = useModalFocus(
  computed(() => pendingDelete.value !== null), closeDelete,
);
void deleteDialog;
void cancelButton;
const popoverSelector = ".shares-help, .share-card-details, .share-card-more";
const closeMenus = (except?: HTMLDetailsElement): void => {
  page.value?.querySelectorAll<HTMLDetailsElement>(popoverSelector).forEach(panel => {
    if (panel !== except) panel.open = false;
  });
};
const onPopoverToggle = (event: Event): void => {
  const panel = event.currentTarget;
  if (panel instanceof HTMLDetailsElement && panel.open) closeMenus(panel);
};
const onPopoverClick = (event: MouseEvent): void => {
  if (!(event.target instanceof Element)) return;
  const summary = event.target.closest("summary");
  const panel = summary?.parentElement;
  // Close before native activation, including keyboard-generated clicks.
  // Delayed toggle events from closed panels cannot replace the new panel.
  if (panel instanceof HTMLDetailsElement && panel.matches(popoverSelector)) closeMenus(panel);
};
const onOutsidePointer = (event: PointerEvent): void => {
  if (!(event.target instanceof Element) || event.target.closest(popoverSelector) === null) closeMenus();
};
const onEscape = (event: KeyboardEvent): void => { if (event.key === "Escape") closeMenus(); };
const showNotice = (text: string): void => {
  if (!active) return;
  globalThis.clearTimeout(noticeTimer);
  notice.value = text;
  noticeTimer = globalThis.setTimeout(() => { notice.value = null; noticeTimer = undefined; }, 2500);
};
const openDelete = (share: RemoteShare, event: Event): void => {
  if (busyToken.value !== null) return;
  if (event.currentTarget instanceof HTMLElement) {
    event.currentTarget.closest("details")?.querySelector<HTMLElement>("summary")?.focus();
  }
  closeMenus();
  deleteError.value = null;
  pendingDelete.value = share;
};

const messageOf = (error: unknown, fallback: string): string =>
  error instanceof ArchiveApiError ? error.message : fallback;

const load = async (): Promise<void> => {
  loadError.value = null;
  try {
    shares.value = await api.listShares();
    now.value = Math.floor(Date.now() / 1000);
  } catch (error: unknown) {
    loadError.value = messageOf(error, "没能读到分享列表，请稍后再试。");
  }
};

onMounted(() => {
  globalThis.document.addEventListener("pointerdown", onOutsidePointer);
  globalThis.document.addEventListener("keydown", onEscape);
  if (shareState.value !== "not_configured") void load();
});
onBeforeUnmount(() => {
  active = false;
  globalThis.clearTimeout(noticeTimer);
  globalThis.document.removeEventListener("pointerdown", onOutsidePointer);
  globalThis.document.removeEventListener("keydown", onEscape);
});

/** 页头右边两个数：只数读到的列表，没有分享时不出。 */
const totals = computed(() => {
  const list = shares.value;
  if (list === null || list.length === 0) return null;
  return {
    live: list.filter((share) => share.state !== "expired").length,
    views: list.reduce((sum, share) => sum + share.views, 0),
  };
});

const replace = (updated: RemoteShare): void => {
  shares.value = (shares.value ?? []).map((item) => (item.token === updated.token ? updated : item));
};

const copyLink = async (share: RemoteShare): Promise<void> => {
  actionError.value = null;
  const ok = await copyText(shareLink(share, currentOrigin()));
  if (ok) showNotice("已复制链接");
  else {
    globalThis.clearTimeout(noticeTimer);
    notice.value = null;
    actionError.value = "没能复制链接，请重试。";
  }
};

const extend = async (share: RemoteShare): Promise<void> => {
  if (busyToken.value !== null) return;
  closeMenus();
  busyToken.value = share.token;
  actionError.value = null;
  try {
    const updated = await api.extendShare(share.token);
    now.value = Math.floor(Date.now() / 1000);
    replace(updated);
    showNotice(updated.expires_at === null ? "永久有效" : "已延期");
  } catch (error: unknown) {
    actionError.value = messageOf(error, "没能延期，请稍后再试。");
  } finally {
    busyToken.value = null;
  }
};

const confirmDelete = async (): Promise<void> => {
  const share = pendingDelete.value;
  if (share === null || busyToken.value !== null) return;
  busyToken.value = share.token;
  actionError.value = null;
  deleteError.value = null;
  try {
    await api.deleteShare(share.token);
    shares.value = (shares.value ?? []).filter((item) => item.token !== share.token);
    showNotice("已删除");
    pendingDelete.value = null;
  } catch (error: unknown) {
    deleteError.value = messageOf(error, "没能删除，请重试。");
  } finally {
    busyToken.value = null;
  }
};
</script>

<template>
  <AppShell
    variant="app"
    :mode="mode"
    :account-name="accountName"
  >
    <section
      ref="page"
      class="shares-page"
      @click.capture="onPopoverClick"
    >
      <header class="shares-head">
        <div class="shares-heading">
          <h1 class="page-title">
            我的分享
          </h1>
          <details
            class="shares-help"
            @toggle="onPopoverToggle"
          >
            <summary aria-label="分享说明">
              <CircleHelp aria-hidden="true" />
            </summary>
            <div class="shares-help-panel">
              <p>在会话中多选消息，即可生成分享链接。</p>
              <p>打开统计只记录次数与时间，不记录访客身份。</p>
              <p>删除后链接失效，原消息保留。</p>
            </div>
          </details>
        </div>
        <dl
          v-if="totals !== null"
          class="shares-stats"
        >
          <div>
            <dt>个有效分享</dt>
            <dd>{{ totals.live }}</dd>
          </div>
          <div>
            <dt>累计打开</dt>
            <dd>{{ totals.views }}</dd>
          </div>
        </dl>
      </header>

      <RemoteUnconfiguredCard
        v-if="shareState === 'not_configured'"
        heading="还没有接入远程归档"
        summary="分享页要放在你的远程归档上，别人才打得开。接入之后就能在多选消息时生成链接。"
      />

      <template v-else>
        <div
          v-if="loadError !== null"
          class="shares-error"
          role="alert"
        >
          {{ loadError }}
          <button
            type="button"
            class="shares-link-button"
            @click="load"
          >
            重试
          </button>
        </div>
        <p
          v-if="actionError !== null"
          class="shares-error"
          role="alert"
        >
          {{ actionError }}
        </p>
        <div
          v-if="notice !== null"
          class="shares-notice"
          role="status"
          aria-live="polite"
        >
          <svg
            viewBox="0 0 24 24"
            aria-hidden="true"
          ><path d="m5 12 4 4L19 6" /></svg>
          <span>{{ notice }}</span>
        </div>

        <p
          v-if="shares === null && loadError === null"
          class="shares-loading"
        >
          正在读取…
        </p>
        <div
          v-else-if="shares !== null && shares.length === 0"
          class="shares-empty"
        >
          <div
            class="shares-empty-glyph"
            aria-hidden="true"
          >
            <svg viewBox="0 0 24 24"><path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1" /></svg>
          </div>
          <h2>暂无分享</h2>
          <p>在会话中多选消息，即可分享</p>
          <RouterLink
            :to="conversationPath"
            class="button primary shares-start"
          >
            去会话
            <svg
              viewBox="0 0 24 24"
              aria-hidden="true"
            ><path d="M4 12h16m-6-6 6 6-6 6" /></svg>
          </RouterLink>
        </div>

        <ul
          v-else-if="shares !== null"
          class="shares-list"
        >
          <li
            v-for="share in shares"
            :key="share.token"
            class="share-card"
            :data-tier="shareTier(share, now)"
          >
            <span
              class="share-card-glyph"
              aria-hidden="true"
            >
              <svg
                viewBox="0 0 24 24"
                width="20"
                height="20"
                fill="none"
                stroke="currentColor"
                stroke-width="1.8"
                stroke-linecap="round"
                stroke-linejoin="round"
              >
                <path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1" />
                <path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1" />
              </svg>
            </span>
            <div class="share-card-body">
              <div class="share-card-title">
                <strong data-feedback-mask="share-title">{{ share.title }}</strong>
                <span
                  class="shares-status"
                  :class="[`is-${shareTier(share, now)}`, { 'is-forever': share.expires_at === null }]"
                >{{ share.state === 'expired' ? '已过期' : shareStatusWords(share, now) }}</span>
              </div>
              <div class="share-card-meta">
                <span>{{ share.message_count }} 条 · {{ share.attachment_count === 0 ? '纯文字' : `${share.attachment_count} 个附件` }}</span>
              </div>
            </div>
            <details
              class="share-card-details"
              @toggle="onPopoverToggle"
            >
              <summary>
                <Eye aria-hidden="true" />
                打开 {{ share.views }} 次
                <ChevronDown
                  class="share-card-chevron"
                  aria-hidden="true"
                />
              </summary>
              <div class="share-card-details-panel">
                <p>{{ shareContentWords(share) }}</p>
                <p>生成于 {{ shareMoment(share.created_at) }}</p>
                <p>{{ shareViewsWords(share) }}</p>
                <p>{{ shareStatusWords(share, now) }}</p>
              </div>
            </details>
            <div class="share-card-actions">
              <button
                v-if="share.state !== 'expired'"
                data-feedback-action="复制分享链接"
                type="button"
                class="share-card-action is-copy"
                @click="copyLink(share)"
              >
                复制链接
              </button>
              <details
                class="share-card-more"
                @toggle="onPopoverToggle"
              >
                <summary aria-label="更多操作">
                  <EllipsisVertical aria-hidden="true" />
                </summary>
                <div class="share-card-menu">
                  <button
                    v-if="share.expires_at !== null || share.state === 'expired'"
                    data-feedback-action="延期分享"
                    type="button"
                    class="share-card-action"
                    :disabled="busyToken === share.token"
                    @click="extend(share)"
                  >
                    {{ share.state === "expired" ? "重新开 7 天" : "延期" }}
                  </button>
                  <button
                    data-feedback-action="删除分享"
                    type="button"
                    class="share-card-action is-danger"
                    :disabled="busyToken === share.token"
                    @click="openDelete(share, $event)"
                  >
                    删掉
                  </button>
                </div>
              </details>
            </div>
          </li>
        </ul>
      </template>

      <div
        v-if="pendingDelete !== null"
        class="shares-confirm-scrim"
        @click.self="closeDelete"
      >
        <section
          ref="deleteDialog"
          class="shares-confirm"
          role="dialog"
          aria-modal="true"
          aria-labelledby="share-delete-title"
          aria-describedby="share-delete-facts"
          :aria-busy="busyToken !== null"
          @keydown="onDeleteKeydown"
        >
          <div
            class="shares-delete-glyph"
            aria-hidden="true"
          >
            <svg viewBox="0 0 24 24"><path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7M14 10v7" /></svg>
          </div>
          <h2 id="share-delete-title">
            删除这条分享？
          </h2>
          <p
            class="shares-delete-subject"
            data-feedback-mask="share-title"
          >
            {{ pendingDelete.title }}
          </p>
          <ul
            id="share-delete-facts"
            class="shares-confirm-list"
          >
            <li>
              <svg
                viewBox="0 0 24 24"
                aria-hidden="true"
              ><path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1" /></svg>链接立即失效
            </li>
            <li>
              <svg
                viewBox="0 0 24 24"
                aria-hidden="true"
              ><path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15" /></svg>分享副本及附件一并删除
            </li>
            <li class="is-retained">
              <svg
                viewBox="0 0 24 24"
                aria-hidden="true"
              ><path d="m5 12 4 4L19 6" /></svg>归档中的原消息保留
            </li>
          </ul>
          <p
            v-if="deleteError !== null"
            class="shares-error"
            role="alert"
          >
            {{ deleteError }}
          </p>
          <div class="shares-confirm-actions">
            <button
              ref="cancelButton"
              data-share-cancel
              type="button"
              class="button quiet"
              :disabled="busyToken !== null"
              @click="closeDelete"
            >
              取消
            </button>
            <button
              data-feedback-action="确认删除分享"
              type="button"
              class="button danger"
              :disabled="busyToken !== null"
              @click="confirmDelete"
            >
              {{ busyToken !== null ? '删除中…' : deleteError === null ? '删除分享' : '重试删除' }}
            </button>
          </div>
        </section>
      </div>
    </section>
  </AppShell>
</template>

<style scoped>
.shares-page { max-width: 960px; margin: 0 auto; }
.shares-page svg { width: 22px; height: 22px; fill: none; stroke: currentColor; stroke-width: 1.7; stroke-linecap: round; stroke-linejoin: round; flex: none; }
.shares-head { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 8px 16px; }
.shares-heading { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.shares-heading .page-title { margin: 0; }
.shares-help, .share-card-details, .share-card-more { position: relative; }
.share-card:has(.share-card-details[open], .share-card-more[open]) { position: relative; z-index: 2; }
.shares-head:has(.shares-help[open]) { position: relative; z-index: 3; }
.shares-page summary { list-style: none; cursor: pointer; }
.shares-page summary::-webkit-details-marker { display: none; }
.shares-help summary, .share-card-more summary { width: 44px; height: 44px; display: grid; place-items: center; color: var(--ink-faint); border-radius: 8px; }
.shares-help summary:hover, .share-card-more summary:hover { background: var(--surface-sunken); }
.shares-help-panel, .share-card-details-panel, .share-card-menu { position: absolute; z-index: 5; padding: 14px; border: 1px solid var(--rule); border-radius: 10px; background: var(--surface-pop); box-shadow: var(--shadow-pop); font-size: 13px; }
.shares-help-panel { right: 0; top: 44px; width: min(290px, calc(100vw - 40px)); }
.shares-help-panel p, .share-card-details-panel p { margin: 0 0 8px; }
.shares-help-panel p:last-child, .share-card-details-panel p:last-child { margin-bottom: 0; }
.shares-stats { display: flex; gap: 16px; margin: 0; color: var(--ink-faint); font-size: 13px; }
.shares-stats div { display: flex; align-items: baseline; gap: 5px; }
.shares-stats div:first-child { flex-direction: row-reverse; }
.shares-stats dd { margin: 0; font-weight: 600; color: var(--ink); }
.shares-list { display: grid; gap: 12px; margin: 20px 0 0; padding: 0; list-style: none; }
.share-card { display: grid; grid-template-columns: 40px minmax(0, 1fr) auto; gap: 12px; align-items: center; padding: 16px; border: 1px solid var(--rule-soft); border-radius: 14px; background: var(--surface-pop); }
.share-card[data-tier="expired"] .share-card-glyph { background: var(--surface-sunken); color: var(--ink-faint); }
.share-card-glyph { display: grid; width: 40px; height: 40px; border-radius: 10px; background: var(--safe-bg); color: var(--accent-solid); place-items: center; }
.share-card-title { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.share-card-title strong { min-width: 0; overflow: hidden; font-size: 16px; text-overflow: ellipsis; white-space: nowrap; }
.share-card-meta { margin-top: 3px; color: var(--ink-faint); font-size: 12px; }
.shares-status { flex: none; display: inline-flex; align-items: center; gap: 5px; padding: 2px 9px; border-radius: var(--radius-pill); background: var(--safe-bg); color: var(--accent-solid); font-size: 12px; white-space: nowrap; }
.shares-status.is-soon { background: var(--warn-bg); color: var(--warn); }
.shares-status.is-forever, .shares-status.is-expired { background: var(--surface-sunken); color: var(--ink-soft); }
.share-card-details { grid-column: 2; color: var(--ink-soft); font-size: 12px; }
.share-card-details summary { display: flex; align-items: center; gap: 6px; min-height: 44px; width: fit-content; }
.share-card-details summary svg { width: 16px; height: 16px; }
.share-card-details[open] .share-card-chevron { transform: rotate(180deg); }
.share-card-details-panel { left: 0; top: 48px; width: min(240px, calc(100vw - 64px)); }
.share-card-actions { grid-column: 3; grid-row: 1 / 3; display: flex; gap: 6px; align-items: center; }
.share-card-action, .shares-link-button { padding: 9px 13px; min-height: 40px; border: 1px solid var(--rule); border-radius: var(--radius-btn); background: var(--surface); color: var(--ink); font: inherit; font-size: 13px; white-space: nowrap; cursor: pointer; }
.share-card-action.is-copy { border-color: transparent; background: var(--safe-bg); color: var(--accent-solid); font-weight: 600; }
.share-card-menu { right: 0; top: 46px; min-width: 148px; padding: 5px; }
.share-card-menu .share-card-action { display: block; width: 100%; border: none; background: transparent; text-align: left; min-height: 44px; }
.share-card-menu .share-card-action:hover { background: var(--surface-sunken); }
.share-card-action.is-danger { color: var(--danger); }
.share-card-action:disabled { cursor: progress; opacity: 0.5; }
.shares-empty { display: flex; min-height: 370px; flex-direction: column; align-items: center; justify-content: center; text-align: center; padding: 40px 0; }
.shares-empty-glyph { display: grid; place-items: center; width: 88px; height: 88px; margin-bottom: 23px; border-radius: 25px; background: var(--safe-bg); color: var(--accent-solid); }
.shares-empty-glyph svg { width: 42px; height: 42px; }
.shares-empty h2 { margin: 0 0 7px; font-size: 19px; font-weight: 600; }
.shares-empty p { margin: 0 0 22px; color: var(--ink-faint); font-size: 13px; }
.shares-start { display: inline-flex; align-items: center; gap: 10px; min-height: 44px; padding: 10px 21px; text-decoration: none; }
.shares-start svg { width: 17px; height: 17px; }
.shares-loading { color: var(--ink-soft); margin-top: 24px; }
.shares-error { margin: 12px 0 0; padding: 8px 12px; border-radius: var(--radius-btn); font-size: 13px; border: 1px solid var(--warn-border); background: var(--warn-bg); color: var(--warn); }
.shares-notice { position: fixed; z-index: 160; left: 50%; bottom: max(32px, calc(env(safe-area-inset-bottom) + 20px)); transform: translateX(-50%); display: flex; align-items: center; gap: 8px; max-width: calc(100vw - 40px); margin: 0; padding: 12px 19px; border-radius: 11px; background: var(--ink); color: var(--surface-pop); box-shadow: var(--shadow-pop); font-size: 14px; pointer-events: none; }
.shares-notice svg { width: 18px; height: 18px; color: var(--accent); }
.shares-confirm-scrim { position: fixed; inset: 0; z-index: 150; display: flex; align-items: center; justify-content: center; background: var(--scrim); padding: 20px; }
.shares-confirm { width: min(100%, 460px); max-height: calc(100dvh - 40px); overflow-y: auto; background: var(--surface-pop); padding: 24px; border-radius: 18px; box-shadow: var(--shadow-pop); }
.shares-delete-glyph { display: grid; place-items: center; width: 45px; height: 45px; border-radius: 13px; background: var(--danger-bg); color: var(--danger); margin-bottom: 15px; }
.shares-confirm h2 { font-size: 21px; margin: 0 0 5px; }
.shares-delete-subject { margin: 0 0 20px; color: var(--ink-soft); font-size: 14px; overflow-wrap: anywhere; }
.shares-confirm-list { display: grid; gap: 11px; margin: 0 0 24px; padding: 0; list-style: none; font-size: 13px; }
.shares-confirm-list li { display: flex; align-items: center; gap: 9px; }
.shares-confirm-list svg { width: 17px; height: 17px; color: var(--ink-faint); }
.shares-confirm-list .is-retained svg { color: var(--accent-solid); }
.shares-confirm-actions { display: flex; gap: 10px; margin-top: 18px; }
.shares-confirm-actions button { flex: 1; min-height: 46px; }
.shares-confirm-actions .danger { background: var(--danger); color: var(--surface-pop); border-color: var(--danger); }
@media (width <= 720px) {
  .shares-heading { width: 100%; }
  .shares-heading .page-title { font-size: 28px; }
  .shares-stats { width: 100%; }
  .share-card { grid-template-columns: 40px minmax(0, 1fr); }
  .share-card-details { grid-column: 1 / 3; grid-row: 2; border-top: 1px solid var(--rule-soft); padding-top: 8px; }
  .share-card-actions { grid-column: 2; grid-row: 2; justify-self: end; padding-top: 8px; }
  .shares-notice { bottom: calc(100px + env(safe-area-inset-bottom)); }
  .shares-confirm-scrim { align-items: flex-end; padding: 0; }
  .shares-confirm { width: 100%; max-width: 460px; max-height: calc(100dvh - 20px); padding-bottom: max(24px, env(safe-area-inset-bottom)); border-radius: 20px 20px 0 0; }
}
</style>
