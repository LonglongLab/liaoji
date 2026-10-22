<script setup lang="ts">
import { computed } from "vue";

import { splitTextWithEmoticons } from "@/lib/wechatEmoticons";
import { openableHref, splitTextWithInlineLinks } from "@/lib/wechatInlineLinks";

/** One run of a search snippet, flagged when it matched the query. */
export interface HighlightPart {
  readonly text: string;
  readonly matched: boolean;
}

const props = defineProps<{
  /** Plain message text. Ignored when `parts` is given. */
  text?: string;
  /** Pre-split highlight runs, for search snippets. */
  parts?: readonly HighlightPart[];
}>();

interface Piece {
  key: string;
  value: string;
  token: string | undefined;
  /** The element this run renders as. */
  tag: "mark" | "span" | "a";
  /** Set only on a run that renders as `<a>`, i.e. one a browser can open. */
  href: string | null;
  isLink: boolean;
}

// Emoticons and links are both resolved inside each highlight run, so a token
// split down the middle by a query match (searching "捂" inside "[捂脸]", or a
// snippet that ends mid-`href`) stays literal text rather than being
// half-replaced. That degrades to exactly what WeChat shows on a client that
// does not know the emoticon.
const pieces = computed<Piece[]>(() => {
  const runs: readonly HighlightPart[] = props.parts ?? [
    { text: props.text ?? "", matched: false },
  ];
  const result: Piece[] = [];
  runs.forEach((run, runIndex) => {
    splitTextWithInlineLinks(run.text).forEach((segment, segmentIndex) => {
      if (segment.kind === "link") {
        // A matched run stays a `<mark>` even when it is a link: the highlight
        // is the reason the row is on screen at all, and a search hit is not a
        // place anyone navigates from. Either way the label reads as WeChat
        // renders it, which is the whole point.
        const href = run.matched ? null : openableHref(segment.href);
        result.push({
          key: `${String(runIndex)}-${String(segmentIndex)}-link`,
          value: segment.value,
          token: undefined,
          tag: run.matched ? "mark" : href === null ? "span" : "a",
          href,
          isLink: true,
        });
        return;
      }
      splitTextWithEmoticons(segment.value).forEach((piece, index) => {
        if (piece.kind === "text" && piece.value === "") return;
        result.push({
          key: `${String(runIndex)}-${String(segmentIndex)}-${String(index)}`,
          value: piece.value,
          token: piece.token,
          tag: run.matched ? "mark" : "span",
          href: null,
          isLink: false,
        });
      });
    });
  });
  return result;
});

const classFor = (piece: Piece): string | undefined => {
  if (piece.isLink) {
    // A `weixin://` target is an in-app action — opening a transfer, a group
    // receipt, a complaint form — that nothing outside WeChat can carry out.
    // It keeps the link colour, because that is what the sentence looked like
    // when it was sent, and loses the hover affordance, because it is inert.
    return piece.href === null ? "wechat-inline-link is-inert" : "wechat-inline-link";
  }
  return piece.token === undefined ? undefined : "wechat-emoticon";
};
</script>

<template>
  <!-- v-text rather than interpolation: a line break the formatter would add
       around `{{ }}` would become real whitespace inside the message body.
       The three branches are kept adjacent with no gap for the same reason,
       and stay three elements rather than one `<component :is>` because
       `vue/no-v-text-v-html-on-component` forbids v-text on that. -->
  <template
    v-for="piece in pieces"
    :key="piece.key"
  >
    <a
      v-if="piece.tag === 'a'"
      class="wechat-inline-link"
      :href="piece.href ?? undefined"
      target="_blank"
      rel="noreferrer"
      v-text="piece.value"
    /><mark
      v-else-if="piece.tag === 'mark'"
      :class="classFor(piece)"
      :role="piece.token === undefined ? undefined : 'img'"
      :title="piece.token"
      :aria-label="piece.token"
      v-text="piece.value"
    /><span
      v-else
      :class="classFor(piece)"
      :role="piece.token === undefined ? undefined : 'img'"
      :title="piece.token"
      :aria-label="piece.token"
      v-text="piece.value"
    />
  </template>
</template>
