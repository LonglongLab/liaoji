/**
 * 切换本机账号——顶部账号切换器和「看自动同步的进度」共用这一条路。
 *
 * 光改 `selectedSourceId` 不够：旧账号的查询缓存、会话与导出的界面状态都得
 * 跟着清掉，否则新账号的页面上会闪出旧账号的数据。这段从前只写在 AppShell 里，
 * 「看进度」要切账号时再抄一份就是两份，所以收到这里。
 */
import { useQueryClient } from "@tanstack/vue-query";
import { useRouter } from "vue-router";

import type { AutoSyncStatus } from "@/api/archiveApi";
import { useArchiveApi } from "@/api/injection";
import { useConversationUiStore } from "@/stores/conversationUi";
import { useExportBuilderStore } from "@/stores/exportBuilder";
import { useOnboardingStore } from "@/stores/onboarding";

type AutoSyncCycle = NonNullable<AutoSyncStatus["current_cycle"]>;

export const useLocalAccountSwitch = (): ((accountId: string) => Promise<boolean>) => {
  const api = useArchiveApi();
  const queryClient = useQueryClient();
  const onboarding = useOnboardingStore();
  const conversationUi = useConversationUiStore();
  const exportBuilder = useExportBuilderStore();

  return async (accountId: string): Promise<boolean> => {
    const previousAccountId = onboarding.selectedSourceId;
    const switched = await onboarding.switchAccount(api, accountId);
    if (!switched) return false;
    if (previousAccountId !== null && previousAccountId !== accountId) {
      const belongsToPreviousAccount = (queryKey: readonly unknown[]): boolean =>
        queryKey.includes(previousAccountId);
      await queryClient.cancelQueries({
        predicate: (query) => belongsToPreviousAccount(query.queryKey),
      });
      queryClient.removeQueries({
        predicate: (query) => belongsToPreviousAccount(query.queryKey),
      });
      conversationUi.resetForAccount();
      exportBuilder.resetForAccount();
    }
    return true;
  };
};

/**
 * 「看自动同步这一轮的进度」——设置页的「看进度」和同步健康页那一行共用。
 *
 * 一轮挨个跑每个账号，而同步健康页与开始页只看选中的那个：先切到正在跑的
 * 账号，否则推的是另一个号时，跳过去只有一张没有进度的状态卡。切不过去（账号
 * 列表过期）照样跳：失败的说法在账号切换器里，停在原地就是「点了没反应」。
 */
export const useOpenAutoSyncCycle = (): ((cycle: AutoSyncCycle) => Promise<void>) => {
  const switchLocalAccount = useLocalAccountSwitch();
  const onboarding = useOnboardingStore();
  const router = useRouter();
  const api = useArchiveApi();

  return async (cycle: AutoSyncCycle): Promise<void> => {
    await switchLocalAccount(cycle.account_id);
    if (cycle.sync_job_id !== null) {
      await router.push("/sync-health");
      return;
    }
    // 开始页的进度面板认的是 currentJob，所以先把它换成**这一轮**的导入。
    // 取不到、或者点的这一刻它已经跑完（下一段就是推送），就去同步健康页：
    // 进开始页只会看见一张退不出来的「已完成」卡。
    const importJobId = cycle.import_job_id;
    const opened =
      importJobId === null
        ? onboarding.reopenImportProgress()
        : await onboarding.openImportJob(api, importJobId).catch(() => false);
    await router.push(opened ? "/start" : "/sync-health");
  };
};
