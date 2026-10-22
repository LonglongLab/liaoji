import type { SearchHit } from "@/api/archiveApi";
import { isSameLocalDay } from "@/utils/time";

export interface HitGroup {
  key: string;
  title: string;
  hits: SearchHit[];
}

const startOfLocalDay = (date: Date): Date =>
  new Date(date.getFullYear(), date.getMonth(), date.getDate());

/**
 * 图片墙按时间分组，和微信一样先给「今天／本周／本月」，更早的按年月成组。
 * 归档是只读的，分组只看消息时间，不需要考虑新消息插队。
 */
export const groupHitsByRecency = (
  hits: SearchHit[],
  nowMs: number = Date.now(),
): HitGroup[] => {
  const now = new Date(nowMs);
  const today = startOfLocalDay(now);
  const weekStart = new Date(today);
  weekStart.setDate(today.getDate() - ((today.getDay() + 6) % 7));
  const monthStart = new Date(now.getFullYear(), now.getMonth(), 1);

  const groups: HitGroup[] = [];
  const indexByKey = new Map<string, number>();

  for (const hit of hits) {
    const at = new Date(hit.created_at * 1000);
    let key: string;
    let title: string;
    if (isSameLocalDay(hit.created_at, nowMs / 1000)) {
      key = "today";
      title = "今天";
    } else if (at >= weekStart) {
      key = "week";
      title = "本周";
    } else if (at >= monthStart) {
      key = "month";
      title = "本月";
    } else {
      key = `${String(at.getFullYear())}-${String(at.getMonth() + 1)}`;
      title = `${String(at.getFullYear())}年${String(at.getMonth() + 1)}月`;
    }

    const index = indexByKey.get(key);
    if (index === undefined) {
      indexByKey.set(key, groups.length);
      groups.push({ key, title, hits: [hit] });
      continue;
    }
    groups[index]?.hits.push(hit);
  }

  return groups;
};
