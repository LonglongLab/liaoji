<script setup lang="ts">
import MiddleFilename from "@/components/MiddleFilename.vue";
import { computed } from "vue";

import type { AttachmentSummary, MessageSummary } from "@/api/archiveApi";
import EmoticonText from "@/components/EmoticonText.vue";
import FileIcon from "@/components/FileIcon.vue";
import ForwardMediaThumb from "@/components/ForwardMediaThumb.vue";
import ForwardSticker from "@/components/ForwardSticker.vue";
import ForwardAvatar from "@/components/ForwardAvatar.vue";
import {
  mergedForwardItemSummary,
  mergedForwardKindLabel,
  mergedForwardRecordTitle,
  mergedForwardSenderLine,
  safeForwardAvatar,
} from "@/lib/mergedForward";
import { formatBytes } from "@/utils/format";
import { formatDateTimeSeparator } from "@/utils/time";

type AppCard = NonNullable<MessageSummary["app_card"]>;
type AppCardItem = AppCard["items"][number];

const props = withDefaults(
  defineProps<{
    card: AppCard;
    // The owning message's attachments, so a media item can find the bytes the
    // Rec-cache backfill resolved for it. Empty (and load undefined) on any end
    // that holds the record but not the bytes -- the item then renders the same
    // honest placeholder it did before.
    attachments?: AttachmentSummary[];
    load?: ((attachmentId: string) => Promise<Blob>) | undefined;
    loadSticker?: ((attachmentId: string) => Promise<Blob>) | undefined;
    resolveAttachment?: ((attachmentId: string) => Promise<AttachmentSummary>) | undefined;
    stickerFetchAvailable?: boolean;
  }>(),
  { attachments: () => [], load: undefined, loadSticker: undefined, resolveAttachment: undefined, stickerFetchAvailable: false },
);

const emit = defineEmits<{
  "open-media": [attachmentId: string];
  "open-file": [attachmentId: string];
  // A collapsed nested record was clicked: the viewer drills into it, WeChat
  // style, instead of this list expanding it inline.
  "open-record": [card: AppCard, title: string];
  "enable-sticker-fetch": [];
}>();

const recordTitle = (item: AppCardItem): string =>
  mergedForwardRecordTitle(item) ?? "聊天记录";

const attachmentsById = computed(
  () => new Map(props.attachments.map((attachment) => [attachment.id, attachment])),
);

interface DisplayItem {
  item: AppCardItem;
  key: string;
  // The resolved attachment for a media item, only when its bytes are here.
  media: AttachmentSummary | null;
  attachment: AttachmentSummary | null;
  fileName: string;
  showAvatar: boolean;
  avatarUrl: string | null;
}

const displayItems = computed<DisplayItem[]>(() =>
  props.card.items.map((item, index) => {
    const attachment =
      item.attachment_id === null
        ? null
        : attachmentsById.value.get(item.attachment_id) ?? null;
    return {
      item,
      key: `${String(index)}:${item.kind}:${String(item.created_at ?? "none")}`,
      media: attachment !== null && attachment.content_available ? attachment : null,
      attachment,
      fileName: attachment?.file_name?.trim() || item.file_name?.trim() || item.text?.trim() || "[文件]",
      avatarUrl: safeForwardAvatar(item.avatar_url),
      showAvatar: index === 0 || item.sender === null || item.sender !== props.card.items[index - 1]?.sender ||
        item.source_desc !== props.card.items[index - 1]?.source_desc ||
        item.avatar_url !== props.card.items[index - 1]?.avatar_url,
    };
  }),
);
</script>

<template>
  <ol class="merged-forward-items">
    <li
      v-for="entry in displayItems"
      :key="entry.key"
      class="merged-forward-item"
      :class="`is-${entry.item.kind}`"
    >
      <div
        class="merged-forward-avatar-slot"
        data-feedback-mask="avatar"
      >
        <template v-if="entry.showAvatar">
          <ForwardAvatar
            :url="entry.avatarUrl"
            :local-ref="entry.item.avatar_ref ?? null"
            :blob-id="entry.item.avatar_blob_id ?? null"
            :sender="entry.item.sender"
          />
        </template>
      </div>
      <div class="merged-forward-item-body">
        <div
          v-if="entry.item.sender !== null || entry.item.created_at !== null"
          class="merged-forward-item-meta"
        >
          <strong
            v-if="entry.item.sender !== null"
            data-feedback-mask="sender-name"
          >{{ mergedForwardSenderLine(entry.item) }}</strong>
          <time
            v-if="entry.item.created_at !== null"
            :datetime="new Date(entry.item.created_at * 1000).toISOString()"
          >
            {{ formatDateTimeSeparator(entry.item.created_at) }}
          </time>
        </div>

        <p
          v-if="entry.item.kind === 'text'"
          data-feedback-mask="message-body"
          class="merged-forward-item-text"
        >
          <EmoticonText :text="entry.item.text?.trim() || '[文本内容为空]'" />
        </p>
        <!-- A nested record collapses to a card, the way WeChat draws it: real
           title plus a few preview lines. Clicking drills into a new level of
           the viewer; nothing expands inline. -->
        <button
          v-else-if="entry.item.kind === 'record' && entry.item.nested !== null"
          data-feedback-mask="message-body"
          class="merged-forward-nested"
          type="button"
          :aria-label="`打开嵌套聊天记录：${recordTitle(entry.item)}`"
          @click="emit('open-record', entry.item.nested, recordTitle(entry.item))"
        >
          <strong class="merged-forward-title">{{ recordTitle(entry.item) }}</strong>
          <ol class="merged-forward-preview">
            <li
              v-for="(previewItem, previewIndex) in entry.item.nested.items.slice(0, 3)"
              :key="`${String(previewIndex)}:${previewItem.kind}`"
            >
              <EmoticonText :text="mergedForwardItemSummary(previewItem)" />
            </li>
          </ol>
          <small
            v-if="entry.item.nested.truncated"
            class="merged-forward-truncated"
          >
            部分内容未完整保存
          </small>
          <span class="merged-forward-footer">
            <span>聊天记录</span>
            <svg
              width="16"
              height="16"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="1.8"
              stroke-linecap="round"
              stroke-linejoin="round"
              aria-hidden="true"
            >
              <path d="m9 18 6-6-6-6" />
            </svg>
          </span>
        </button>
        <div
          v-else-if="entry.item.kind === 'record'"
          class="merged-forward-nested is-empty"
        >
          <p class="merged-forward-placeholder">
            嵌套聊天记录未完整保存
          </p>
        </div>
        <!-- An image whose bytes the backfill resolved: a real thumbnail that
           opens the shared MediaViewer, exactly like a bubble image. -->
        <button
          v-else-if="entry.item.kind === 'image' && load !== undefined && entry.media !== null"
          class="merged-forward-media-button"
          type="button"
          aria-label="打开图片"
          @click="emit('open-media', entry.media.id)"
        >
          <ForwardMediaThumb
            :load="load"
            :attachment-id="entry.media.id"
            alt="图片"
          />
          <span
            v-if="entry.media.variant === 'thumbnail'"
            class="merged-forward-variant"
          >仅缩略图</span>
        </button>
        <!-- A video whose bytes are here: no cached still to show inline, so a
           play tile that opens the MediaViewer, which plays it. -->
        <button
          v-else-if="entry.item.kind === 'video' && load !== undefined && entry.media !== null"
          class="merged-forward-media-button is-video"
          type="button"
          aria-label="播放视频"
          @click="emit('open-media', entry.media.id)"
        >
          <span
            class="merged-forward-play"
            aria-hidden="true"
          >▶</span>
          <span>视频</span>
        </button>
        <!-- A file whose bytes are here: opens the shared FilePreviewViewer. -->
        <button
          v-else-if="entry.item.kind === 'file' && load !== undefined && entry.media !== null"
          class="merged-forward-file is-openable"
          type="button"
          :aria-label="`预览文件：${entry.fileName}`"
          @click="emit('open-file', entry.media.id)"
        >
          <FileIcon :file-name="entry.fileName" />
          <span
            data-feedback-mask="file-name"
            class="merged-forward-file-name"
          >
            <MiddleFilename :name="entry.fileName" />
          </span>
          <span class="merged-forward-file-meta">
            <span v-if="entry.item.file_size !== null">{{ formatBytes(entry.item.file_size) }}</span>
            <span class="merged-forward-file-note">点击预览</span>
          </span>
        </button>
        <div
          v-else-if="entry.item.kind === 'file'"
          class="merged-forward-file"
        >
          <FileIcon :file-name="entry.fileName" />
          <span
            data-feedback-mask="file-name"
            class="merged-forward-file-name"
          >
            <MiddleFilename :name="entry.fileName" />
          </span>
          <span class="merged-forward-file-meta">
            <span v-if="entry.item.file_size !== null">{{ formatBytes(entry.item.file_size) }}</span>
            <!-- The archive holds the record's description of the file, not the
               file: WeChat's own card is a download entry point and this one
               cannot be. Saying so beats a dead click target. -->
            <span class="merged-forward-file-note">仅归档了文件信息</span>
          </span>
        </div>
        <div
          v-else-if="entry.item.kind === 'sticker' && entry.attachment !== null && (loadSticker !== undefined || load !== undefined)"
        >
          <ForwardSticker
            :attachment="entry.attachment"
            :load="(loadSticker ?? load)!"
            :resolve="resolveAttachment"
            :fetch-available="stickerFetchAvailable"
            @open="emit('open-media', $event)"
            @enable-sticker-fetch="emit('enable-sticker-fetch')"
          />
        </div>
        <div
          v-else-if="
            entry.item.kind === 'image'
              || entry.item.kind === 'video'
              || entry.item.kind === 'voice'
              || entry.item.kind === 'sticker'
          "
          class="merged-forward-placeholder"
        >
          <span class="merged-forward-kind">{{ mergedForwardKindLabel(entry.item.kind) }}</span>
          <span>{{ mergedForwardKindLabel(entry.item.kind) }}内容未保存媒体引用</span>
          <small v-if="entry.item.file_size !== null">{{ formatBytes(entry.item.file_size) }}</small>
        </div>
        <div
          v-else-if="entry.item.kind === 'link'"
          class="merged-forward-placeholder"
        >
          <span class="merged-forward-kind">{{ mergedForwardKindLabel(entry.item.kind) }}</span>
          <span data-feedback-mask="message-body"><EmoticonText :text="entry.item.text?.trim() || `[${mergedForwardKindLabel(entry.item.kind)}]`" /></span>
        </div>
        <div
          v-else
          class="merged-forward-placeholder is-unknown"
        >
          <span class="merged-forward-kind">未知</span>
          <span data-feedback-mask="message-body"><EmoticonText :text="entry.item.text?.trim() || '未知条目'" /></span>
        </div>
        <div
          v-if="entry.item.reply?.subtitle"
          class="merged-forward-reply"
          data-feedback-mask="message-body"
        >
          <EmoticonText :text="entry.item.reply.subtitle" />
        </div>
      </div>
    </li>
  </ol>
</template>
