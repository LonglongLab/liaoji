import { defineStore } from "pinia";

/**
 * 多选态：选中的是哪几条消息（PSU-2.168）。
 *
 * **存 id 不存下标。** 转录往上续读时会把更早的一批整体贴到数组前面，同一条消息
 * 在贴入前后的下标不是同一个——`MessageTranscript` 的 `TranscriptAnchor` 已经为
 * 同一件事踩过一次坑（那里的注释写着「记消息而不是记下标」）。这里的每个操作都
 * 只认 id，需要顺序时由调用方把当下那串 id 传进来现算。
 *
 * 「哪些可选」也不住在这里：可不可选是转录说了算（系统提示不可选），store 只是
 * 照着做。传进来的 `selectable` 是一个判定，不是一份要同步维护的副本。
 */
interface MessageSelectionState {
  active: boolean;
  /** 选中的消息 id。用 Set 是为了 toggle/命中都是常数时间，转录可以很长。 */
  picked: Set<string>;
  /**
   * 最后点的那一条：`Shift`+点击从它连选到当前这条（交互第二版）。
   * 和微信、文件管理器一样按「上一次点的」算，不按「最早选的」。
   */
  anchor: string | null;
}

export const useMessageSelectionStore = defineStore("messageSelection", {
  state: (): MessageSelectionState => ({
    active: false,
    picked: new Set<string>(),
    anchor: null,
  }),
  getters: {
    count: (state): number => state.picked.size,
    isPicked: (state) => (messageId: string): boolean => state.picked.has(messageId),
    /**
     * 选中的消息，按**转录里的顺序**排好。
     *
     * 导出读起来必须是读者看到的顺序，不是他点选的顺序。顺序的唯一依据是调用方
     * 传进来的当下这串 id；不在这串里的（已经被卸载出加载窗口）自然落选。
     */
    orderedIds: (state) => (order: readonly string[]): string[] =>
      order.filter((id) => state.picked.has(id)),
  },
  actions: {
    /** Explicit menu entry: start with no message picked. */
    begin(): void {
      this.active = true;
      this.picked = new Set();
      this.anchor = null;
    },
    /**
     * 进入多选态，并把发起的那一条选上。
     *
     * `selectable` 给了就校验：从一条不可选的消息上进多选没有意义，进去之后
     * 那一条还是灰的，用户会以为是坏了。
     */
    enter(messageId: string, selectable?: ReadonlySet<string>): void {
      if (selectable !== undefined && !selectable.has(messageId)) return;
      this.active = true;
      this.picked = new Set([messageId]);
      this.anchor = messageId;
    },
    toggle(messageId: string): void {
      this.anchor = messageId;
      // 整份换掉而不是原地增删：Pinia 对 Set 的原地改动不会触发依赖它的计算属性。
      const next = new Set(this.picked);
      if (next.has(messageId)) next.delete(messageId);
      else next.add(messageId);
      this.picked = next;
    },
    /**
     * 「选到这里」：把已选的头尾之间整段选上，跳过不可选的。
     *
     * 端点取的是**转录顺序**里最早和最晚的两条已选消息，不是点选的先后——
     * 先点晚的再点早的是很自然的操作，两种点法应当给出同一段。
     */
    selectRange(order: readonly string[], selectable: ReadonlySet<string>): void {
      const positions = order.flatMap((id, index) => (this.picked.has(id) ? [index] : []));
      // 至少要有两个**还在这串里**的端点。`indexOf` 的 -1 不能当下标用：
      // 那会把区间的一头拉到转录开头去。
      if (positions.length < 2) return;
      const from = positions[0] as number;
      const to = positions[positions.length - 1] as number;
      const next = new Set(this.picked);
      for (let index = from; index <= to; index += 1) {
        const id = order[index];
        if (id !== undefined && selectable.has(id)) next.add(id);
      }
      this.picked = next;
    },
    /**
     * `Shift`+点击：从锚点到这一条整段选上，跳过不可选的。**只加不减**——
     * 段外已经选了的照旧留着。
     *
     * 锚点不在眼下这串里（已被卸载出加载窗口）时拿不出一段，退回普通点选。
     */
    extendTo(messageId: string, order: readonly string[], selectable: ReadonlySet<string>): void {
      const from = this.anchor === null ? -1 : order.indexOf(this.anchor);
      const to = order.indexOf(messageId);
      if (from < 0 || to < 0) {
        this.toggle(messageId);
        return;
      }
      const next = new Set(this.picked);
      for (let index = Math.min(from, to); index <= Math.max(from, to); index += 1) {
        const id = order[index];
        if (id !== undefined && selectable.has(id)) next.add(id);
      }
      this.picked = next;
      this.anchor = messageId;
    },
    /**
     * 框选松手：把框碰到的并进来（交互第二版，照 PC 微信）。
     *
     * 阅读态下框到了可选的消息，就此进入多选态；一条都没框到就什么都不做——
     * 在空白处随手拖一下不该把人带进多选。`ids` 按转录顺序给，末一条当锚点。
     *
     * `base` 是拉框开始前已经选了的那些：多选态下框是实时往它上面加的，框缩回来
     * 时要还原成它，而不是把框曾经碰过的都留下。不给就以眼下已选的为底。
     */
    pickMany(ids: readonly string[], selectable: ReadonlySet<string>, base?: ReadonlySet<string>): void {
      const usable = ids.filter((id) => selectable.has(id));
      if (!this.active && usable.length === 0) return;
      const next = new Set(base ?? (this.active ? this.picked : []));
      for (const id of usable) next.add(id);
      this.active = true;
      this.picked = next;
      const last = usable[usable.length - 1];
      if (last !== undefined) this.anchor = last;
    },
    /** `Ctrl+A`：眼下已加载的全部可选消息。 */
    selectAll(order: readonly string[], selectable: ReadonlySet<string>): void {
      this.picked = new Set(order.filter((id) => selectable.has(id)));
    },
    exit(): void {
      this.active = false;
      this.picked = new Set<string>();
      this.anchor = null;
    },
  },
});
