<script setup lang="ts">
import { computed, watch } from "vue";

import type { SearchHit, SearchIndexStatus } from "@/api/archiveApi";
import AvatarBadge from "@/components/AvatarBadge.vue";
import EmoticonText from "@/components/EmoticonText.vue";
import { useVisibleIds } from "@/composables/useVisibleIds";
import { hitSummary } from "@/lib/conversationSearchTabs";
import type { RenderConversation } from "@/lib/transcript";
import { formatConversationListTime, formatRelativeTime } from "@/utils/time";

const props = withDefaults(defineProps<{
  items: RenderConversation[];
  selectedId: string | null;
  filterText: string;
  loading: boolean;
  fetchingMore: boolean;
  hasMore: boolean;
  errorText: string | null;
  searchHits: SearchHit[];
  searchLoading: boolean;
  searchFetchingMore: boolean;
  searchHasMore: boolean;
  searchTotalCount: number;
  searchConversationCounts: Record<string, number>;
  searchErrorText: string | null;
  searchIndexStatus: SearchIndexStatus;
  conversationNames: Record<string, string>;
  // 历史消息是否仍在导入:空态要区分"档案里没有数据"与"历史尚未导到"。
  importActive?: boolean;
  // 这一端有没有服务端搜索。false 时输入框仍过滤已加载的会话名（那是纯本地
  // 行为），但不再画聊天记录检索区——没有端点的功能不摆入口。缺省 true，
  // 且必须写在 withDefaults 里：Vue 把缺省的 boolean prop 强转成 false。
  searchEnabled?: boolean;
  // 这一端的会话名过滤会不会匹配群成员。本机匹配 7 列（会话名 / 联系人名 /
  // 备注 / 群成员名 / 群昵称 / 成员的联系人名与备注）；远程归档没有通讯录也
  // 没有成员名单，只匹配会话名与不透明标识。false 时那一行不再说「群成员
  // 匹配」——那句话在这一端是过度声称。缺省 true，理由同上一个 prop。
  memberMatchEnabled?: boolean;
}>(), { searchEnabled: true, memberMatchEnabled: true });

const emit = defineEmits<{
  "update:filterText": [value: string];
  select: [conversationId: string];
  /**
   * 一条会话的**那一组**命中，不是其中某一条。
   *
   * 这一行说的是「共 N 条相关聊天记录」，点它该到的是那 N 条的列表——
   * 从前它直接跳到最近的那一条，另外 N-1 条没有任何一页摆得出来。
   */
  "select-search-conversation": [
    group: { conversationId: string; hit: SearchHit; count: number },
  ];
  "load-more": [];
  "load-more-search": [];
  retry: [];
  "retry-search": [];
  /**
   * 现在真的在视口里（含上下各一屏缓冲）的会话 id；`null` = 这一端没有
   * `IntersectionObserver`，按全部算（PSU-2.122）。
   *
   * 远程端据此决定取哪几张头像：一页 100 条、屏幕上 8 条，此前是 100 张全取。
   * 普通列表行与搜索态「联系人 / 群聊」行**都要**挂观察：后者漏挂时，搜索
   * 结果里只有先前缓存过的几张有图，往下全是首字。
   * 本机端的头像是 `/api/v1/avatars/{id}` 这样的稳定 URL，浏览器自己管缓存，
   * 不用听这一条——**多出来的这个事件不监听也完全正确**。
   */
  "visible-change": [conversationIds: readonly string[] | null];
  /**
   * 这一行看起来**马上要被打开**：指针按在上面，或者键盘焦点落到了它。
   *
   * 按下与松手之间、焦点落定与按下回车之间都有上百毫秒，那一段够把最新一页
   * 先要回来。只报意图，取不取、怎么取由上层定——本机那一端读的是同机
   * SQLite，**这个事件不监听也完全正确**。
   */
  "prefetch-intent": [conversationId: string];
}>();

const visibleRows = useVisibleIds();
watch(visibleRows.ids, (ids) => { emit("visible-change", ids); }, { immediate: true });

interface HighlightPart {
  text: string;
  matched: boolean;
}

interface MessageSearchGroup {
  conversationId: string;
  conversationName: string;
  hit: SearchHit;
  summary: string;
  count: number;
  nameParts: HighlightPart[];
  summaryParts: HighlightPart[];
}

/**
 * 这一行为什么被匹配上。
 *
 * 群聊那一支分两句：本机的名称过滤真的会去匹配群成员（`query.py` 那七列），
 * 远程归档没有成员名单，说「或群成员匹配」就是在替这一端声称它做不到的事。
 * `null`（不知道是不是群聊）不站边——两种猜法都可能是错的。
 */
const matchLabel = (isGroup: boolean | null): string => {
  if (isGroup === true) {
    return props.memberMatchEnabled ? "群聊或群成员匹配 · " : "群聊 · ";
  }
  return isGroup === false ? "联系人匹配 · " : "";
};

const normalizedFilter = computed(() => props.filterText.trim());
const searchActive = computed(() => normalizedFilter.value !== "");
const serverSearchEnabled = computed(() => props.searchEnabled);

/**
 * 预览缺席分两种：会话真没有归档消息，和这一端没同步预览。后者说
 * 「暂无归档消息」是对着 74 条消息撒谎，条数是仅有的诚实替代。
 */
const previewText = (item: RenderConversation): string => {
  if (item.last_message_preview !== null) return item.last_message_preview;
  if (item.message_count > 0) return `${item.message_count.toLocaleString("zh-CN")} 条消息`;
  return "暂无归档消息";
};

const splitHighlight = (text: string): HighlightPart[] => {
  const query = normalizedFilter.value;
  if (query === "") return [{ text, matched: false }];
  const index = text.toLocaleLowerCase("zh-CN").indexOf(query.toLocaleLowerCase("zh-CN"));
  if (index < 0) return [{ text, matched: false }];
  const parts: HighlightPart[] = [];
  if (index > 0) parts.push({ text: text.slice(0, index), matched: false });
  parts.push({ text: text.slice(index, index + query.length), matched: true });
  if (index + query.length < text.length) {
    parts.push({ text: text.slice(index + query.length), matched: false });
  }
  return parts;
};

const conversationMatches = computed(() =>
  props.items.map((item) => ({ item, nameParts: splitHighlight(item.display_name) })),
);

const messageGroups = computed<MessageSearchGroup[]>(() => {
  const groups = new Map<string, { hit: SearchHit; count: number }>();
  for (const hit of props.searchHits) {
    const existing = groups.get(hit.conversation_id);
    if (existing === undefined) {
      groups.set(hit.conversation_id, { hit, count: 1 });
    } else {
      existing.count += 1;
    }
  }
  return Array.from(groups, ([conversationId, group]) => {
    const conversationName = props.conversationNames[conversationId] ?? conversationId;
    const summary = hitSummary(group.hit);
    return {
      conversationId,
      conversationName,
      hit: group.hit,
      summary,
      count: props.searchConversationCounts[conversationId] ?? group.count,
      nameParts: splitHighlight(conversationName),
      summaryParts: splitHighlight(summary),
    };
  });
});

const loadedConversationIds = computed(
  () => new Set(messageGroups.value.map((group) => group.conversationId)),
);
const hiddenConversationCount = computed(() =>
  Object.keys(props.searchConversationCounts).filter(
    (conversationId) => !loadedConversationIds.value.has(conversationId),
  ).length,
);
const remainingHitCount = computed(() =>
  Math.max(props.searchTotalCount - props.searchHits.length, 0),
);
const moreButtonLabel = computed(() => {
  if (props.searchFetchingMore) return "正在加载……";
  if (hiddenConversationCount.value > 0) {
    return `加载更多聊天记录（还有 ${String(hiddenConversationCount.value)} 个会话）`;
  }
  return `加载更多聊天记录（还有 ${String(remainingHitCount.value)} 条）`;
});

const hasSearchResults = computed(
  () => conversationMatches.value.length > 0 || messageGroups.value.length > 0,
);

// 会话检索和记录检索是同一个本地服务的两次请求，服务连不上时两边一起失败。
// 分成两张告警卡片会让一次停摆看着像两处坏了，所以文案相同就并成一张。
const sharedErrorText = computed(() =>
  props.errorText !== null && props.errorText === props.searchErrorText
    ? props.errorText
    : null,
);

const retryAll = (): void => {
  emit("retry");
  emit("retry-search");
};

const onFilterInput = (event: Event): void => {
  emit("update:filterText", (event.target as HTMLInputElement).value);
};

const onScroll = (event: Event): void => {
  const el = event.target as HTMLElement;
  if (
    props.hasMore &&
    !props.fetchingMore &&
    el.scrollTop + el.clientHeight >= el.scrollHeight - 120
  ) {
    emit("load-more");
  }
};

</script>

<template>
  <aside
    class="conv-list"
    :class="{ 'is-searching': searchActive }"
    aria-label="会话列表"
  >
    <div class="conv-filter">
      <input
        :value="filterText"
        type="search"
        placeholder="搜索会话、联系人、聊天记录"
        aria-label="搜索会话、联系人、聊天记录"
        autocomplete="off"
        @input="onFilterInput"
      >
    </div>
    <div
      class="conv-scroll"
      @scroll="onScroll"
    >
      <template v-if="searchActive">
        <div
          v-if="sharedErrorText !== null"
          class="notice-warn conv-error"
          role="alert"
        >
          <strong>搜索暂时不可用</strong>
          <p>{{ sharedErrorText }}</p>
          <button
            class="text-action"
            type="button"
            @click="retryAll"
          >
            重试
          </button>
        </div>

        <div
          v-else-if="errorText !== null"
          class="notice-warn conv-error"
          role="alert"
        >
          <strong>会话名称检索失败</strong>
          <p>{{ errorText }}</p>
          <button
            class="text-action"
            type="button"
            @click="emit('retry')"
          >
            重试
          </button>
        </div>

        <section
          v-if="conversationMatches.length > 0"
          class="conv-search-group"
          aria-labelledby="conv-search-contacts-title"
        >
          <h2 id="conv-search-contacts-title">
            联系人 / 群聊
          </h2>
          <button
            v-for="match in conversationMatches"
            :key="match.item.id"
            :ref="(el) => { visibleRows.observe(el as Element | null, match.item.id); }"
            data-feedback-action="打开会话"
            type="button"
            class="conv-search-contact"
            @click="emit('select', match.item.id)"
          >
            <AvatarBadge
              :name="match.item.display_name"
              :src="match.item.avatar_ref"
              :alt="`${match.item.display_name}的头像`"
              size="md"
            />
            <span
              data-feedback-mask="conversation-name"
              class="conv-search-copy"
            >
              <strong>
                <template
                  v-for="(part, index) in match.nameParts"
                  :key="index"
                ><mark v-if="part.matched">{{ part.text }}</mark><template v-else>{{ part.text }}</template></template>
              </strong>
              <small>{{ matchLabel(match.item.is_group) }}{{ match.item.message_count.toLocaleString("zh-CN") }} 条消息</small>
            </span>
          </button>
          <p
            v-if="fetchingMore"
            class="conv-search-status"
            role="status"
          >
            正在加载更多会话……
          </p>
        </section>

        <div
          v-if="serverSearchEnabled && searchErrorText !== null && sharedErrorText === null"
          class="notice-warn conv-error"
          role="alert"
        >
          <strong>聊天记录检索失败</strong>
          <p>{{ searchErrorText }}</p>
          <button
            class="text-action"
            type="button"
            @click="emit('retry-search')"
          >
            重试
          </button>
        </div>

        <div
          v-else-if="serverSearchEnabled && searchErrorText === null && searchIndexStatus === 'unavailable'"
          class="notice-warn conv-error"
          role="status"
        >
          <strong>搜索索引尚未建立</strong>
          <p>当前只能匹配会话名称；请在下一次增量导入中启用搜索索引。</p>
        </div>

        <section
          v-if="serverSearchEnabled && messageGroups.length > 0"
          class="conv-search-group"
          aria-labelledby="conv-search-messages-title"
        >
          <h2 id="conv-search-messages-title">
            聊天记录
          </h2>
          <button
            v-for="group in messageGroups"
            :key="group.conversationId"
            data-feedback-action="打开会话"
            type="button"
            class="conv-search-message"
            @click="emit('select-search-conversation', {
              conversationId: group.conversationId,
              hit: group.hit,
              count: group.count,
            })"
          >
            <span class="conv-search-message-head">
              <strong data-feedback-mask="conversation-name">
                <template
                  v-for="(part, index) in group.nameParts"
                  :key="index"
                ><mark v-if="part.matched">{{ part.text }}</mark><template v-else>{{ part.text }}</template></template>
              </strong>
              <time>{{ formatRelativeTime(group.hit.created_at) }}</time>
            </span>
            <span
              data-feedback-mask="message-preview"
              class="conv-search-snippet"
            >
              <EmoticonText :parts="group.summaryParts" />
            </span>
            <small>
              {{ group.count.toLocaleString("zh-CN") }} 条相关聊天记录
            </small>
          </button>
          <button
            v-if="searchHasMore"
            class="conv-search-more"
            type="button"
            :disabled="searchFetchingMore"
            @click="emit('load-more-search')"
          >
            {{ moreButtonLabel }}
          </button>
        </section>

        <p
          v-if="serverSearchEnabled && searchIndexStatus === 'building'"
          class="conv-search-status"
          role="status"
        >
          搜索索引仍在构建，当前结果可能不完整。
        </p>
        <p
          v-if="serverSearchEnabled && searchLoading"
          class="conv-search-status"
          role="status"
        >
          正在搜索会话与聊天记录……
        </p>
        <p
          v-else-if="!hasSearchResults && errorText === null && searchErrorText === null && searchIndexStatus !== 'unavailable'"
          data-feedback-mask="search-query"
          class="conv-search-empty"
        >
          {{ serverSearchEnabled
            ? `未找到与「${normalizedFilter}」相关的会话或消息`
            : `未找到名称匹配「${normalizedFilter}」的会话` }}
        </p>
      </template>

      <template v-else>
        <div
          v-if="errorText !== null"
          class="notice-warn conv-error"
          role="alert"
        >
          <strong>会话列表加载失败</strong>
          <p>{{ errorText }}</p>
          <button
            class="text-action"
            type="button"
            @click="emit('retry')"
          >
            重新加载
          </button>
        </div>
        <p
          v-else-if="loading"
          class="conv-hint"
        >
          正在加载会话……
        </p>
        <p
          v-else-if="items.length === 0"
          class="conv-hint"
          data-testid="conversation-empty-hint"
        >
          {{ importActive ? "历史消息仍在导入，这里很快会出现会话。" : "档案里还没有会话。" }}
        </p>
        <button
          v-for="item in items"
          :key="item.id"
          :ref="(el) => { visibleRows.observe(el as Element | null, item.id); }"
          data-feedback-action="打开会话"
          type="button"
          class="conv-item"
          :class="{ 'is-selected': item.id === selectedId }"
          :aria-current="item.id === selectedId ? 'true' : undefined"
          @click="emit('select', item.id)"
          @pointerdown="emit('prefetch-intent', item.id)"
          @focus="emit('prefetch-intent', item.id)"
        >
          <AvatarBadge
            :name="item.display_name"
            :src="item.avatar_ref"
            :alt="`${item.display_name}的头像`"
            size="md"
          />
          <span class="conv-item-main">
            <span class="conv-item-head">
              <strong data-feedback-mask="conversation-name">{{ item.display_name }}</strong>
              <time>{{ formatConversationListTime(item.last_message_at) }}</time>
            </span>
            <small
              data-feedback-mask="message-preview"
              class="conv-item-preview"
            >
              <EmoticonText :text="previewText(item)" />
            </small>
          </span>
        </button>
        <p
          v-if="fetchingMore"
          class="conv-hint"
        >
          正在加载更多……
        </p>
      </template>
    </div>
  </aside>
</template>
