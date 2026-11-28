import type { JSZipObject } from "jszip";

import {
  createZipBudget,
  loadGuardedZip,
  readZipTextBounded,
} from "@/lib/filePreview/zipGuard";

export type SheetLimits = {
  rowLimit: number;
  columnLimit: number;
};

export type ParsedSheetGrid = {
  name: string;
  columns: string[];
  rows: string[][];
  totalRows: number;
  totalColumns: number;
};

export const parseCsvGrid = (text: string, limits: SheetLimits): ParsedSheetGrid[] => {
  const rows: string[][] = [];
  let row: string[] = [];
  let cell = "";
  let inQuotes = false;

  for (let i = 0; i < text.length; i += 1) {
    const ch = text[i] ?? "";
    if (inQuotes) {
      if (ch === '"') {
        if (text[i + 1] === '"') {
          cell += '"';
          i += 1;
        } else {
          inQuotes = false;
        }
      } else {
        cell += ch;
      }
      continue;
    }
    if (ch === '"' && cell === "") {
      inQuotes = true;
    } else if (ch === ",") {
      row.push(cell);
      cell = "";
    } else if (ch === "\r" || ch === "\n") {
      if (ch === "\r" && text[i + 1] === "\n") i += 1;
      row.push(cell);
      rows.push(row);
      row = [];
      cell = "";
    } else {
      cell += ch;
    }
  }
  if (cell !== "" || row.length > 0) {
    row.push(cell);
    rows.push(row);
  }

  const nonBlank = rows.filter((entry) => entry.some((value) => value !== ""));
  if (nonBlank.length === 0) return [];
  const totalRows = nonBlank.length;
  const totalColumns = nonBlank.reduce((widest, entry) => Math.max(widest, entry.length), 0);
  const bounded = nonBlank
    .slice(0, limits.rowLimit)
    .map((entry) => entry.slice(0, limits.columnLimit));
  const [header, ...rest] = bounded;
  return [
    {
      name: "CSV",
      columns: header ?? [],
      rows: rest,
      totalRows,
      totalColumns,
    },
  ];
};

const localTagName = (element: Element): string => {
  const tag = element.tagName;
  const colon = tag.indexOf(":");
  return colon === -1 ? tag : tag.slice(colon + 1);
};

const childElements = (parent: Element, localName?: string): Element[] => {
  const result: Element[] = [];
  for (let i = 0; i < parent.children.length; i += 1) {
    const child = parent.children[i];
    if (child === undefined) continue;
    if (localName === undefined || localTagName(child) === localName) {
      result.push(child);
    }
  }
  return result;
};

const collectText = (root: Element): string => {
  const parts: string[] = [];
  const visit = (node: Element): void => {
    const name = localTagName(node);
    if (name === "t") {
      parts.push(node.textContent);
      return;
    }
    if (name === "rPh") return;
    for (const child of childElements(node)) {
      visit(child);
    }
  };
  visit(root);
  return parts.join("");
};

const columnIndexFromRef = (ref: string | null): number | null => {
  if (ref === null) return null;
  const match = /^([A-Za-z]+)\d+$/.exec(ref.trim());
  const letters = match?.[1]?.toUpperCase();
  if (letters === undefined) return null;
  let index = 0;
  for (let i = 0; i < letters.length; i += 1) {
    index = index * 26 + (letters.charCodeAt(i) - 64);
  }
  return index > 0 ? index - 1 : null;
};

const normalizeZipPath = (target: string): string => {
  const cleaned = target.replaceAll("\\", "/").replace(/^\/+/, "");
  if (cleaned.startsWith("xl/")) return cleaned;
  const resolved = `xl/${cleaned}`.replace(/\/+/g, "/");
  const segments: string[] = [];
  for (const part of resolved.split("/")) {
    if (part === "" || part === ".") continue;
    if (part === "..") {
      segments.pop();
    } else {
      segments.push(part);
    }
  }
  return segments.join("/");
};

const parseXml = (source: string): Document =>
  new DOMParser().parseFromString(source, "application/xml");

const readSharedStrings = (xml: string): string[] => {
  const root = parseXml(xml).documentElement;
  return childElements(root, "si").map((item) => collectText(item));
};

const readWorkbookRelationships = (xml: string): Map<string, string> => {
  const map = new Map<string, string>();
  const root = parseXml(xml).documentElement;
  for (const rel of childElements(root, "Relationship")) {
    const id = rel.getAttribute("Id");
    const target = rel.getAttribute("Target");
    if (id !== null && target !== null) {
      map.set(id, normalizeZipPath(target));
    }
  }
  return map;
};

const cellText = (cell: Element, sharedStrings: readonly string[]): string => {
  const kind = cell.getAttribute("t");
  if (kind === "inlineStr") {
    const inline = childElements(cell, "is")[0];
    return inline === undefined ? "" : collectText(inline);
  }
  const valueNode = childElements(cell, "v")[0];
  const raw = valueNode?.textContent ?? "";
  if (kind === "s") {
    const index = Number.parseInt(raw, 10);
    return Number.isNaN(index) ? "" : (sharedStrings[index] ?? "");
  }
  if (kind === "b") {
    return raw === "1" ? "TRUE" : raw === "0" ? "FALSE" : raw;
  }
  return raw;
};

const parseWorksheetXml = (
  xml: string,
  sharedStrings: readonly string[],
  limits: SheetLimits,
): Omit<ParsedSheetGrid, "name"> => {
  const root = parseXml(xml).documentElement;
  const sheetData = childElements(root, "sheetData")[0];
  if (sheetData === undefined) {
    return { columns: [], rows: [], totalRows: 0, totalColumns: 0 };
  }
  const parsedRows: string[][] = [];
  for (const rowEl of childElements(sheetData, "row")) {
    const row: string[] = [];
    let fallbackCol = 0;
    for (const cellEl of childElements(rowEl, "c")) {
      const explicitCol = columnIndexFromRef(cellEl.getAttribute("r"));
      const col = explicitCol ?? fallbackCol;
      fallbackCol = col + 1;
      const value = cellText(cellEl, sharedStrings);
      if (col >= row.length) {
        while (row.length < col) row.push("");
        row.push(value);
      } else {
        row[col] = value;
      }
    }
    if (row.some((value) => value !== "")) {
      parsedRows.push(row);
    }
  }
  const totalRows = parsedRows.length;
  const totalColumns = parsedRows.reduce((widest, entry) => Math.max(widest, entry.length), 0);
  const bounded = parsedRows
    .slice(0, limits.rowLimit)
    .map((entry) => entry.slice(0, limits.columnLimit));
  const [header, ...rest] = bounded;
  return {
    columns: header ?? [],
    rows: rest,
    totalRows,
    totalColumns,
  };
};

export const parseXlsxSheets = async (
  buffer: ArrayBuffer,
  limits: SheetLimits,
): Promise<ParsedSheetGrid[]> => {
  const { files } = await loadGuardedZip(buffer);
  const budget = createZipBudget();
  const entryMap = new Map<string, JSZipObject>();
  for (const entry of files) {
    if (!entry.dir) {
      entryMap.set(entry.name.replace(/^\/+/, "").toLowerCase(), entry);
    }
  }

  const workbookEntry = entryMap.get("xl/workbook.xml");
  if (workbookEntry === undefined) return [];
  const workbookRoot = parseXml(await readZipTextBounded(workbookEntry, budget)).documentElement;
  const sheetsContainer = childElements(workbookRoot, "sheets")[0];
  if (sheetsContainer === undefined) return [];

  const relsEntry = entryMap.get("xl/_rels/workbook.xml.rels");
  const rels =
    relsEntry === undefined
      ? new Map<string, string>()
      : readWorkbookRelationships(await readZipTextBounded(relsEntry, budget));

  const sharedEntry = entryMap.get("xl/sharedstrings.xml");
  const sharedStrings =
    sharedEntry === undefined
      ? []
      : readSharedStrings(await readZipTextBounded(sharedEntry, budget));

  const sheets: ParsedSheetGrid[] = [];
  const sheetElements = childElements(sheetsContainer, "sheet");
  for (let index = 0; index < sheetElements.length; index += 1) {
    const sheetEl = sheetElements[index];
    if (sheetEl === undefined) continue;
    const name = sheetEl.getAttribute("name") ?? `Sheet${String(index + 1)}`;
    const relId =
      sheetEl.getAttribute("r:id") ??
      sheetEl.getAttributeNS(
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
        "id",
      ) ??
      sheetEl.getAttribute("id");
    const targetPath =
      (relId !== null ? rels.get(relId) : undefined) ??
      `xl/worksheets/sheet${String(index + 1)}.xml`;
    const sheetEntry = entryMap.get(targetPath.toLowerCase());
    if (sheetEntry === undefined) continue;
    const grid = parseWorksheetXml(
      await readZipTextBounded(sheetEntry, budget),
      sharedStrings,
      limits,
    );
    sheets.push({ name, ...grid });
  }
  return sheets;
};
