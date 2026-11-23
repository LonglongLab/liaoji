/** Directory keys end in the search term. Reuse rows only within the same account and directory. */
export const directoryPlaceholder = <T>(key: readonly unknown[]) =>
  (previous: T | undefined, query: { queryKey: readonly unknown[] } | undefined): T | undefined => {
    const previousKey = query?.queryKey;
    if (previousKey?.length !== key.length) return undefined;
    return key.slice(0, -1).every((part, index) => part === previousKey[index]) ? previous : undefined;
  };
