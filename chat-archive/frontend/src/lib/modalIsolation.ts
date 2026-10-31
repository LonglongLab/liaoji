const layers: HTMLElement[] = [];
const isolated = new WeakMap<HTMLElement, { count: number; previous: boolean }>();

/** Keep the original inert state, including when one confirmation opens above another. */
export const isolateModal = (root: HTMLElement): { isTop: () => boolean; release: () => void } => {
  const touched: HTMLElement[] = [];
  let branch = root.parentElement;
  while (branch !== null && branch !== document.body) {
    const parent = branch.parentElement;
    if (parent === null) break;
    for (const sibling of parent.children) {
      if (!(sibling instanceof HTMLElement) || sibling === branch) continue;
      const entry = isolated.get(sibling) ?? { count: 0, previous: sibling.inert };
      entry.count += 1;
      isolated.set(sibling, entry);
      sibling.inert = true;
      touched.push(sibling);
    }
    branch = parent;
  }
  layers.push(root);
  let released = false;
  return {
    isTop: () => layers.at(-1) === root,
    release: () => {
      if (released) return;
      released = true;
      const index = layers.indexOf(root);
      if (index >= 0) layers.splice(index, 1);
      for (const element of touched) {
        const entry = isolated.get(element);
        if (entry === undefined) continue;
        entry.count -= 1;
        if (entry.count === 0) { element.inert = entry.previous; isolated.delete(element); }
      }
    },
  };
};
