import {
  AtSign,
  CircleDot,
  Instagram,
  MessageCircle,
  Phone,
  Send,
} from "lucide-react";

export const platforms = [
  {
    id: "bale",
    name: "بله",
    icon: MessageCircle,
    summary: "فوروارد از منبع و کمپین انبوه",
    statusLabel: "آماده",
    statusTone: "success",
    ready: true,
  },
  {
    id: "telegram",
    name: "تلگرام",
    icon: Send,
    summary: "آماده طراحی، نیازمند اتصال Backend",
    statusLabel: "نیازمند اتصال",
    statusTone: "warning",
    ready: false,
  },
  {
    id: "whatsapp",
    name: "واتساپ",
    icon: Phone,
    summary: "آماده طراحی، Adapter عملیاتی ندارد",
    statusLabel: "به‌زودی",
    statusTone: "neutral",
    ready: false,
  },
  {
    id: "instagram",
    name: "اینستاگرام",
    icon: Instagram,
    summary: "برای فاز چندپیام‌رسانی نگه داشته شده",
    statusLabel: "به‌زودی",
    statusTone: "neutral",
    ready: false,
  },
  {
    id: "rubika",
    name: "روبیکا",
    icon: CircleDot,
    summary: "نیازمند اتصال حساب و Adapter",
    statusLabel: "نیازمند اتصال",
    statusTone: "warning",
    ready: false,
  },
  {
    id: "eitaa",
    name: "ایتا",
    icon: AtSign,
    summary: "در معماری UI پیش‌بینی شده",
    statusLabel: "به‌زودی",
    statusTone: "neutral",
    ready: false,
  },
  {
    id: "soroush",
    name: "سروش پلاس",
    icon: MessageCircle,
    summary: "در معماری UI پیش‌بینی شده",
    statusLabel: "به‌زودی",
    ready: false,
    statusTone: "neutral",
  },
];

export function getPlatform(platformId) {
  return platforms.find((platform) => platform.id === platformId) ?? platforms[0];
}
