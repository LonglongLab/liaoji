import { computed, nextTick, ref, watch, type ComputedRef, type Ref } from "vue";

import { buildTranscriptRows, type RenderMessage } from "@/lib/transcript";

import {
  useMessageAnchor,
  type AnchorWindow,
  type JumpOutcome,
} from "./useMessageAnchor";
import { useTranscriptInitialPosition } from "./useTranscriptInitialPosition";
import type { WorkspaceRouteLayer } from "./useWorkspaceRoute";

/**
 * 转录的定位与落点，两端一份（PSU-2.87 · W4）。
 *
 * 两端挂的是同一个 `MessageTranscript`、同一种分页、同一段窗口行为
 * （`useMessageAnchor` / `useTranscriptInitialPosition` 早已共享），可**什么时候
 * 定位、定位失败说什么、`layer=record` 那一层怎么取卡片**仍是两份。这一片把那
 * 三件收成一份。
 *
 * ## 端口
 *
 * - `scope` / `fetchWindow`：取窗口的端点不同（本机 `/messages/window`，远端
 *   `/remote/…/messages/window`，记录模型 v9 起），窗口这件事一个字不差。
 *   注入的是「取」，不是「端」。
 * - `onJumpFailed`：**能力差异**。够不到 v9 的远端答
 *   `REMOTE_RECORD_MODEL_TOO_OLD`，那一端要退回「打开会话、命中翻进来才高亮」
 *   并说出来；本机没有这一档（窗口端点一直都在）。见
 *   `docs/local-remote-boundary.md` §5 的两种「不可用」。
 * - `transcriptRef` 由这一片持有并交回：滚动出口只有一个，单测里可以给替身。
 */
export interface WorkspaceTranscriptHandle {
  scrollToIndex: (index: number, align?: "center" | "end") => void;
  scrollToMessage: (messageId: string) => Promise<void>;
  captureReadingPosition: () => void;
}

export interface WorkspaceTranscriptOptions {
  routeLayer: Ref<WorkspaceRouteLayer>;
  routeMessageId: Ref<string | null>;
  /** 这一端已加载的消息（远端的已经过 `remoteMessageToRender`）。 */
  messages: ComputedRef<RenderMessage[]>;
  /** 这一次定位属于哪一段；`null` = 还没准备好（账号/归档/会话未定）。 */
  scope: () => string | null;
  fetchWindow: (
    anchorMessageId: string,
    request: { before: number; after: number },
  ) => Promise<AnchorWindow>;
  /** 还在取第一页吗。 */
  loading: () => boolean;
  /** 还有更早的页吗。 */
  hasMore: () => boolean;
  /** 定位失败时这一端有没有自己的说法；返回 `true` = 已处理，不再报错。 */
  onJumpFailed?: (outcome: { message: string; code: string | null }) => boolean;
}

export interface WorkspaceTranscript {
  transcriptRef: Ref<WorkspaceTranscriptHandle | null>;
  anchor: ReturnType<typeof useMessageAnchor>;
  rows: ComputedRef<ReturnType<typeof buildTranscriptRows>>;
  messagesById: ComputedRef<Map<string, RenderMessage>>;
  jumpError: Ref<string | null>;
  loadOlderWindow: () => Promise<void>;
  loadNewerWindow: () => Promise<void>;
  /** 定位到一条消息：已加载就滚过去，没加载就取一段窗口。 */
  jumpTo: (messageId: string) => Promise<void>;
  /** 打开会话停在最新一条那一段；端专属编排偶尔要作废它。 */
  initialPosition: ReturnType<typeof useTranscriptInitialPosition>;
  /** `layer=record` 那一层要画的卡片；不是合并转发就不开层。 */
  recordCard: ComputedRef<RenderMessage["app_card"]>;
  /** 记录消息自己的附件，好让转发详情画得出它的媒体。 */
  recordAttachments: ComputedRef<RenderMessage["attachments"]>;
}

export const useWorkspaceTranscript = (
  options: WorkspaceTranscriptOptions,
): WorkspaceTranscript => {
  const transcriptRef = ref<WorkspaceTranscriptHandle | null>(null);
  const jumpError = ref<string | null>(null);

  const rows = computed(() => buildTranscriptRows(options.messages.value));

  const anchor = useMessageAnchor(
    { scope: options.scope, fetch: options.fetchWindow },
    options.messages,
  );

  /**
   * 定位窗口里的记录与转录里的是同一种形状，只是窗口那一份来自另一个端点。
   *
   * 窗口那一批也进表：`layer=record` 的深链、以及窗口内的引用预览，靠的都是
   * 「这条消息认得出来」。
   */
  const messagesById = computed(() => {
    const map = new Map<string, RenderMessage>();
    for (const message of options.messages.value) map.set(message.id, message);
    const windowItems = anchor.windowItems.value;
    if (windowItems !== null) {
      for (const message of windowItems) map.set(message.id, message);
    }
    return map;
  });

  // 打开会话停在最新一条。微信式默认：不恢复「上次读到哪」。
  const initialPosition = useTranscriptInitialPosition({
    loading: options.loading,
    rowCount: () => rows.value.length,
    scrollToIndex: (index) => {
      transcriptRef.value?.scrollToIndex(index, "end");
    },
  });

  /**
   * 定位一条命中就该让它站在**视野中间**（裁定 D6）。
   *
   * 对齐写出来而不是靠组件的默认值：这一片是两端唯一一份定位，落点是它答应的
   * 事，不该由被调用方的默认参数替它答应。
   */
  const jumpTo = async (messageId: string): Promise<void> => {
    jumpError.value = null;
    const outcome: JumpOutcome = await anchor.jumpTo(messageId);
    if (outcome.kind === "loaded") {
      transcriptRef.value?.scrollToIndex(outcome.index, "center");
      return;
    }
    if (outcome.kind === "window") {
      await transcriptRef.value?.scrollToMessage(messageId);
      return;
    }
    if (options.onJumpFailed?.({ message: outcome.message, code: outcome.code }) === true) {
      return;
    }
    jumpError.value = outcome.message;
  };

  const loadOlderWindow = async (): Promise<void> => {
    const error = await anchor.loadOlderWindow();
    if (error !== null) jumpError.value = error;
  };

  const loadNewerWindow = async (): Promise<void> => {
    const error = await anchor.loadNewerWindow();
    if (error !== null) jumpError.value = error;
  };

  /** 路由指名了一条消息，就不该再往最新那头跳。 */
  const positionedByRoute = computed(
    () =>
      options.routeMessageId.value !== null &&
      (options.routeLayer.value === "context" || options.routeLayer.value === "record"),
  );

  // 换了作用域（账号 / 归档 / 会话），窗口就作废：它属于读者刚才在看的那一段。
  watch(
    () => options.scope(),
    (scope) => {
      anchor.exitWindow();
      jumpError.value = null;
      initialPosition.reset(scope !== null && !positionedByRoute.value);
    },
    { immediate: true },
  );

  watch([options.messages, options.loading, options.hasMore], () => {
    initialPosition.schedule();
  });

  /**
   * 地址栏指名了一条消息就去定位它。
   *
   * 作用域也在被监听的那几个里：它是问端点要窗口的前提，而在远端它来自另一个
   * 查询。少了它，第一拍（归档标识还没回来）会把这次定位记成「已经做过」，而
   * 标识回来时会话与命中都没变，watch 不会再叫一次——定位就这么无声地丢了。
   */
  let positionedRouteAnchor = "";
  watch(
    [() => options.scope(), options.routeLayer, options.routeMessageId],
    async ([scope, layer, messageId]) => {
      // `record` 与 `context` 走同一条：深链指向的那一条常常不在已加载的页里
      // ——它是别人发来的一条链接。捞得回来却不捞只是少做（裁定 D7）。
      if ((layer !== "context" && layer !== "record") || scope === null || messageId === null) {
        positionedRouteAnchor = "";
        anchor.exitWindow();
        return;
      }
      const key = `${scope}:${layer}:${messageId}`;
      if (key === positionedRouteAnchor) return;
      positionedRouteAnchor = key;
      initialPosition.reset(false);
      await nextTick();
      if (layer === "context") {
        await jumpTo(messageId);
        return;
      }
      // `record`：卡片已经在手上就不必再问端点。
      if (isMergedForward(messagesById.value.get(messageId))) return;
      const outcome = await anchor.jumpTo(messageId);
      if (outcome.kind === "error") {
        if (
          options.onJumpFailed?.({ message: outcome.message, code: outcome.code }) === true
        ) {
          return;
        }
        jumpError.value = outcome.message;
        return;
      }
      if (!isMergedForward(messagesById.value.get(messageId))) {
        jumpError.value = "这条消息没有可查看的合并转发详情。";
      }
    },
    { immediate: true },
  );

  /**
   * 这一层是**合并转发详情**层，所以只认合并转发的卡片。
   *
   * 取不到就不开层：弹一个空壳比不弹更糟。
   */
  const recordCard = computed<RenderMessage["app_card"]>(() => {
    if (options.routeLayer.value !== "record" || options.routeMessageId.value === null) {
      return null;
    }
    const card = messagesById.value.get(options.routeMessageId.value)?.app_card ?? null;
    return card?.kind === "merged_forward" ? card : null;
  });

  const recordAttachments = computed<RenderMessage["attachments"]>(() => {
    if (options.routeLayer.value !== "record" || options.routeMessageId.value === null) {
      return [];
    }
    return messagesById.value.get(options.routeMessageId.value)?.attachments ?? [];
  });

  return {
    transcriptRef,
    anchor,
    rows,
    messagesById,
    jumpError,
    loadOlderWindow,
    loadNewerWindow,
    jumpTo,
    initialPosition,
    recordCard,
    recordAttachments,
  };
};

const isMergedForward = (message: RenderMessage | undefined): boolean =>
  message?.app_card?.kind === "merged_forward";
