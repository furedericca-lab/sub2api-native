import {
  Settings2,
  SlidersHorizontal,
  Users,
  Network,
  Layers3,
  KeyRound,
  type LucideIcon,
} from "lucide-react";

export type NavigationItem = {
  to: string;
  label: string;
  shortLabel: string;
  icon: LucideIcon;
};

export type NavigationGroup = {
  label: string;
  items: readonly NavigationItem[];
};

export const navigationGroups: readonly NavigationGroup[] = [
  {
    label: "资源管理",
    items: [
      { to: "/profiles", label: "站点池", shortLabel: "站点", icon: Layers3 },
      { to: "/account-pool", label: "账户池", shortLabel: "账户", icon: Users },
      { to: "/api-keys", label: "密钥池", shortLabel: "密钥", icon: KeyRound },
    ],
  },
  {
    label: "调度状态",
    items: [{ to: "/relay", label: "API 聚合", shortLabel: "聚合", icon: Network }],
  },
  {
    label: "系统",
    items: [
      { to: "/settings/outlook", label: "邮箱设置", shortLabel: "邮箱", icon: Settings2 },
      { to: "/settings/registration", label: "注册设置", shortLabel: "注册", icon: SlidersHorizontal },
    ],
  },
];

export const navigationItems: readonly NavigationItem[] = navigationGroups.flatMap((group) => group.items);

export const mobilePrimaryItems: readonly NavigationItem[] = [
  navigationGroups[0].items[0],
  navigationGroups[0].items[1],
  navigationGroups[1].items[0],
];
