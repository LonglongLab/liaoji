import { definePreset } from "@primeuix/themes";
import Aura from "@primeuix/themes/aura";

// Shared mature controls follow the archive's existing paper and green tokens.
export const uiOptions = {
  locale: {
    startsWith: "开头是", contains: "包含", notContains: "不包含", endsWith: "结尾是",
    equals: "等于", notEquals: "不等于", noFilter: "不过滤",
    emptySearchMessage: "没有匹配的选项", emptyFilterMessage: "没有匹配的选项", emptySelectionMessage: "未选择选项", emptyMessage: "暂无选项",
    searchMessage: "{0} 个匹配选项", selectionMessage: "已选择 {0} 项",
    chooseDate: "选择日期", prevMonth: "上个月", nextMonth: "下个月", prevYear: "上一年", nextYear: "下一年",
    prevDecade: "前十年", nextDecade: "后十年", chooseMonth: "选择月份", chooseYear: "选择年份",
    dayNames: ["星期日", "星期一", "星期二", "星期三", "星期四", "星期五", "星期六"],
    dayNamesShort: ["日", "一", "二", "三", "四", "五", "六"],
    dayNamesMin: ["日", "一", "二", "三", "四", "五", "六"],
    monthNames: ["一月", "二月", "三月", "四月", "五月", "六月", "七月", "八月", "九月", "十月", "十一月", "十二月"],
    monthNamesShort: ["1月", "2月", "3月", "4月", "5月", "6月", "7月", "8月", "9月", "10月", "11月", "12月"],
    today: "今天", clear: "清除", firstDayOfWeek: 1, dateFormat: "yy-mm-dd",
  },
  theme: {
    preset: definePreset(Aura, {
      semantic: {
        primary: { 50: "#eef1e7", 100: "#cddac2", 200: "#b2d3a8", 300: "#81af73", 400: "#45985d", 500: "#137a3d", 600: "#0e6f36", 700: "#0b5d2d", 800: "#084c25", 900: "#063b1d", 950: "#032612" },
        formField: { borderRadius: "8px", paddingX: "12px", paddingY: "11px" },
        colorScheme: { light: {
          primary: { color: "var(--accent-solid)", contrastColor: "#ffffff", hoverColor: "var(--accent-ink)" },
          formField: { background: "var(--surface-pop)", color: "var(--ink)", borderColor: "var(--rule)", hoverBorderColor: "var(--ink-faint)", focusBorderColor: "var(--accent-solid)", placeholderColor: "var(--ink-faint)" },
          content: { background: "var(--surface-pop)", color: "var(--ink)", borderColor: "var(--rule)" },
        } },
      },
    }),
    options: { darkModeSelector: false },
  },
};
