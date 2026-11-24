export interface CosRegionOption {
  readonly value: string;
  readonly name: string;
  readonly label: string;
}

export const DEFAULT_STORAGE_REGION = "ap-nanjing";
export const DEFAULT_BACKUP_REGION = "ap-guangzhou";

export const COS_REGIONS: readonly CosRegionOption[] = [
  { value: "ap-nanjing", name: "南京", label: "南京（ap-nanjing）" },
  { value: "ap-guangzhou", name: "广州", label: "广州（ap-guangzhou）" },
  { value: "ap-shanghai", name: "上海", label: "上海（ap-shanghai）" },
  { value: "ap-beijing", name: "北京", label: "北京（ap-beijing）" },
  { value: "ap-chengdu", name: "成都", label: "成都（ap-chengdu）" },
  { value: "ap-chongqing", name: "重庆", label: "重庆（ap-chongqing）" },
  { value: "ap-hongkong", name: "中国香港", label: "中国香港（ap-hongkong）" },
  { value: "ap-singapore", name: "新加坡", label: "新加坡（ap-singapore）" },
];

const COS_REGION_BY_VALUE = new Map(COS_REGIONS.map((item) => [item.value, item]));

export const cosEndpointForRegion = (region: string): string =>
  `https://cos.${region}.myqcloud.com`;

export const cosRegionLabel = (region: string | null | undefined): string => {
  if (region === null || region === undefined) return "—";
  return COS_REGION_BY_VALUE.get(region)?.label ?? region;
};

export const cosRegionName = (region: string | null | undefined): string => {
  if (region === null || region === undefined) return "";
  return COS_REGION_BY_VALUE.get(region)?.name ?? region;
};
