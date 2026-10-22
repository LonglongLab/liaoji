<script setup lang="ts">
import { computed, ref, toRef, watch } from "vue";

import AvatarBadge from "@/components/AvatarBadge.vue";
import WechatCleanupSection from "@/components/WechatCleanupSection.vue";
import { useModalFocus } from "@/composables/useModalFocus";
import type { CapabilityState } from "@/api/archiveApi";
import type { RenderConversationDetail, RenderMember } from "@/lib/transcript";
import { formatRelativeTime } from "@/utils/time";

/**
 * 两端共用这一个抽屉（PSU-2.47 起）。远端答得出的比本机少三样，而「少」是
 * 类型的一部分：`media_count` / `member_count` / `is_group` 都可以是 null，
 * 意思是「这一端没有这个数」，不是零、也不是「不是群」。补一个貌似合理的值
 * 就等于替远端说了一句它没说过的话，而这块界面正是那句话唯一的出口。
 */
const props = withDefaults(
  defineProps<{
    conversation: RenderConversationDetail | null;
    members: RenderMember[];
    membersLoading: boolean;
    membersFetchingMore: boolean;
    membersHaveMore: boolean;
    open: boolean;
    /**
     * 名单**问不到**时说的那句话（不是「名单是空的」）。
     * 有值时名单区整个让位给它——列一个空名单等于断言这个群没有别人。
     */
    membersUnavailableNote?: string | null;
    /** 这一端能不能导出会话／能不能在会话里检索。远端两样都还没有。 */
    exportAvailable?: boolean;
    searchAvailable?: boolean;
    /**
     * 「在微信中清理」（PSU-2.189）：视图按能力清单给出这一端的状态。不传＝这一端没有
     * 这件事（远程网页），整块不出现；抽屉自己不判「是不是远程」。
     */
    wechatCleanup?: { accountId: string; state: CapabilityState } | null;
  }>(),
  {
    membersUnavailableNote: null,
    exportAvailable: true,
    searchAvailable: true,
    wechatCleanup: null,
  },
);

const emit = defineEmits<{ close: []; export: []; search: []; "load-more-members": [] }>();

const { container, initial, onKeydown } = useModalFocus(
  toRef(props, "open"),
  () => emit("close"),
);
void container;
void initial;

const memberQuery = ref("");
const membersExpanded = ref(false);
const MEMBER_PREVIEW_COUNT = 10;
const filteredMembers = computed(() => {
  const query = memberQuery.value.trim().toLocaleLowerCase("zh-CN");
  if (query === "") return props.members;
  return props.members.filter((member) =>
    [member.display_name, member.group_alias ?? ""]
      .join(" ")
      .toLocaleLowerCase("zh-CN")
      .includes(query),
  );
});
const visibleMembers = computed(() =>
  membersExpanded.value ? filteredMembers.value : props.members.slice(0, MEMBER_PREVIEW_COUNT),
);
const canExpandMembers = computed(() =>
  props.members.length > 0 || props.membersHaveMore,
);

watch(
  () => props.conversation?.id,
  () => {
    memberQuery.value = "";
    membersExpanded.value = false;
  },
);

const clearMemberQuery = (): void => {
  memberQuery.value = "";
};

/**
 * 名单是空的时候说的那句话。
 *
 * 「档案里没有」与「还没传上来」在这里是两句话：`member_count === null` 说明
 * 这条会话的通讯录还没发布过，那时候说「档案中没有可用成员」就是把一次没做完
 * 的上传说成了一个事实。
 */
const emptyMemberNote = computed(() =>
  props.conversation?.member_count === null
    ? "群成员名单尚未从本机上传"
    : "档案中没有可用成员",
);
</script>

<template>
  <aside
    v-if="open"
    ref="container"
    class="info-drawer"
    role="dialog"
    aria-modal="true"
    aria-label="会话信息"
    @keydown="onKeydown"
  >
    <header class="info-drawer-head">
      <button
        ref="initial"
        class="info-drawer-back"
        type="button"
        aria-label="返回会话"
        @click="emit('close')"
      >
        ←
      </button>
      <strong>会话信息</strong>
      <span aria-hidden="true" />
    </header>
    <template v-if="conversation !== null">
      <div class="info-drawer-body">
        <div class="info-identity">
          <AvatarBadge
            :name="conversation.display_name"
            :src="conversation.avatar_ref ?? conversation.contact?.avatar_ref"
            :alt="`${conversation.display_name}的头像`"
            size="lg"
          />
          <h2
            data-feedback-mask="conversation-name"
            class="info-name"
          >
            {{ conversation.display_name }}
          </h2>
        </div>
        <section
          v-if="conversation.is_group === false && conversation.contact !== null"
          class="info-section"
        >
          <strong>联系人</strong>
          <dl class="info-contact">
            <div v-if="conversation.contact.remark !== null">
              <dt>备注</dt>
              <dd data-feedback-mask="contact-name">
                {{ conversation.contact.remark }}
              </dd>
            </div>
            <div v-if="conversation.contact.signature !== null">
              <dt>签名</dt>
              <dd data-feedback-mask="contact-name">
                {{ conversation.contact.signature }}
              </dd>
            </div>
            <div v-if="conversation.contact.source_description !== null">
              <dt>来源</dt>
              <dd data-feedback-mask="contact-name">
                {{ conversation.contact.source_description }}
              </dd>
            </div>
          </dl>
        </section>

        <section
          v-if="conversation.is_group === true"
          class="info-section member-section"
        >
          <div class="member-section-head">
            <strong>群成员</strong>
            <small v-if="conversation.member_count === null">{{ filteredMembers.length }}</small>
            <small v-else>{{ filteredMembers.length }} / {{ conversation.member_count }}</small>
          </div>
          <form
            v-if="membersUnavailableNote === null && membersExpanded"
            class="member-search"
            @submit.prevent
          >
            <input
              v-model="memberQuery"
              type="search"
              aria-label="搜索群成员"
              placeholder="搜索成员或群昵称"
            >
            <button
              v-if="memberQuery !== ''"
              class="text-action"
              type="button"
              @click="clearMemberQuery"
            >
              清除
            </button>
          </form>
          <p
            v-if="membersUnavailableNote !== null"
            class="info-note member-unavailable-note"
            role="note"
          >
            {{ membersUnavailableNote }}
          </p>
          <p
            v-else-if="membersLoading"
            class="info-note"
          >
            正在加载成员……
          </p>
          <ul
            v-else
            class="member-list"
            :class="{ 'is-expanded': membersExpanded }"
            :tabindex="membersExpanded ? 0 : undefined"
            aria-label="群成员名单"
          >
            <li
              v-for="member in visibleMembers"
              :key="member.id"
            >
              <AvatarBadge
                :name="member.display_name"
                :src="member.avatar_ref"
                :alt="`${member.display_name}的头像`"
                size="sm"
              />
              <span data-feedback-mask="contact-name">{{ member.display_name }}</span>
              <small
                v-if="member.group_alias !== null"
                data-feedback-mask="contact-name"
              >{{ member.group_alias }}</small>
              <small v-else-if="member.is_self">我</small>
            </li>
          </ul>
          <p
            v-if="membersUnavailableNote === null && !membersLoading && filteredMembers.length === 0"
            class="info-note"
          >
            {{ memberQuery === "" ? emptyMemberNote : "没有匹配的已加载成员" }}
          </p>
          <p
            v-if="memberQuery !== '' && membersHaveMore"
            class="info-note member-search-note"
          >
            仅筛选已加载成员；继续加载后可扩大匹配范围。
          </p>
          <button
            v-if="membersUnavailableNote === null && canExpandMembers"
            class="text-action member-expand"
            type="button"
            :aria-expanded="membersExpanded"
            @click="membersExpanded = !membersExpanded; clearMemberQuery()"
          >
            {{ membersExpanded ? "收起成员" : "查看全部群成员" }}
          </button>
          <button
            v-if="membersUnavailableNote === null && membersExpanded && membersHaveMore"
            class="text-action"
            type="button"
            :disabled="membersFetchingMore"
            @click="emit('load-more-members')"
          >
            {{ membersFetchingMore ? "正在加载" : "加载更多成员" }}
          </button>
        </section>

        <dl class="info-fields">
          <div>
            <dt>类型</dt>
            <dd v-if="conversation.is_group === null">
              档案未记录
            </dd>
            <dd v-else>
              {{ conversation.is_group ? "群聊" : "单聊" }}
            </dd>
          </div>
          <div>
            <dt>消息数</dt>
            <dd>{{ conversation.message_count.toLocaleString("zh-CN") }} 条</dd>
          </div>
          <div v-if="conversation.media_count !== null">
            <dt>媒体数</dt>
            <dd>{{ conversation.media_count.toLocaleString("zh-CN") }} 个</dd>
          </div>
          <div>
            <dt>最近消息</dt>
            <dd>{{ formatRelativeTime(conversation.last_message_at) }}</dd>
          </div>
          <div v-if="conversation.is_group === true">
            <dt>成员</dt>
            <dd v-if="conversation.member_count === null">
              名单尚未上传
            </dd>
            <dd v-else>
              {{ conversation.member_count.toLocaleString("zh-CN") }} 人
            </dd>
          </div>
          <div v-if="conversation.is_group && conversation.my_group_alias !== null">
            <dt>我的群昵称</dt>
            <dd data-feedback-mask="contact-name">
              {{ conversation.my_group_alias }}
            </dd>
          </div>
        </dl>

        <section
          v-if="conversation.is_group === true"
          class="info-section"
        >
          <strong>群公告</strong>
          <p data-feedback-mask="message-body">
            {{ conversation.announcement ?? "档案中没有可用公告" }}
          </p>
        </section>
      </div>
      <footer
        v-if="searchAvailable || exportAvailable || (wechatCleanup !== null && wechatCleanup.state !== 'unsupported')"
        class="info-drawer-actions"
        aria-label="会话操作"
      >
        <div
          v-if="searchAvailable || exportAvailable"
          class="info-actions-main"
        >
          <button
            v-if="searchAvailable"
            class="button secondary info-search"
            type="button"
            @click="emit('search')"
          >
            查找聊天记录 ›
          </button>
          <button
            v-if="exportAvailable"
            data-feedback-action="导出这个会话"
            class="button secondary info-export"
            type="button"
            @click="emit('export')"
          >
            导出这个会话
          </button>
        </div>
        <WechatCleanupSection
          v-if="wechatCleanup !== null && wechatCleanup.state !== 'unsupported'"
          :conversation-id="conversation.id"
          :account-id="wechatCleanup.accountId"
          :state="wechatCleanup.state"
        />
      </footer>
    </template>
    <p
      v-else
      class="info-note"
    >
      正在加载会话信息……
    </p>
  </aside>
</template>
