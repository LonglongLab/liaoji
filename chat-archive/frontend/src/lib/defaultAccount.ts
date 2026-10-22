/**
 * 「打开时显示哪个微信」（PSU-2.119）。
 *
 * 在这之前两端各自有一套：本机记得住上次（localStorage），远程连上次都不记得
 * （选择只活在 Pinia 里），而两边的兜底都是「清单第一个」——那个第一个按
 * `acc_` + sha256 前缀排，是谁纯属哈希偶然，加个号或发生一次归并它还会变。
 *
 * 所以这里放两样东西，两端共用同一份：
 *
 * - `resolveOpeningAccount`：**纯函数**，回答「这一次打开该落在谁身上、为什么」。
 *   为什么要一起答：面板底部那行要如实说出现在生效的是哪条规则，而「诚实展示」
 *   的前提是这个理由不是界面自己猜的。
 * - `localAccountChoices` / `remoteAccountChoices`：存取，失败一律静默——
 *   一个存储开关不该把工作区挡在门外。
 *
 * 两端的差异只有一处，在类型里就说完了：本机档案没有归并维度（账号就是数据
 * 目录），`mergedIds` 给空数组即可。
 */

/** 解析时需要知道的关于一个账号的全部。 */
export interface AccountIdentity {
  readonly id: string;
  /**
   * 这条上传流并掉的其它 id（PSU-2.86/2.90）。同一个微信在两台机器上曾经算
   * 两个账号，归并之后老 id 只活在这里。
   */
  readonly mergedIds: readonly string[];
}

/** 这一次落在这个账号上，是被谁决定的。 */
export type OpeningReason =
  | "preferred"
  | "session"
  | "pinned"
  | "healed"
  | "last"
  | "hint"
  | "first"
  | "none";

export interface OpeningChoice {
  readonly id: string | null;
  readonly reason: OpeningReason;
  /**
   * 钉住的默认解析成了另一个 id（跟着归并走）时，这里是要写回存储的新值。
   * 写回**不在这里做**：解析是纯函数，副作用留给调用方的 watch。
   */
  readonly heal: string | null;
  /**
   * 钉过默认，但那个 id 在这份清单里既不在、也找不到继承者。
   *
   * **只在清单非空时才可能为真**：清单为空是「还没答 / 旧远端 / 空归档」，
   * 那时说「你的默认不在了」是在替一份还没到的数据下结论。
   */
  readonly pinnedLost: boolean;
}

export interface OpeningInputs {
  readonly accounts: readonly AccountIdentity[];
  /** 链接里的 `?account=`。一条发过来的链接必须在收的人那边打开同一个账号。 */
  readonly preferred?: string | null;
  /** 这次打开里手动切过的那个。 */
  readonly session?: string | null;
  /** 钉住的默认。 */
  readonly pinned?: string | null;
  /** 上次看过的那个。 */
  readonly last?: string | null;
  /**
   * 服务端提名的那个：本机 `bootstrap.active_account_id`。远程端没有这个字段，
   * 不传即可——**差异声明在这里，不是漏在某一端的代码里**。
   */
  readonly serverHint?: string | null;
}

/**
 * 一个存下来的 id，换成它今天的身份（PSU-2.90 的 `heirOf`，搬到这里给两端用）。
 *
 * 认不得就是 null——「被并掉的」与「根本不存在的」必须是两种结果，否则自愈会
 * 把真问题一起盖住：链接里打错一个字和链接指向一条已归并的流，不该同样地
 * 把人丢到清单第一个账号上。
 */
export const accountHeir = (
  accounts: readonly AccountIdentity[],
  candidate: string | null,
): string | null => {
  if (candidate === null) return null;
  if (accounts.some((item) => item.id === candidate)) return candidate;
  return accounts.find((item) => item.mergedIds.includes(candidate))?.id ?? null;
};

export const resolveOpeningAccount = ({
  accounts,
  preferred = null,
  session = null,
  pinned = null,
  last = null,
  serverHint = null,
}: OpeningInputs): OpeningChoice => {
  const known = (candidate: string | null): boolean =>
    candidate !== null && accounts.some((item) => item.id === candidate);
  const heirOf = (candidate: string | null): string | null =>
    accountHeir(accounts, candidate);

  const pinnedHeir = heirOf(pinned);
  const pinnedLost = accounts.length > 0 && pinned !== null && pinnedHeir === null;
  const answer = (id: string | null, reason: OpeningReason): OpeningChoice => ({
    id,
    reason,
    heal: null,
    pinnedLost,
  });

  const preferredHeir = heirOf(preferred);
  if (preferredHeir !== null) return answer(preferredHeir, "preferred");
  const sessionHeir = heirOf(session);
  if (sessionHeir !== null) return answer(sessionHeir, "session");
  if (pinnedHeir !== null) {
    return pinnedHeir === pinned
      ? answer(pinnedHeir, "pinned")
      : { id: pinnedHeir, reason: "healed", heal: pinnedHeir, pinnedLost: false };
  }
  const lastHeir = heirOf(last);
  if (lastHeir !== null) return answer(lastHeir, "last");
  if (known(serverHint)) return answer(serverHint, "hint");
  const first = accounts[0]?.id ?? null;
  return answer(first, first === null ? "none" : "first");
};

/** 一端的两把键。`null` = 还不知道是哪份归档，读空、写不落地。 */
export interface AccountChoices {
  readPinned(): string | null;
  writePinned(accountId: string | null): void;
  readLast(): string | null;
  writeLast(accountId: string | null): void;
}

// 本机端的「上次」沿用原来那把键：换了名字等于让每个老用户重新选一次账号。
const LOCAL_PINNED_KEY = "chat-archive:default-account-id";
const LOCAL_LAST_KEY = "chat-archive:selected-account-id";
const REMOTE_PINNED_PREFIX = "chat-archive:remote-default-account-id:";
const REMOTE_LAST_PREFIX = "chat-archive:remote-selected-account-id:";

const read = (key: string | null): string | null => {
  if (key === null) return null;
  try {
    const value = globalThis.localStorage.getItem(key)?.trim();
    return value ? value : null;
  } catch {
    return null;
  }
};

const write = (key: string | null, value: string | null): void => {
  if (key === null) return;
  try {
    if (value === null) globalThis.localStorage.removeItem(key);
    else globalThis.localStorage.setItem(key, value);
  } catch {
    // 浏览器可能整个禁掉存储。本次运行的内存选择仍然有效，不该因此报错。
  }
};

const choices = (pinnedKey: string | null, lastKey: string | null): AccountChoices => ({
  readPinned: () => read(pinnedKey),
  writePinned: (accountId) => write(pinnedKey, accountId),
  readLast: () => read(lastKey),
  writeLast: (accountId) => write(lastKey, accountId),
});

export const localAccountChoices: AccountChoices = choices(
  LOCAL_PINNED_KEY,
  LOCAL_LAST_KEY,
);

/**
 * 远程端按归档分键：一个浏览器可以先后连上两份归档，它们的账号 id 不通用。
 * 归档标识要等 status 回来才有，在那之前写下去只能写进一把猜的键里。
 */
export const remoteAccountChoices = (archiveId: string | null): AccountChoices =>
  archiveId === null || archiveId === ""
    ? choices(null, null)
    : choices(REMOTE_PINNED_PREFIX + archiveId, REMOTE_LAST_PREFIX + archiveId);
