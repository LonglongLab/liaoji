/**
 * The link markup WeChat leaves inside a message body, resolved at display time.
 *
 * A system notice is a template WeChat renders, not a sentence someone typed,
 * and the template arrives with its presentation still in it:
 *
 *   你有一笔待接收的<_wc_custom_link_ href="weixin://wxpay/transfer/…">转账</_wc_custom_link_>
 *
 * WeChat's own client draws that as `你有一笔待接收的转账` with the last two
 * characters blue. Printed raw, it is three lines of `href=` where the reader
 * expects six characters. Two families appear in this repo's own archive --
 * 113 system notices (208 `_wc_custom_link_` tags, 32 `<a>`), plus 8 text and
 * quote messages from 微信团队 and service accounts that carry `<a>` the same
 * way.
 *
 * The same two rules `wechatEmoticons` holds itself to apply here:
 *
 *  1. Only these two tag names are ever unwrapped, and only when a matching
 *     close tag follows with no `<` in between. Anything else -- a lone
 *     `<`, an unclosed tag, a shape not on this list -- stays verbatim, which
 *     is also what WeChat falls back to.
 *  2. Nothing is rewritten in storage. The archived `content_text` keeps the
 *     tag and the target; this module only decides what a reader sees.
 *
 * The target is carried through so the caller can decide what is openable.
 * Most of these are not: `weixin://wxpay/transfer/remindrcvmsg?…` is an
 * in-app action no browser can perform, and pretending otherwise would put a
 * live-looking link on a page where clicking it does nothing.
 */

export interface InlineLinkSegment {
  readonly kind: "text" | "link";
  /** What the reader sees: the run of text, or the link's own label. */
  readonly value: string;
  /** The raw target, exactly as WeChat wrote it. Only set for links. */
  readonly href?: string;
}

// `\1` closes what `<(...)` opened, so `<a …>…</_wc_custom_link_>` is not a
// link. `[^<]*` refuses to cross into another tag, which keeps an unclosed
// opening tag from swallowing the rest of the notice. `\b` is what stops `<a`
// from matching `<abbr`; `_` is a word character, so `<_wc_custom_link_>` with
// no attributes still has a boundary before `>`.
const LINK = /<(_wc_custom_link_|a)\b([^>]*)>([^<]*)<\/\1\s*>/gi;
const HREF = /\bhref\s*=\s*(?:"([^"]*)"|'([^']*)')/i;

/**
 * Split message text into literal runs and the links WeChat wrote into it.
 *
 * Always returns at least one segment, so an empty body still has a shape.
 */
export function splitTextWithInlineLinks(text: string): InlineLinkSegment[] {
  const segments: InlineLinkSegment[] = [];
  let cursor = 0;

  for (const match of text.matchAll(LINK)) {
    const start = match.index;
    const before = text.slice(cursor, start);
    if (before !== "") segments.push({ kind: "text", value: before });
    // `<a href="…"></a>` labels nothing, so it contributes nothing: the
    // sentence still reads as WeChat renders it, and no invisible element is
    // left sitting in the middle of it.
    const label = match[3] ?? "";
    if (label !== "") {
      const href = HREF.exec(match[2] ?? "");
      const target = href?.[1] ?? href?.[2];
      segments.push(
        target === undefined || target === ""
          ? { kind: "link", value: label }
          : { kind: "link", value: label, href: target },
      );
    }
    cursor = start + match[0].length;
  }

  const tail = text.slice(cursor);
  if (tail !== "" || segments.length === 0) {
    segments.push({ kind: "text", value: tail });
  }
  return segments;
}

/**
 * The target as an href, or null when nothing should be clickable.
 *
 * Same rule as `LinkPreviewCard`: only http(s) becomes a real anchor. A
 * `weixin://` target is a WeChat-app action -- opening a transfer, a group
 * receipt, a complaint form -- that nothing outside WeChat can carry out.
 */
export function openableHref(href: string | undefined): string | null {
  if (href === undefined || href.trim() === "") return null;
  try {
    const parsed = new URL(href);
    return parsed.protocol === "http:" || parsed.protocol === "https:" ? parsed.href : null;
  } catch {
    return null;
  }
}
