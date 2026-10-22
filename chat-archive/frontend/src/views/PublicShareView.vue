<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { useRoute } from "vue-router";

import { ArchiveApiError, type AttachmentSummary, type PublicShare } from "@/api/archiveApi";
import { useArchiveApi } from "@/api/injection";
import MediaViewer from "@/components/MediaViewer.vue";
import MessageTypeRenderer from "@/components/MessageTypeRenderer.vue";
import { downloadBlob } from "@/lib/filePreview/download";
import {
  browserFetch,
  createShareLoader,
  dateRangeWords,
  markNoIndex,
  shareExpiryFooter,
  toRenderMessages,
} from "@/lib/share/publicShare";
import { buildTranscriptRows } from "@/lib/transcript";
import { displayableImage } from "@/lib/wxgf/toBlob";

/**
 * 对方打开分享链接看到的页面（PSU-2.183）。
 *
 * 远端上**唯一不需要任何身份**的一页：不经过登录守卫、不挂应用外壳（没有侧栏、
 * 顶栏、账号、反馈、同步状态），不设 cookie、不写本地存储。气泡、时间、引用与
 * 卡片照 App 画（`MessageTypeRenderer`）；图片能放大、语音能播、视频能看、文件能下载。
 *
 * 失效只说一句「已失效」，不说是过期还是被删，也不透露原来是什么会话——
 * 后端对四种情况回的本来就是同一个响应。
 */
const route = useRoute();
const api = useArchiveApi();

const token = computed(() => String(route.params["token"] ?? ""));
const share = ref<PublicShare | null>(null);
const state = ref<"loading" | "ready" | "gone" | "unavailable">("loading");

const messages = computed(() => (share.value === null ? [] : toRenderMessages(share.value)));
const rows = computed(() => buildTranscriptRows(messages.value));
const range = computed(() => dateRangeWords(messages.value));

let loadAttachment: (attachmentId: string) => Promise<Blob> = () =>
  Promise.reject(new Error("分享还没读到"));

// 读到之后要重新取一次才拿得到新地址（5 分钟就过期）。重取也会被记成一次打开——
// 这是「只记打开次数」换来的代价，比为此多开一个只给地址的接口简单。
const fetchShare = (): Promise<PublicShare> => api.getPublicShare(token.value);

const load = async (): Promise<void> => {
  try {
    const counted = globalThis.document.documentElement.dataset.sharePageToken === token.value;
    if (counted) delete globalThis.document.documentElement.dataset.sharePageToken;
    const loaded = counted ? await api.getPublicShare(token.value, { countView: false }) : await fetchShare();
    const fetchBytes = createShareLoader(loaded.attachments, fetchShare, browserFetch);
    // 分享带的是存着的那份字节（PSU-2.185）：wxgf 在这一页解——静态的解成 PNG，
    // 动图标成 `image/x-wxgf` 交给表情组件逐帧播。与会话页的加载器出口同一个函数。
    loadAttachment = (attachmentId) => fetchBytes(attachmentId).then(displayableImage);
    share.value = loaded;
    state.value = "ready";
  } catch (error: unknown) {
    state.value =
      error instanceof ArchiveApiError && error.code === "SHARE_GONE" ? "gone" : "unavailable";
  }
};

/** 与 `MediaViewer` 的 `MediaViewerItem` 同形（从 .vue 导入的类型 lint 解不开）。 */
interface MediaViewerItem {
  attachment: AttachmentSummary;
  senderName: string;
  createdAt: number;
}

const viewerOpen = ref(false);
const viewerIndex = ref(0);
const mediaItems = computed<MediaViewerItem[]>(() =>
  messages.value.flatMap((message) =>
    message.attachments
      .filter(
        (attachment) =>
          attachment.content_available &&
          (attachment.kind === "image" || attachment.kind === "video"),
      )
      .map((attachment) => ({
        attachment,
        senderName: message.sender_display_name ?? "",
        createdAt: message.created_at,
      })),
  ),
);

const openMedia = (attachmentId: string): void => {
  const index = mediaItems.value.findIndex((item) => item.attachment.id === attachmentId);
  if (index < 0) return;
  viewerIndex.value = index;
  viewerOpen.value = true;
};

const downloadError = ref<string | null>(null);
const openFile = async (attachmentId: string): Promise<void> => {
  const attachment = messages.value
    .flatMap((message) => message.attachments)
    .find((item) => item.id === attachmentId);
  if (attachment === undefined || !attachment.content_available) return;
  downloadError.value = null;
  try {
    downloadBlob(await loadAttachment(attachmentId), attachment.file_name ?? "文件");
  } catch {
    downloadError.value = "这个文件现在下载不了，请稍后再试。";
  }
};

const loader = (attachmentId: string): Promise<Blob> => loadAttachment(attachmentId);

let unmark: (() => void) | null = null;
onMounted(() => {
  unmark = markNoIndex();
  void load();
});
onBeforeUnmount(() => {
  unmark?.();
});
</script>

<template>
  <main
    class="public-share"
    :data-state="state"
    tabindex="0"
    aria-label="分享的聊天记录"
  >
    <p
      v-if="state === 'loading'"
      class="public-share-status"
      role="status"
    >
      正在打开…
    </p>

    <section
      v-else-if="state === 'gone'"
      class="public-share-gone"
    >
      <h1>这个分享已失效</h1>
      <p>链接已过期，或者分享的人已经把它删了。</p>
    </section>

    <section
      v-else-if="state === 'unavailable'"
      class="public-share-gone"
    >
      <h1>暂时打不开</h1>
      <p>服务器现在没有回应，请稍后再试。</p>
    </section>

    <template v-else-if="share !== null">
      <header class="public-share-head">
        <h1>{{ share.title }}</h1>
        <p>{{ range }} · 共 {{ share.message_count }} 条</p>
        <a
          class="public-share-agent-link"
          :href="`/s/${encodeURIComponent(token)}.md`"
          rel="nofollow"
        >AI 读取</a>
      </header>

      <p
        v-if="downloadError !== null"
        class="public-share-error"
        role="alert"
      >
        {{ downloadError }}
      </p>

      <div class="public-share-stream transcript-window-channel">
        <div
          v-for="row in rows"
          :key="row.message.id"
          class="transcript-window-row"
        >
          <div
            v-if="row.dateLabel !== null"
            class="date-chip"
          >
            {{ row.dateLabel }}
          </div>
          <MessageTypeRenderer
            :message="row.message"
            :reply-preview="null"
            :highlighted="false"
            :show-sender-name="true"
            :load-attachment="loader"
            :load-thumbnail="undefined"
            :resolve-attachment="undefined"
            @open-media="openMedia"
            @open-file="openFile"
          />
        </div>
      </div>

      <footer class="public-share-foot">
        <span>这是一份分享的聊天记录</span> · <span>共 {{ share.message_count }} 条</span> ·
        <span>{{ shareExpiryFooter(share.expires_at) }}</span> · <span>由「聊记」生成</span>
      </footer>

      <MediaViewer
        :open="viewerOpen"
        :items="mediaItems"
        :initial-index="viewerIndex"
        :load-attachment="loader"
        @close="viewerOpen = false"
      />
    </template>
  </main>
</template>

<style scoped>
.public-share {
  box-sizing: border-box;
  width: 100%;
  height: 100%;
  min-height: 0;
  overflow-y: auto;
  scrollbar-gutter: stable;
  padding: 24px max(16px, calc((100% - 728px) / 2));
  padding-bottom: max(24px, env(safe-area-inset-bottom));
  background: var(--canvas, var(--surface));
}

.public-share-head h1 {
  margin: 0;
  font-size: 19px;
  overflow-wrap: anywhere;
}

.public-share-head p {
  margin: 4px 0 16px;
  color: var(--ink-soft);
  font-size: 13px;
}

.public-share-agent-link {
  display: inline-flex;
  align-items: center;
  min-height: 44px;
  margin-bottom: 8px;
  color: var(--accent, #137a3d);
  text-underline-offset: 3px;
}

.public-share-agent-link:focus-visible {
  outline: 2px solid currentColor;
  outline-offset: 3px;
}

.public-share-stream {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 2px;
}

.public-share-stream > .transcript-window-row {
  min-width: 0;
}

.public-share-foot {
  margin-top: 24px;
  padding: 16px 0 0;
  border-top: 1px solid var(--rule-soft);
  background: var(--surface);
  color: var(--ink-soft);
  font-size: 12.5px;
  text-align: center;
  text-wrap: balance;
}

/* 断行只落在「·」之间：「共 3 条」不能拆成两行。 */
.public-share-foot span {
  white-space: nowrap;
}

.public-share-status,
.public-share-gone {
  margin-top: 30vh;
  text-align: center;
}

.public-share-gone h1 {
  margin: 0 0 8px;
  font-size: 20px;
}

.public-share-gone p {
  margin: 0;
  color: var(--ink-soft);
}

.public-share-error {
  margin: 0 0 12px;
  padding: 8px 12px;
  border: 1px solid var(--warn-border);
  border-radius: var(--radius-btn);
  background: var(--warn-bg);
  color: var(--warn);
  font-size: 13px;
}
</style>
