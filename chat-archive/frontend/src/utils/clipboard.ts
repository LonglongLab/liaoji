/**
 * 复制到剪贴板，失败就说失败（PSU-2.68）。
 *
 * 放在 .ts 里而不是组件内联：`.vue` 上开着 ESLint 的 `no-undef` 且没有配
 * browser globals，浏览器 API 只能待在模块里。
 *
 * 不先判断 `navigator.clipboard` 在不在——按 TS 的类型它永远在，而运行时在
 * 非安全上下文里它就是不在。所以直接调用并接住：不存在会抛 TypeError，
 * 没授权会 reject，两种都是同一个答案「没复制成功」。
 *
 * 返回 false 而不是静默当作成功：一个骗人的「已复制」会让用户去另一台机器上
 * 粘一个空剪贴板，然后回来怀疑是配对码不对。
 */
export const copyText = async (text: string): Promise<boolean> => {
  try {
    await globalThis.navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
};
