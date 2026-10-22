/**
 * 配对码在两台机器之间怎么走（PSU-2.68）。
 *
 * 码是 `<32 位 id>.<64 位十六进制 secret>`（共 97 字符），手打不现实，
 * 而两台 PC 之间没有共享剪贴板。
 * 于是签发那一台把「地址 + 码」编成一条链接放进二维码，兑换那一台的粘贴框
 * 同时认得裸码和这条链接——粘进去就少填一格，也少一次抄错的机会。
 *
 * 用自定义 scheme 而不是 https：这串东西**不该被任何人打开**，它只是一个
 * 容器。写成 https 链接，浏览器和聊天软件都会热心地去访问它，把一次性的
 * 配对码送到某个日志里。
 */

export const PAIRING_LINK_SCHEME = "chat-archive://pair";

export interface PairingInput {
  /** 链接里带了地址就是它；粘的是裸码则为 null，表单里那一格保持用户自己填的。 */
  baseUrl: string | null;
  pairingCode: string;
}

export const pairingLink = (baseUrl: string, pairingCode: string): string =>
  `${PAIRING_LINK_SCHEME}?base=${encodeURIComponent(baseUrl)}` +
  `&code=${encodeURIComponent(pairingCode)}`;

/**
 * 把用户粘进来的东西解成「地址（可能没有）+ 码」。
 *
 * 不做校验：这里只负责拆，合不合法由后端说了算——本机多一套判定标准，只会
 * 在两边不一致时把一个能用的码拦下来。
 */
export const parsePairingInput = (pasted: string): PairingInput => {
  const trimmed = pasted.trim();
  if (!trimmed.startsWith(`${PAIRING_LINK_SCHEME}?`)) {
    return { baseUrl: null, pairingCode: trimmed };
  }
  const query = new URLSearchParams(trimmed.slice(`${PAIRING_LINK_SCHEME}?`.length));
  const code = query.get("code");
  if (code === null || code === "") {
    // 形状像链接但没有码：当作裸码原样交回去，让后端给出它自己的说法，
    // 而不是在这里悄悄清空用户刚粘进来的东西。
    return { baseUrl: null, pairingCode: trimmed };
  }
  const base = query.get("base");
  return { baseUrl: base === null || base === "" ? null : base, pairingCode: code };
};

/** 距离到期还有多少秒；已过期为 0。`expiresAt` 是服务端给的 Unix 秒。 */
export const remainingSeconds = (expiresAt: number, nowMs: number): number =>
  Math.max(0, Math.floor(expiresAt - nowMs / 1000));

/** `m:ss`。到期后由调用方换成一句话，不是显示 `0:00` 继续躺着。 */
export const formatCountdown = (seconds: number): string => {
  const safe = Math.max(0, Math.floor(seconds));
  const minutes = Math.floor(safe / 60);
  return `${String(minutes)}:${String(safe % 60).padStart(2, "0")}`;
};
