import type { DirectoryEntry } from "@/lib/searchScopes";
import type { RenderConversation } from "@/lib/transcript";

/** Shared entry adapter for conversation selectors in search and export. */
export const conversationDirectoryEntry = (
  item: Pick<RenderConversation, "id" | "display_name" | "avatar_ref">,
  avatarFor?: (id: string) => string | null,
): DirectoryEntry => ({
  id: item.id,
  name: item.display_name,
  avatarRef: avatarFor === undefined ? item.avatar_ref ?? null : avatarFor(item.id),
});
