import { useQueryClient } from "@tanstack/vue-query";
import { watch, type Ref } from "vue";

import type { SearchHit } from "@/api/archiveApi";

/**
 * 检索命中落在**列表还没翻到的会话**上时，把那条会话的名字问回来（PSU-2.85）。
 *
 * ## 为什么这件事需要一份共享实现
 *
 * 真机归档 614 个会话、列表一页 100 个，而**输入框同时驱动会话列表那份查询**——
 * `q=` 筛的是会话名不是正文，搜一个只在正文里出现的词，它答 0 条。于是靠「翻到过的页」
 * 取名的做法在这里注定失败：约 84% 的命中会话印不透明标识，点进去之后标题却是对的，
 * 同一条会话两处两个说法。
 *
 * 这个坑四个调用点各踩一次、各修一次，修出了四份不一样的实现（PSU-2.84 自己就是
 * 新添的第三份）。所以它在这里只写一遍。
 *
 * ## `scope` 是这个设计的关键
 *
 * 调用方传的是**它自己那条会话详情 query 键的前缀**：
 *
 * - 本机 `["conversation", accountId]` → 键与 `useConversationDetailQuery` 完全相同；
 * - 远程 `["remote", archiveId, "conversation", accountId]` → 同理。
 *
 * 于是两端白拿三件事：**同键去重**（命中列表是个 computed，它依赖逐渐填满的头像表，
 * 搜完那一秒内会重算好几次；没有去重就是把同一批请求重发好几遍，跨境链路上单条约
 * 一秒）、**与「点进去」共享缓存**（补名字顺带预热了打开那一次，反过来也成立）、
 * 以及**换账号时被 `AppShell` 既有的「键里含旧账号 id」清理顺带带走**。
 *
 * `scope` 为 `null` 表示这一端还没定下来（归档标识未到 / 账号未定），此时不发请求，
 * 也不会拿 `null` 去污染缓存键。
 *
 * ## 问不到就不编
 *
 * `resolve` 抛错，或答出来的名字是空的（远端 `display_name` 可以是 `null`），一律记成
 * **问不到**：不写 `names`，界面照实印不透明标识。这一档是四个调用点共同的既有行为，
 * 收敛时一个字没改。
 */
export interface HydratedConversationName {
  id: string;
  name: string;
}

export interface ConversationNameHydrationOptions {
  /** 已知的名字表。调用方持有它，并负责在换账号时清空。 */
  names: Ref<Record<string, string>>;
  /** 这一页的命中。缺名判据只看 `conversation_id`。 */
  hits: Ref<readonly SearchHit[]>;
  /** 详情 query 键的前缀；`null` = 这一端还没定下来。 */
  scope: Ref<readonly unknown[] | null>;
  /** 问一条会话叫什么。`scope` 与缓存键里的那一份**是同一个**。 */
  resolve: (
    conversationId: string,
    scope: readonly unknown[],
  ) => Promise<HydratedConversationName | null>;
}

/**
 * 把一批名字并进表里，**没有真的变化就把原来那份原样交回**。
 *
 * 引用不变这一点是有用途的：`names` 是个 ref，每次写入都会推着下游 computed 重算；
 * 而列表每翻一页都会把同一批名字再送一遍。三个调用点（两个独立搜索页与本 composable）
 * 从前各写了一遍这段合并，其中两处因为在闭包里改 `let changed` 而被类型收窄判成恒假。
 */
export const rememberConversationNames = (
  current: Record<string, string>,
  entries: readonly (HydratedConversationName | null)[],
): Record<string, string> => {
  const next = { ...current };
  let changed = false;
  for (const entry of entries) {
    if (entry === null || entry.id === "" || entry.name === "") continue;
    if (next[entry.id] === entry.name) continue;
    next[entry.id] = entry.name;
    changed = true;
  }
  return changed ? next : current;
};

export const useConversationNameHydration = (
  options: ConversationNameHydrationOptions,
): void => {
  const queryClient = useQueryClient();
  // 代次只用来**丢弃过期作用域的在途答案**（去重归 `ensureQueryData` 管）：换账号时
  // 调用方会把名字表清空，旧账号的在途答案落地就会把别人的名字挂到这条会话上。
  let generation = 0;

  const hydrate = async (
    scope: readonly unknown[],
    hits: readonly SearchHit[],
    current: number,
  ): Promise<void> => {
    const missing = [
      ...new Set(
        hits
          .map((hit) => hit.conversation_id)
          .filter((id) => id !== "" && options.names.value[id] === undefined),
      ),
    ];
    if (missing.length === 0) return;
    const resolved = await Promise.all(
      missing.map(async (conversationId) => {
        try {
          return await queryClient.ensureQueryData({
            queryKey: [...scope, conversationId],
            queryFn: (): Promise<HydratedConversationName | null> =>
              options.resolve(conversationId, scope),
            retry: false,
          });
        } catch {
          return null;
        }
      }),
    );
    if (current !== generation) return;
    // 没有新名字时交回的是**同一个引用**，赋值因此是 no-op，不会白推一轮下游 computed。
    options.names.value = rememberConversationNames(options.names.value, resolved);
  };

  // 依赖表里**没有** `names`：学到名字不该自我触发。缺名集合只会缩小，所以这条是收敛的。
  watch(
    [options.scope, options.hits],
    ([scope, hits]) => {
      const current = ++generation;
      if (scope === null) return;
      void hydrate(scope, hits, current);
    },
    { immediate: true },
  );
};
